#!/usr/bin/env python3
"""
codeloom — a map of your codebase, in one command, for AI agents.

Single-file, zero-dependency, no-daemon. Walks a project, respects .gitignore,
and emits a compact "table of contents" (folder tree + per-module one-liners +
entry points) that Claude Code, Cursor, Codex, Gemini or any coding agent can
read in a second to build a mental model BEFORE burning tokens on grep/read.

Usage:
    codeloom                       # map current directory -> stdout
    codeloom /path/to/repo         # map a specific repo
    codeloom --write MAP.md        # also write to MAP.md (gitignored-friendly)
    codeloom --json                # machine-readable JSON for tooling
    codeloom --max-files 2000      # cap traversal (default 5000)

Runs 100% locally. No network, no API keys, no GPU.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

VERSION = "0.26.0"

# Adaptive full-source threshold: symbols at or below this many tokens return
# their actual implementation by default (no --full needed); larger symbols
# stay summary-first to avoid a token dump. ~40 tokens ≈ a small function.
ADAPTIVE_FULL_THRESHOLD = 40

# --------------------------------------------------------------------------- #
# Optional progressive-enhancement backends.
# codeloom stays zero-dependency by default, but gets dramatically more precise
# when richer tools are present. Each backend is gated on a `try: import` so
# the core always works with stdlib only. See README "Known limits" for how
# each limit is removed.
# --------------------------------------------------------------------------- #

# --- Optional tree-sitter backend (multi-language precision) ----------------
# If tree_sitter + a language grammar are importable, use real AST parsing for
# that language instead of regex. Falls back to regex when absent.
_TS_AVAILABLE = False
_TS_LANG = {}  # ext -> (Language, query-for-functions)
try:
    import tree_sitter
    from tree_sitter import Language, Parser  # noqa: F401
    _TS_AVAILABLE = True
except ImportError:
    _TS_AVAILABLE = False

def _ts_grammar_for(ext: str):
    """Return a tree-sitter Language for a file extension, or None.
    Wires up the languages codeloom already supports via regex, so
    --install-grammars gives real AST precision across all of them."""
    if not _TS_AVAILABLE:
        return None
    try:
        if ext == ".py":
            import tree_sitter_python
            return Language(tree_sitter_python.language())
        if ext in (".js", ".jsx"):
            import tree_sitter_javascript
            return Language(tree_sitter_javascript.language())
        if ext in (".ts", ".tsx"):
            import tree_sitter_typescript
            return Language(tree_sitter_typescript.language())
        if ext == ".go":
            import tree_sitter_go
            return Language(tree_sitter_go.language())
        if ext == ".rs":
            import tree_sitter_rust
            return Language(tree_sitter_rust.language())
        if ext == ".java":
            import tree_sitter_java
            return Language(tree_sitter_java.language())
        if ext in (".c", ".h"):
            import tree_sitter_c
            return Language(tree_sitter_c.language())
        if ext in (".cpp", ".hpp", ".cc", ".cxx"):
            import tree_sitter_cpp
            return Language(tree_sitter_cpp.language())
        if ext == ".cs":
            import tree_sitter_c_sharp
            return Language(tree_sitter_c_sharp.language())
        if ext == ".rb":
            import tree_sitter_ruby
            return Language(tree_sitter_ruby.language())
        if ext == ".php":
            import tree_sitter_php
            return Language(tree_sitter_php.language())
        if ext == ".swift":
            import tree_sitter_swift
            return Language(tree_sitter_swift.language())
        if ext == ".kt":
            import tree_sitter_kotlin
            return Language(tree_sitter_kotlin.language())
        if ext == ".dart":
            import tree_sitter_dart
            return Language(tree_sitter_dart.language())
        if ext == ".lua":
            import tree_sitter_lua
            return Language(tree_sitter_lua.language())
        if ext == ".sh":
            import tree_sitter_bash
            return Language(tree_sitter_bash.language())
        if ext in (".ex", ".exs"):
            import tree_sitter_elixir
            return Language(tree_sitter_elixir.language())
        if ext == ".ml":
            import tree_sitter_ocaml
            return Language(tree_sitter_ocaml.language())
        if ext == ".scala":
            import tree_sitter_scala
            return Language(tree_sitter_scala.language())
        if ext == ".hs":
            import tree_sitter_haskell
            return Language(tree_sitter_haskell.language())
        if ext == ".zig":
            import tree_sitter_zig
            return Language(tree_sitter_zig.language())
        if ext == ".pl":
            import tree_sitter_perl
            return Language(tree_sitter_perl.language())
        if ext == ".fs":
            import tree_sitter_fsharp
            return Language(tree_sitter_fsharp.language())
        if ext == ".ps1":
            import tree_sitter_powershell
            return Language(tree_sitter_powershell.language())
    except Exception:
        return None
    return None

def _ts_parse(path: str, ext: str):
    """Parse a file with tree-sitter, returning the root node or None."""
    lang = _ts_grammar_for(ext)
    if lang is None:
        return None
    try:
        parser = Parser(lang)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tree = parser.parse(f.read().encode("utf-8"))
        return tree.root_node
    except Exception:
        return None

# --------------------------------------------------------------------------- #
# Parallel file parsing (--parallel) — stdlib multiprocessing, opt-in
# --------------------------------------------------------------------------- #
# Heavy ops (--cross, --deadcode, --calls) re-parse every file. On a monorepo
# that's single-threaded and slow. --parallel dispatches the per-file reads
# across processes (stdlib multiprocessing, no deps) for a big speedup.

def _read_file_worker(args):
    """Module-level worker for multiprocessing: read a file's text."""
    path, ext = args
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return (path, fh.read())
    except OSError:
        return (path, None)

def read_files_parallel(files: List[str], parallel: bool = False) -> dict:
    """Read all files' text. With parallel=True, use multiprocessing."""
    if not parallel or len(files) < 50:
        out = {}
        for f in files:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    out[f] = fh.read()
            except OSError:
                out[f] = None
        return out
    import multiprocessing as mp
    exts = {f: os.path.splitext(f)[1].lower() for f in files}
    with mp.Pool() as pool:
        results = pool.map(_read_file_worker, [(f, exts[f]) for f in files])
    return {path: text for path, text in results}

def _scan_calls_worker(args):
    """Module-level worker: scan one file's text for call edges.
    Returns (module, {caller: set(callees)}) or (module, None) if skipped."""
    f, text, root, all_defined = args
    ext = os.path.splitext(f)[1].lower()
    if ext not in CALL_LANG_RULES:
        return (None, None)
    mod = module_name_of(f, root)
    if text is None:
        return (mod, None)
    # tree-sitter fast-path
    ts_root = _ts_parse(f, ext)
    if ts_root is not None:
        edges = {}
        for caller, callee in _ts_call_edges(ts_root):
            if callee in all_defined and callee != caller:
                edges.setdefault(caller, set()).add(callee)
        return (mod, {k: v for k, v in edges.items() if v})
    # string/comment-aware scanner
    def_re, _ = CALL_LANG_RULES[ext]
    clean = _strip_strings_comments(text, ext)
    clean_lines = clean.splitlines()
    current_func = None
    edges = {}
    for line, clean_line in zip(text.splitlines(), clean_lines):
        dm = re.match(def_re, clean_line)
        if dm:
            name = next((g for g in dm.groups() if g), None)
            current_func = name
            edges.setdefault(current_func, set())
            for cm in re.finditer(r"\b(\w+)\s*\(", clean_line):
                callee = cm.group(1)
                if callee in all_defined and callee != current_func:
                    edges[current_func].add(callee)
            continue
        if current_func:
            for cm in re.finditer(r"\b(\w+)\s*\(", clean_line):
                callee = cm.group(1)
                if callee in all_defined and callee != current_func:
                    edges[current_func].add(callee)
    return (mod, {k: v for k, v in edges.items() if v})

def install_grammars(do_install: bool = False) -> str:
    """One-command opt-in installer for tree-sitter language grammars.
    Keeps the single-file zero-dep core; grammars are an optional precision
    upgrade. With do_install=True, actually runs pip install."""
    pkgs = [
        "tree-sitter",
        "tree-sitter-python",
        "tree-sitter-javascript",
        "tree-sitter-typescript",
        "tree-sitter-go",
        "tree-sitter-rust",
        "tree-sitter-java",
        "tree-sitter-c",
        "tree-sitter-cpp",
        "tree-sitter-c-sharp",
        "tree-sitter-ruby",
        "tree-sitter-php",
        "tree-sitter-swift",
        "tree-sitter-kotlin",
        "tree-sitter-dart",
        "tree-sitter-lua",
        "tree-sitter-bash",
        "tree-sitter-elixir",
        "tree-sitter-ocaml",
        "tree-sitter-scala",
        "tree-sitter-haskell",
        "tree-sitter-zig",
        "tree-sitter-perl",
        "tree-sitter-fsharp",
        "tree-sitter-powershell",
    ]
    cmd = "pip install " + " ".join(pkgs)
    if do_install:
        import subprocess as _sp
        print("Installing tree-sitter grammars (this may take a minute)...")
        r = _sp.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode == 0:
            return "Installed tree-sitter grammars. codeloom now uses precise AST parsing.\n"
        return f"Install failed (exit {r.returncode}). Run manually:\n  {cmd}\n"
    return (
        "codeloom --install-grammars\n"
        "==========================\n"
        "Installs optional tree-sitter language grammars for precise multi-language\n"
        "AST parsing (instead of the regex/brace-matching fallback).\n\n"
        "This is OPT-IN — codeloom works fully without it (zero-dep, single file).\n"
        "Run:\n\n"
        "  codeloom --install-grammars --yes\n\n"
        "to actually install, or manually:\n\n"
        f"  {cmd}\n\n"
        "After installing, codeloom auto-detects the grammars and uses real AST\n"
        "parsing for those languages. No config needed.\n"
    )

def _ts_function_names(root_node) -> set:
    """Extract function/class names from a tree-sitter tree (best-effort)."""
    names = set()
    if root_node is None:
        return names
    def walk(node):
        t = node.type
        if t in ("function_definition", "function_declaration", "method_definition",
                 "class_declaration", "struct_item", "impl_item", "func_declaration",
                 "func_literal", "method_declaration", "type_declaration"):
            # find the name child
            for child in node.children:
                if child.type in ("identifier", "name", "type_identifier", "field_identifier"):
                    names.add(child.text.decode("utf-8", "replace"))
                    break
        for child in node.children:
            walk(child)
    walk(root_node)
    return names

def _ts_call_edges(root_node) -> set:
    """Extract (caller, callee) pairs from a tree-sitter tree (best-effort)."""
    edges = set()
    if root_node is None:
        return edges
    def walk(node):
        if node.type in ("function_definition", "function_declaration", "method_definition",
                         "func_declaration", "method_declaration"):
            caller = None
            for child in node.children:
                if child.type in ("identifier", "name", "field_identifier"):
                    caller = child.text.decode("utf-8", "replace")
                    break
            if caller:
                for sub in node.children:
                    _collect_calls(sub, caller, edges)
        for child in node.children:
            walk(child)
    walk(root_node)
    return edges

def _collect_calls(node, caller, edges):
    """Recursively collect call targets within a function body."""
    if node.type in ("call_expression", "call", "function_call"):
        # find the function name
        for child in node.children:
            if child.type in ("identifier", "field_identifier", "name"):
                callee = child.text.decode("utf-8", "replace")
                edges.add((caller, callee))
                break
    for child in node.children:
        _collect_calls(child, caller, edges)

# --- Optional embedding backend (task-scoring precision) --------------------
# If an embedding source is available, use real semantic similarity for --task
# relevance instead of token overlap. Sources (checked in order):
#   1. local sentence-transformers (if installed)
#   2. an OpenAI-compatible API via CODELOOM_EMBED_BASE_URL + CODELOOM_EMBED_API_KEY
_EMBED_AVAILABLE = False
try:
    import numpy  # noqa: F401
    _EMBED_AVAILABLE = True
except ImportError:
    _EMBED_AVAILABLE = False

def _embedding_backend():
    """Return a callable text->vector, or None if no backend available."""
    # 1. local sentence-transformers
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        def _local(texts):
            vecs = _model.encode(texts)
            return [v.tolist() for v in vecs]
        return _local
    except Exception:
        pass
    # 2. OpenAI-compatible API
    base = os.environ.get("CODELOOM_EMBED_BASE_URL")
    key = os.environ.get("CODELOOM_EMBED_API_KEY")
    if base and key:
        import urllib.request
        def _api(texts):
            req = urllib.request.Request(
                base.rstrip("/") + "/embeddings",
                data=json.dumps({"model": os.environ.get("CODELOOM_EMBED_MODEL", "text-embedding-3-small"),
                                 "input": texts}).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return [d["embedding"] for d in data["data"]]
        return _api
    return None

def _cosine_sim(a, b):
    """Cosine similarity between two vectors (stdlib, no numpy needed)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

# --- Optional runtime-trace mode (static blind spots) ----------------------
# Static analysis can't see dynamic imports/monkeypatching. --trace runs a
# command (e.g. the test suite) under sys.settrace and records the ACTUAL call
# edges at runtime. Opt-in because it executes code.
_TRACE_WRAPPER = r'''
import sys, json, os

# A sys.settrace tracer that records (caller, callee) edges for functions
# defined in the target codebase. Writes edges to a temp file on exit.
_edges = set()
_caller_stack = []

def _trace(frame, event, arg):
    if event == "call":
        code = frame.f_code
        caller = _caller_stack[-1] if _caller_stack else None
        callee = (code.co_filename, code.co_name)
        if caller:
            _edges.add((caller, callee))
        _caller_stack.append(callee)
    elif event == "return":
        if _caller_stack:
            _caller_stack.pop()
    return _trace

def _run():
    sys.settrace(_trace)
    try:
        # run the target script
        import runpy
        script = sys.argv[1]
        sys.argv = sys.argv[1:]
        runpy.run_path(script, run_name="__main__")
    finally:
        sys.settrace(None)
        out = os.environ.get("CODELOOM_TRACE_OUT", "")
        if out:
            with open(out, "w") as f:
                json.dump(list(_edges), f)

if __name__ == "__main__":
    _run()
'''

def _trace_call_edges(command: List[str], cwd: str) -> dict:
    """Run a command under sys.settrace, recording (caller, callee) edges.
    Returns {module: {caller: set(callee)}} for codebase-defined functions.
    Uses a wrapper script that installs a tracer and runs the target."""
    import subprocess
    import tempfile
    if not command:
        return {}
    # write the wrapper to a temp file
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(_TRACE_WRAPPER)
        wrapper = f.name
    out_path = os.path.join(tempfile.gettempdir(), "codeloom_trace_out.json")
    env = dict(os.environ)
    env["CODELOOM_TRACE_OUT"] = out_path
    try:
        subprocess.run(
            [sys.executable, wrapper] + command,
            cwd=cwd, env=env, timeout=60, capture_output=True,
        )
        with open(out_path, "r") as f:
            raw_edges = json.load(f)
    except Exception:
        raw_edges = []
    finally:
        try:
            os.unlink(wrapper)
            os.unlink(out_path)
        except OSError:
            pass
    # group edges by module (relative to cwd)
    result: dict = {}
    for (caller_file, caller_name), (callee_file, callee_name) in raw_edges:
        # skip stdlib/site-packages/frozen modules
        if caller_file.startswith("<") or callee_file.startswith("<"):
            continue
        if "site-packages" in caller_file or "site-packages" in callee_file:
            continue
        if "dist-packages" in caller_file or "dist-packages" in callee_file:
            continue
        try:
            cmod = os.path.relpath(caller_file, cwd)
            callee_mod = os.path.relpath(callee_file, cwd)
        except ValueError:
            continue
        # only keep codebase-defined (relative paths, not outside the repo)
        if cmod.startswith("..") or callee_mod.startswith(".."):
            continue
        result.setdefault(cmod, {}).setdefault(caller_name, set()).add(callee_name)
    return result

def render_trace(command: List[str], cwd: str) -> str:
    """Render the runtime call edges from --trace."""
    edges = _trace_call_edges(command, cwd)
    buf = io.StringIO()
    buf.write(f"# runtime trace: {' '.join(command)}\n")
    if not edges:
        buf.write("No codebase-defined call edges recorded (or command produced none).\n")
        return buf.getvalue()
    total = sum(len(c) for c in edges.values())
    buf.write(f"{len(edges)} modules, {total} callers\n\n")
    for mod, funcs in sorted(edges.items()):
        for caller, callees in sorted(funcs.items()):
            if callees:
                buf.write(f"  {mod}.{caller}() -> {', '.join(sorted(callees))}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Language / structure detection
# --------------------------------------------------------------------------- #

# map file extension -> (comment_prefix, "module" keyword set for outline)
# comment_prefix: what starts a line comment in that language (used to emit
# the one-liner as a comment so the map stays valid-looking source).
LANG_RULES: dict = {
    ".py":   ("#", {"def ", "class ", "async def ", "@"}),
    ".js":   ("//", {"function ", "class ", "const ", "export "}),
    ".ts":   ("//", {"function ", "class ", "const ", "export ", "interface ", "type "}),
    ".jsx":  ("//", {"function ", "const ", "export "}),
    ".tsx":  ("//", {"function ", "const ", "export ", "interface ", "type "}),
    ".go":   ("//", {"func ", "type ", "const ", "var "}),
    ".rs":   ("//", {"fn ", "struct ", "enum ", "trait ", "impl ", "mod "}),
    ".java": ("//", {"public ", "private ", "protected ", "class ", "interface ", "@"}),
    ".rb":   ("#",  {"def ", "class ", "module ", "@"}),
    ".php":  ("//", {"function ", "class ", "interface ", "namespace "}),
    ".c":    ("//", {"int ", "void ", "char ", "struct ", "static "}),
    ".h":    ("//", {"int ", "void ", "char ", "struct ", "#define ", "#include "}),
    ".cpp":  ("//", {"int ", "void ", "char ", "struct ", "class ", "static "}),
    ".hpp":  ("//", {"int ", "void ", "char ", "struct ", "class ", "template "}),
    ".cs":   ("//", {"public ", "private ", "protected ", "class ", "interface ", "namespace "}),
    ".swift":("//", {"func ", "class ", "struct ", "enum ", "protocol ", "extension "}),
    ".kt":   ("//", {"fun ", "class ", "interface ", "object ", "data class "}),
    ".sh":   ("#",  {"function ", "#!/"}),
    ".lua":  ("--", {"function ", "local function "}),
    ".ex":   ("#",  {"def ", "defmodule ", "defp ", "defmacro "}),
    ".exs":  ("#",  {"def ", "defmodule ", "defp "}),
    ".dart": ("//", {"void ", "int ", "class ", "abstract ", "typedef "}),
    ".ml":   ("(*", {"let ", "type ", "module "}),
    ".scala":("//", {"def ", "class ", "object ", "trait "}),
    ".r":    ("#",  {"<- function", "function(", "#'"}),
}

# Entry-point files / dirs worth calling out explicitly.
ENTRY_HINTS = {
    "main.py", "app.py", "cli.py", "index.js", "index.ts", "main.go",
    "main.rs", "manage.py", "__init__.py", "server.js", "server.ts",
    "setup.py", "pyproject.toml", "package.json", "go.mod", "Cargo.toml",
    "Makefile", "Dockerfile", "README.md",
}

# --------------------------------------------------------------------------- #
# .gitignore parsing (stdlib `fnmatch`; supports negation, anchoring, **, dir-only)
# --------------------------------------------------------------------------- #

@dataclass
class GitignoreRule:
    pattern: str      # normalized pattern (no leading ! or /)
    negated: bool     # True for '!pattern'
    anchored: bool    # True for '/pattern' or pattern containing '/'
    dir_only: bool    # True for 'pattern/'
    base: str         # directory the .gitignore lives in (for nested files)

def _normalize_glob(p: str) -> str:
    """Convert a gitignore pattern to an fnmatch-compatible glob.
    Handles ** (match any depth) and leading/trailing slashes."""
    p = p.strip()
    # collapse ** to a match-any-depth token; fnmatch doesn't do ** natively,
    # so we translate it to a pattern that matches across separators.
    # We'll handle ** by splitting on it and matching segments.
    return p

def parse_gitignore(path: str) -> List[GitignoreRule]:
    """Parse a .gitignore file into rules. Handles negation (!), slash
    anchoring, ** globs, and directory-only patterns."""
    rules: List[GitignoreRule] = []
    base = os.path.dirname(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                # strip trailing spaces (unless escaped)
                if line.endswith(" ") and not line.endswith("\\ "):
                    line = line.rstrip()
                if not line or line.startswith("#"):
                    continue
                negated = line.startswith("!")
                if negated:
                    line = line[1:]
                # strip leading slash (anchored to base)
                anchored = line.startswith("/")
                if anchored:
                    line = line[1:]
                dir_only = line.endswith("/")
                if dir_only:
                    line = line.rstrip("/")
                if not line:
                    continue
                # a pattern containing a slash (after stripping) is anchored
                if "/" in line:
                    anchored = True
                rules.append(GitignoreRule(line, negated, anchored, dir_only, base))
    except OSError:
        pass
    return rules

def _match_glob(pattern: str, relpath: str) -> bool:
    """Match a gitignore pattern against a path relative to the rule's base.
    Supports *, ?, **, and character classes via fnmatch, with ** matching
    across directory separators."""
    # Normalize separators
    relpath = relpath.replace(os.sep, "/")
    pattern = pattern.replace(os.sep, "/")

    # Handle ** (match any number of path segments)
    if "**" in pattern:
        # Split pattern on ** and match each part as a prefix/suffix
        parts = pattern.split("**")
        # If pattern is exactly '**' or '**/...', match everything under base
        if parts[0] == "" and parts[-1] == "":
            return True
        # '**/foo' matches foo at any depth
        if parts[0] == "":
            return _match_glob(parts[-1].lstrip("/"), relpath) or \
                   any(_match_glob(parts[-1].lstrip("/"), r) for r in _ancestors(relpath))
        # 'foo/**' matches everything under foo
        if parts[-1] == "":
            prefix = parts[0].rstrip("/")
            return relpath == prefix or relpath.startswith(prefix + "/")
        # 'foo/**/bar' — match foo/.../bar
        prefix = parts[0].rstrip("/")
        suffix = parts[-1].lstrip("/")
        if relpath.startswith(prefix + "/") and relpath.endswith(suffix):
            return True
        return False

    # No ** — use fnmatch on the full relative path
    import fnmatch
    return fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(os.path.basename(relpath), pattern)

def _ancestors(relpath: str) -> List[str]:
    """Return all ancestor paths of a relative path (for ** matching)."""
    parts = relpath.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]

def is_ignored(path: str, rules: List[GitignoreRule]) -> bool:
    """Check if a path is ignored, honoring negation. Returns True if ignored.
    The LAST matching rule wins (gitignore semantics). A pattern that matches a
    directory also ignores everything under it."""
    ignored = False
    for rule in rules:
        # compute path relative to the rule's base
        try:
            rel = os.path.relpath(path, rule.base)
        except ValueError:
            continue
        rel = rel.replace(os.sep, "/")
        matched = False
        if rule.anchored:
            # anchored: match the full relative path, or a directory prefix
            matched = _match_glob(rule.pattern, rel)
            if not matched and not rule.dir_only:
                # '/build' should also ignore 'build/out.js' (dir prefix)
                matched = rel.startswith(rule.pattern.rstrip("/") + "/")
        else:
            # unanchored: match basename, any path segment, or dir prefix
            matched = (_match_glob(rule.pattern, rel) or
                       _match_glob(rule.pattern, os.path.basename(rel)) or
                       any(_match_glob(rule.pattern, a) for a in _ancestors(rel)))
            if not matched:
                # 'node_modules/' should ignore 'node_modules/x.js'
                matched = rel.startswith(rule.pattern.rstrip("/") + "/")
        if not matched:
            continue
        # dir_only patterns only match directories (or their contents)
        if rule.dir_only and not (os.path.isdir(path) or rel.startswith(rule.pattern.rstrip("/") + "/")):
            continue
        ignored = not rule.negated
    return ignored

# --------------------------------------------------------------------------- #
# Module outline extraction
# --------------------------------------------------------------------------- #

def detect_lang(fname: str) -> Optional[Tuple[str, set]]:
    ext = os.path.splitext(fname)[1].lower()
    return LANG_RULES.get(ext)

def module_outline(path: str, max_lines: int = 8) -> List[str]:
    """Return up to `max_lines` one-liners describing top-level declarations."""
    lang = detect_lang(path)
    if not lang:
        return []
    comment, keywords = lang
    lines: List[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if len(lines) >= max_lines:
                    break
                s = raw.rstrip("\n").strip()
                if not s or s.startswith(comment):
                    continue
                # skip multiline-block comment starts for /* */ langs crudely
                if s.startswith("/*") or s.startswith("*"):
                    continue
                for kw in keywords:
                    if s.startswith(kw):
                        lines.append(s[:90])
                        break
    except OSError:
        pass
    return lines

# --------------------------------------------------------------------------- #
# Tree builder
# --------------------------------------------------------------------------- #

@dataclass
class Node:
    name: str
    is_dir: bool = False
    outline: List[str] = field(default_factory=list)
    children: List["Node"] = field(default_factory=list)

def _walk(root: str, rules: List[GitignoreRule], max_files: int, files: List[str],
          _visited: Optional[set] = None) -> None:
    """Collect file paths, respecting ignores, capped by max_files.
    Guards against symlink loops by tracking visited real paths.
    Merges nested .gitignore files as it descends (gitignore semantics:
    a subdirectory's .gitignore adds rules scoped to that directory)."""
    if len(files) >= max_files:
        return
    if _visited is None:
        _visited = set()
    try:
        real = os.path.realpath(root)
        if real in _visited:
            return  # symlink loop
        _visited.add(real)
        entries = sorted(os.listdir(root))
    except OSError:
        return
    # merge a nested .gitignore if present (rules scoped to this dir)
    nested_gi = os.path.join(root, ".gitignore")
    if os.path.isfile(nested_gi):
        rules = rules + parse_gitignore(nested_gi)
    for e in entries:
        if e.startswith(".") and e not in (".gitignore", ".env.example"):
            continue
        full = os.path.join(root, e)
        if is_ignored(full, rules):
            continue
        if os.path.isdir(full):
            _walk(full, rules, max_files, files, _visited)
        elif os.path.isfile(full):
            if len(files) >= max_files:
                return
            files.append(full)

def build_tree(files: List[str], root: str, want_outline: bool) -> Node:
    tree = Node(root or ".", is_dir=True)
    for path in files:
        rel = os.path.relpath(path, root) if root else path
        parts = rel.split(os.sep)
        cur = tree
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            child = next((c for c in cur.children if c.name == part), None)
            if child is None:
                child = Node(part, is_dir=not is_last)
                cur.children.append(child)
            cur = child
            if is_last and want_outline:
                cur.outline = module_outline(path)
    return tree

# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_tree(node: Node, prefix: str = "", is_root: bool = True) -> List[str]:
    out: List[str] = []
    items = node.children
    for i, child in enumerate(items):
        last = (i == len(items) - 1)
        connector = "└── " if last else "├── "
        if is_root and i == 0:
            connector = "    "  # root itself is the base
        label = child.name
        if child.is_dir:
            label += "/"
            out.append(f"{prefix}{connector}{label}")
            out.extend(render_tree(child, prefix + ("    " if last else "│   "), False))
        else:
            out.append(f"{prefix}{connector}{label}")
            for o in child.outline:
                out.append(f"{prefix}    {o}")
    return out

def entry_points(files: List[str]) -> List[str]:
    """Pick the highest-signal entry points, not every README/__init__."""
    eps = [f for f in files if os.path.basename(f) in ENTRY_HINTS]
    # Dedupe: a nested README/__init__.py is usually noise; keep root-level
    # manifests/entry files plus the top entry script. Cap for readability.
    ranked: List[Tuple[int, str]] = []
    for f in eps:
        name = os.path.basename(f)
        depth = f.count(os.sep)
        score = 0
        if name in {"README.md", "package.json", "pyproject.toml", "go.mod", "Cargo.toml"}:
            score += 100 - depth * 10  # prefer shallow
        if name in {"main.py", "app.py", "cli.py", "index.js", "index.ts", "main.go", "main.rs", "manage.py", "server.js", "server.ts"}:
            score += 60
        if name == "__init__.py":
            score = 10 - depth  # almost never a real entry point
        if name in {"Makefile", "Dockerfile"}:
            score += 40
        ranked.append((score, f))
    ranked.sort(key=lambda t: -t[0])
    return [f for _, f in ranked[:15]]

def build_map(root: str, want_outline: bool, max_files: int) -> dict:
    root = os.path.abspath(root)
    gi = os.path.join(root, ".gitignore")
    rules = parse_gitignore(gi) if os.path.isfile(gi) else []
    files: List[str] = []
    _walk(root, rules, max_files, files)
    tree = build_tree(files, root, want_outline)
    return {
        "root": root,
        "file_count": len(files),
        "entry_points": entry_points(files),
        "tree": tree,
    }

# --------------------------------------------------------------------------- #
# Framework-aware intelligence (--framework)
# --------------------------------------------------------------------------- #

# Detect the web/app framework from manifest files, then surface its structure:
# entry points, routes, models, config, conventions. This is the "framework
# level" understanding jcodemunch users asked for (#201) — zero-dep, in-file.
FRAMEWORK_MANIFESTS = {
    "package.json": "node",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "go.mod": "go",
    "composer.json": "php",
    "Cargo.toml": "rust",
    "Gemfile": "ruby",
    "pom.xml": "java",
    "build.gradle": "java",
}

def detect_framework(root: str) -> Optional[str]:
    """Detect the primary framework from manifest files + directory conventions."""
    # 1. manifest-based detection
    for manifest, lang in FRAMEWORK_MANIFESTS.items():
        path = os.path.join(root, manifest)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if manifest == "package.json":
            if '"next"' in text or '"next/' in text:
                return "Next.js"
            if '"nuxt' in text:
                return "Nuxt"
            if '"react' in text or '"react-dom' in text:
                return "React"
            if '"vue' in text:
                return "Vue"
            if '"express' in text:
                return "Express"
            if '"fastify' in text:
                return "Fastify"
            if '"svelte' in text:
                return "Svelte"
            return "Node.js"
        if manifest == "pyproject.toml":
            if "fastapi" in text:
                return "FastAPI"
            if "django" in text:
                return "Django"
            if "flask" in text:
                return "Flask"
            if "starlette" in text:
                return "Starlette"
            return "Python"
        if manifest == "composer.json":
            if "laravel" in text:
                return "Laravel"
            if "symfony" in text:
                return "Symfony"
            return "PHP"
        if manifest == "go.mod":
            return "Go"
        if manifest == "Cargo.toml":
            return "Rust"
        if manifest == "Gemfile":
            return "Ruby on Rails" if "rails" in text else "Ruby"
        if manifest == "pom.xml" or manifest == "build.gradle":
            return "Java"
        return lang
    # 2. directory-convention fallback
    if os.path.isdir(os.path.join(root, "app")) and os.path.isdir(os.path.join(root, "routes")):
        return "Laravel"
    if os.path.isdir(os.path.join(root, "pages")) and os.path.isdir(os.path.join(root, "components")):
        return "Next.js"
    if os.path.isdir(os.path.join(root, "src")) and os.path.isfile(os.path.join(root, "manage.py")):
        return "Django"
    return None

def _find_framework_files(root: str, patterns: List[str], max_files: int) -> List[str]:
    """Find files matching glob patterns (routes, models, config, etc.)."""
    import fnmatch
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # skip heavy dirs
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build")]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if any(fnmatch.fnmatch(rel, p) for p in patterns):
                found.append(rel)
                if len(found) >= max_files:
                    return found
    return found

def render_framework(root: str, max_files: int) -> str:
    """Emit a framework-aware structural overview: framework, entry points,
    routes, models, config, conventions."""
    framework = detect_framework(root)
    buf = io.StringIO()
    buf.write(f"# codeloom --framework\n")
    buf.write(f"framework: {framework or 'unknown'}\n\n")

    # entry points
    files: List[str] = []
    gi = os.path.join(root, ".gitignore")
    rules = parse_gitignore(gi) if os.path.isfile(gi) else []
    _walk(root, rules, max_files, files)
    eps = entry_points(files)
    if eps:
        buf.write("## Entry points\n")
        for e in eps[:10]:
            buf.write(f"  {e}\n")
        buf.write("\n")

    # framework-specific structure
    if framework in ("Next.js", "Nuxt", "Vue", "React", "Svelte"):
        pages = _find_framework_files(root, ["pages/**", "app/**", "components/**", "routes/**"], max_files)
        if pages:
            buf.write("## Routes / pages / components\n")
            for p in pages[:20]:
                buf.write(f"  {p}\n")
            buf.write("\n")
    elif framework in ("FastAPI", "Flask", "Starlette", "Django"):
        routes = _find_framework_files(root, ["**/routes*.py", "**/urls*.py", "**/views*.py", "**/api*.py", "**/models*.py", "**/schemas*.py"], max_files)
        if routes:
            buf.write("## Routes / views / models\n")
            for r in routes[:20]:
                buf.write(f"  {r}\n")
            buf.write("\n")
    elif framework in ("Laravel", "Symfony"):
        routes = _find_framework_files(root, ["routes/**", "app/Http/Controllers/**", "app/Models/**", "config/**"], max_files)
        if routes:
            buf.write("## Routes / controllers / models / config\n")
            for r in routes[:20]:
                buf.write(f"  {r}\n")
            buf.write("\n")
    elif framework in ("Express", "Fastify"):
        routes = _find_framework_files(root, ["**/routes*.js", "**/routes*.ts", "**/controllers/**", "**/models/**"], max_files)
        if routes:
            buf.write("## Routes / controllers / models\n")
            for r in routes[:20]:
                buf.write(f"  {r}\n")
            buf.write("\n")

    # config files
    configs = _find_framework_files(root, [".env*", "config/**", "*.config.js", "*.config.ts", "tsconfig.json", "next.config.*", "nuxt.config.*", "vite.config.*", "webpack.config.*"], max_files)
    if configs:
        buf.write("## Config\n")
        for c in configs[:15]:
            buf.write(f"  {c}\n")
        buf.write("\n")

    buf.write("## Conventions\n")
    if framework in ("Next.js", "Nuxt"):
        buf.write("  File-based routing: pages/ or app/ dir maps to URLs\n")
    elif framework == "Django":
        buf.write("  urls.py -> views.py -> models.py; settings in settings.py\n")
    elif framework == "FastAPI":
        buf.write("  routes defined via @app.get/@app.post decorators; Pydantic schemas\n")
    elif framework == "Laravel":
        buf.write("  routes/web.php + routes/api.php; Eloquent models in app/Models\n")
    elif framework == "Express":
        buf.write("  app.use() middleware chain; routes in routes/ or inline\n")
    else:
        buf.write("  (no framework-specific conventions detected)\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Call-graph intelligence (Python stdlib `ast`, zero deps)
# --------------------------------------------------------------------------- #

def module_name_of(path: str, root: str) -> str:
    """Map a file path to its dotted module name, e.g. src/core/engine.py -> src.core.engine.
    Strips known source extensions so `util.js` -> `util`."""
    rel = os.path.relpath(path, root)
    ext = os.path.splitext(rel)[1].lower()
    if ext in LANG_RULES or ext in IMPORT_LANG_RULES:
        rel = rel[:-(len(ext))]
    elif rel.endswith(os.sep + "__init__"):
        rel = rel[:-(len(os.sep) + 9)]
    if rel.endswith("__init__"):
        rel = rel[:-9]
    return rel.replace(os.sep, ".")

def _resolve_import(target: str, importer_mod: str, root: str, module_map: dict) -> Optional[str]:
    """Resolve an imported module name to an existing local module, or None.
    Handles absolute, relative (from .x / from ..x), namespace packages, and the
    common case where the import is relative to the source root (e.g. 'core.engine'
    -> 'src.core.engine').
    Strategy:
      1. exact match
      2. importer-relative: resolve against the importer's package prefix
      3. drop trailing segments (pkg.module -> pkg)
      4. suffix match against known modules (core.engine matches src.core.engine)
      5. workspace-root match: try each detected workspace root as a base
    """
    if target.startswith("."):
        # relative import — handled by caller via parse_module; skip here
        return None

    # 1. exact match
    if target in module_map:
        return target

    # 2. importer-relative: if importer is src.core.engine, try src.core.<target>
    if importer_mod and "." in importer_mod:
        importer_pkg = ".".join(importer_mod.split(".")[:-1])
        while importer_pkg:
            cand = f"{importer_pkg}.{target}"
            if cand in module_map:
                return cand
            importer_pkg = ".".join(importer_pkg.split(".")[:-1])

    # 3. drop trailing segments (pkg.module -> pkg)
    parts = target.split(".")
    for i in range(len(parts) - 1, 0, -1):
        cand = ".".join(parts[:i])
        if cand in module_map:
            return cand

    # 4. suffix match: does any local module end with '.target' (or equal it)?
    tgt_segs = target.split(".")
    best = None
    for mod in module_map:
        msegs = mod.split(".")
        if len(msegs) >= len(tgt_segs) and msegs[-len(tgt_segs):] == tgt_segs:
            # prefer shallowest (fewest segments above the match)
            if best is None or len(msegs) < len(best.split(".")):
                best = mod
    if best:
        return best

    # 5. workspace-root match: try each detected workspace root as a base.
    # Handles multi-root workspaces (monorepos) where imports resolve against
    # a package root (pyproject.toml / package.json / go.mod) rather than the
    # repo root. e.g. 'packages/foo/src/bar' imports 'baz' -> 'packages/foo/src/baz'.
    for ws in _workspace_roots(root):
        cand = f"{ws}.{target}"
        if cand in module_map:
            return cand
    return None


def _workspace_roots(root: str) -> List[str]:
    """Detect workspace/package roots from package metadata files. Returns
    dotted module prefixes (e.g. 'packages.foo.src') that imports may resolve
    against. Cached per root."""
    if not hasattr(_workspace_roots, "_cache"):
        _workspace_roots._cache = {}
    if root in _workspace_roots._cache:
        return _workspace_roots._cache[root]
    roots: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirpath or ".venv" in dirpath or "node_modules" in dirpath:
            continue
        # a package root is a dir with a manifest and a src/ or package dir
        has_manifest = any(f in filenames for f in ("pyproject.toml", "package.json", "go.mod"))
        if not has_manifest:
            continue
        # the package's source root is <dir>/src (Python) or <dir> (JS/Go)
        src_dir = os.path.join(dirpath, "src")
        if os.path.isdir(src_dir):
            rel = os.path.relpath(src_dir, root)
            roots.append(rel.replace(os.sep, "."))
        else:
            rel = os.path.relpath(dirpath, root)
            if rel != ".":
                roots.append(rel.replace(os.sep, "."))
    _workspace_roots._cache[root] = roots
    return roots

def parse_module(path: str, root: str, module_map: dict) -> dict:
    """Return {defs, imports} for a single Python file."""
    mod = module_name_of(path, root)
    info = {"mod": mod, "defs": [], "imports": []}  # imports = resolved local module names
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except (SyntaxError, OSError):
        return info
    package = ".".join(mod.split(".")[:-1]) if "." in mod else ""

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # top-level only for the outline (walk already flattens, so dedupe by lineno depth)
            info["defs"].append(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                info["imports"].append(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import
                level = node.level
                base_parts = package.split(".") if package else []
                if level > 1:
                    base_parts = base_parts[:-(level - 1)] if len(base_parts) >= level - 1 else []
                target = ".".join(base_parts + ([base] if base else []))
            else:
                target = base
            info["imports"].append(target)
    return info

def build_graph(files: List[str], root: str) -> dict:
    """Build import dependency graph: {module: set(local_module_deps)}."""
    module_map = {}
    for f in files:
        if f.endswith(".py"):
            module_map[module_name_of(f, root)] = f

    graph: dict = {}
    for f in files:
        if not f.endswith(".py"):
            continue
        mod = module_name_of(f, root)
        info = parse_module(f, root, module_map)
        deps: set = set()
        for imp in info["imports"]:
            resolved = _resolve_import(imp, mod, root, module_map)
            if resolved and resolved != mod:
                deps.add(resolved)
        graph[mod] = deps
    return graph

# regex-based import detection for non-Python languages (best-effort, zero-dep)
IMPORT_LANG_RULES: dict = {
    ".js":   (r"^\s*(?:import\s+.*?from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))", r"^import\s+['\"]([^'\"]+)['\"]"),
    ".ts":   (r"^\s*(?:import\s+.*?from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))", r"^import\s+['\"]([^'\"]+)['\"]"),
    ".jsx":  (r"^\s*(?:import\s+.*?from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))", r"^import\s+['\"]([^'\"]+)['\"]"),
    ".tsx":  (r"^\s*(?:import\s+.*?from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))", r"^import\s+['\"]([^'\"]+)['\"]"),
    ".go":   (r"^\s*import\s+[\"']([^\"']+)[\"']", r"^\s*import\s+[\"']([^\"']+)[\"']"),
    ".rs":   (r"^\s*(?:use|mod)\s+([\w:]+)", r"^\s*use\s+([\w:]+)"),
    ".java": (r"^\s*import\s+([\w.]+)", r"^\s*import\s+([\w.]+)"),
    ".c":    (r"^\s*#include\s*[<\"]([^>\"]+)[>\"]", r"^\s*#include\s*[<\"]([^>\"]+)[>\"]"),
    ".h":    (r"^\s*#include\s*[<\"]([^>\"]+)[>\"]", r"^\s*#include\s*[<\"]([^>\"]+)[>\"]"),
    ".cpp":  (r"^\s*#include\s*[<\"]([^>\"]+)[>\"]", r"^\s*#include\s*[<\"]([^>\"]+)[>\"]"),
    ".hpp":  (r"^\s*#include\s*[<\"]([^>\"]+)[>\"]", r"^\s*#include\s*[<\"]([^>\"]+)[>\"]"),
    ".cs":   (r"^\s*using\s+([\w.]+)", r"^\s*using\s+([\w.]+)"),
    ".rb":   (r"^\s*require\s+['\"]([^'\"]+)['\"]", r"^\s*require\s+['\"]([^'\"]+)['\"]"),
    ".php":  (r"^\s*(?:use\s+([\w\\]+)|require\w*\s*\(?['\"]([^'\"]+)['\"]\))", r"^\s*use\s+([\w\\]+)"),
    ".swift":(r"^\s*import\s+([\w.]+)", r"^\s*import\s+([\w.]+)"),
    ".kt":   (r"^\s*import\s+([\w.]+)", r"^\s*import\s+([\w.]+)"),
    ".dart": (r"^\s*import\s+['\"]([^'\"]+)['\"]", r"^\s*import\s+['\"]([^'\"]+)['\"]"),
    ".lua":  (r"^\s*require\s*\(?['\"]([^'\"]+)['\"]\)?", r"^\s*require\s*\(?['\"]([^'\"]+)['\"]\)?"),
}

def build_graph_multi(files: List[str], root: str) -> dict:
    """Build a cross-language import dependency graph: {module: set(deps)}.
    Python uses precise `ast`; other languages use best-effort regex. Deps are
    resolved to local modules via suffix matching."""
    # collect all local module names (any language)
    module_map = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in LANG_RULES or ext in IMPORT_LANG_RULES:
            module_map[module_name_of(f, root)] = f

    graph: dict = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext == ".py":
            mod = module_name_of(f, root)
            info = parse_module(f, root, module_map)
            deps = set()
            for imp in info["imports"]:
                resolved = _resolve_import(imp, mod, root, module_map)
                if resolved and resolved != mod:
                    deps.add(resolved)
            graph[mod] = deps
        elif ext in IMPORT_LANG_RULES:
            mod = module_name_of(f, root)
            deps = set()
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            def_re, _ = IMPORT_LANG_RULES[ext]
            for m in re.finditer(def_re, text, re.MULTILINE):
                target = next((g for g in m.groups() if g), None)
                if not target:
                    continue
                # strip quotes/angle brackets and resolve to local module
                target = target.strip("'\"<>")
                # strip relative path prefixes (./, ../) for JS/TS/Dart
                while target.startswith("./") or target.startswith("../"):
                    target = target[2:] if target.startswith("./") else target[3:]
                # for C includes, strip .h extension
                if ext in (".c", ".h", ".cpp", ".hpp"):
                    target = target.rsplit(".", 1)[0] if "." in target else target
                resolved = _resolve_import(target, mod, root, module_map)
                if resolved and resolved != mod:
                    deps.add(resolved)
            graph[mod] = deps
    return graph

def render_graph_multi(graph: dict, root: str, start: Optional[str] = None) -> str:
    buf = io.StringIO()
    if start:
        fs = focus_subgraph(graph, start)
        buf.write(f"# focus: {start}\n")
        buf.write(f"## depends_on ({len(fs['depends_on'])})\n")
        for d in fs["depends_on"]:
            buf.write(f"  {d}\n")
        buf.write(f"## depended_on_by ({len(fs['depended_on_by'])})\n")
        for d in fs["depended_on_by"]:
            buf.write(f"  {d}\n")
        return buf.getvalue()
    buf.write("# import graph (multi-language)\n")
    edges = [(m, d) for m, deps in sorted(graph.items()) for d in sorted(deps)]
    buf.write(f"{len(graph)} modules, {len(edges)} edges\n\n")
    for m, d in edges:
        buf.write(f"  {m} -> {d}\n")
    return buf.getvalue()

def reachable(graph: dict, start: str, direction: str = "out") -> set:
    """BFS over the graph. direction='out' = what start depends on; 'in' = what depends on start."""
    seen: set = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        nxt = graph[cur] if direction == "out" else _dependents(graph, cur)
        for n in nxt:
            if n not in seen:
                stack.append(n)
    seen.discard(start)
    return seen

def _dependents(graph: dict, mod: str) -> list:
    return [m for m, deps in graph.items() if mod in deps]

def focus_subgraph(graph: dict, start: str) -> dict:
    """Deps (out) + dependents (in) of a module, for agent focus."""
    return {
        "module": start,
        "depends_on": sorted(reachable(graph, start, "out")),
        "depended_on_by": sorted(reachable(graph, start, "in")),
    }

def render_graph(graph: dict, root: str, start: Optional[str] = None) -> str:
    buf = io.StringIO()
    if start:
        fs = focus_subgraph(graph, start)
        buf.write(f"# focus: {start}\n")
        buf.write(f"## depends_on ({len(fs['depends_on'])})\n")
        for d in fs["depends_on"]:
            buf.write(f"  {d}\n")
        buf.write(f"## depended_on_by ({len(fs['depended_on_by'])})\n")
        for d in fs["depended_on_by"]:
            buf.write(f"  {d}\n")
        buf.write("\n## import edges touching this module\n")
        for mod, deps in sorted(graph.items()):
            for d in sorted(deps):
                if mod == start or d == start:
                    buf.write(f"  {mod} -> {d}\n")
        return buf.getvalue()
    # full graph
    buf.write("# import graph\n")
    edges = [(m, d) for m, deps in sorted(graph.items()) for d in sorted(deps)]
    buf.write(f"{len(graph)} modules, {len(edges)} edges\n\n")
    for m, d in edges:
        buf.write(f"  {m} -> {d}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Function-level call graph (Python `ast`)
# --------------------------------------------------------------------------- #

def build_call_graph(files: List[str], root: str) -> dict:
    """Build function-level call graph: {module: {func: set(called_funcs)}}.
    Only reports calls to functions DEFINED within the codebase (across all
    modules) — builtins and stdlib calls are noise for structural understanding.
    Best-effort static analysis via `ast`."""
    # First pass: collect all defined functions per module.
    defined: dict = {}  # module -> set of function names
    for f in files:
        if not f.endswith(".py"):
            continue
        mod = module_name_of(f, root)
        defined[mod] = set()
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined[mod].add(node.name)

    # All function names defined anywhere in the codebase.
    all_defined: set = set()
    for s in defined.values():
        all_defined |= s

    # Second pass: find calls, keeping only callees defined in the codebase.
    calls: dict = {}  # module -> {caller: set(callee)}
    for f in files:
        if not f.endswith(".py"):
            continue
        mod = module_name_of(f, root)
        calls[mod] = {}
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                caller = node.name
                callees = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        if isinstance(fn, ast.Name) and fn.id in all_defined:
                            callees.add(fn.id)
                        elif isinstance(fn, ast.Attribute) and fn.attr in all_defined:
                            callees.add(fn.attr)
                if callees:
                    calls[mod][caller] = callees
    return calls

def render_calls(calls: dict, root: str, start: Optional[str] = None) -> str:
    buf = io.StringIO()
    if start:
        # focus: show calls in one module
        buf.write(f"# function calls in {start}\n")
        for caller, callees in sorted(calls.get(start, {}).items()):
            if callees:
                buf.write(f"  {caller}() -> {', '.join(sorted(callees))}\n")
        return buf.getvalue()
    buf.write("# function call graph\n")
    total = sum(len(c) for c in calls.values())
    buf.write(f"{len(calls)} modules, {total} callers\n\n")
    for mod, funcs in sorted(calls.items()):
        for caller, callees in sorted(funcs.items()):
            if callees:
                buf.write(f"  {mod}.{caller}() -> {', '.join(sorted(callees))}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Cross-file call graph (resolve A.main() -> B.engine.run() across modules)
# --------------------------------------------------------------------------- #

def build_cross_call_graph(files: List[str], root: str) -> dict:
    """Build a cross-file call graph: {module: {func: set(qualified_callees)}}.
    Resolves calls to their defining module, so `A.main()` calling `engine.run()`
    (imported from B) yields `A.main -> B.engine.run`. Uses Python `ast` for
    precise resolution; falls back to regex for non-Python files."""
    # Pass 1: collect defined symbols per module (funcs + classes + methods).
    symbols: dict = {}  # module -> {name: set(qualified_names)}
    for f in files:
        if not f.endswith(".py"):
            continue
        mod = module_name_of(f, root)
        symbols[mod] = {}
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols[mod].setdefault(node.name, set()).add(f"{mod}.{node.name}")
            elif isinstance(node, ast.ClassDef):
                symbols[mod].setdefault(node.name, set()).add(f"{mod}.{node.name}")
                # methods
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols[mod].setdefault(sub.name, set()).add(f"{mod}.{node.name}.{sub.name}")

    # Pass 2: per-module import map: local_name -> (module, imported_name)
    # Resolve import targets to actual module paths (suffix match) so
    # `from utils.retry import retry` maps to `src.utils.retry.retry`.
    module_names = set(symbols.keys())
    def _resolve_mod(target: str) -> str:
        """Resolve a dotted import target to an actual module name."""
        if target in module_names:
            return target
        tsegs = target.split(".")
        best = None
        for mod in module_names:
            msegs = mod.split(".")
            if len(msegs) >= len(tsegs) and msegs[-len(tsegs):] == tsegs:
                if best is None or len(msegs) < len(best.split(".")):
                    best = mod
        return best or target

    import_maps: dict = {}  # module -> {local_name: qualified_target}
    for f in files:
        if not f.endswith(".py"):
            continue
        mod = module_name_of(f, root)
        import_maps[mod] = {}
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, OSError):
            continue
        package = ".".join(mod.split(".")[:-1]) if "." in mod else ""
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name.split(".")[0]
                    import_maps[mod][local] = _resolve_mod(a.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    level = node.level
                    base_parts = package.split(".") if package else []
                    if level > 1:
                        base_parts = base_parts[:-(level - 1)] if len(base_parts) >= level - 1 else []
                    target = ".".join(base_parts + ([base] if base else []))
                else:
                    target = base
                resolved_base = _resolve_mod(target)
                for a in node.names:
                    local = a.asname or a.name
                    import_maps[mod][local] = f"{resolved_base}.{a.name}" if resolved_base else a.name

    # Pass 3: find calls, resolve to qualified callee, keep only codebase-defined.
    # Build the set of all qualified names defined in the codebase.
    all_defined_q: set = set()
    for mod_syms in symbols.values():
        for qnames in mod_syms.values():
            all_defined_q |= qnames

    calls: dict = {}  # module -> {caller: set(qualified_callee)}
    for f in files:
        if not f.endswith(".py"):
            continue
        mod = module_name_of(f, root)
        calls[mod] = {}
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            caller = node.name
            callees = set()
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                if isinstance(fn, ast.Name):
                    # local function call
                    if fn.id in symbols.get(mod, {}):
                        callees |= symbols[mod][fn.id]
                    # imported name
                    elif fn.id in import_maps.get(mod, {}):
                        target = import_maps[mod][fn.id]
                        callees.add(target)
                elif isinstance(fn, ast.Attribute):
                    # obj.method() — resolve obj to its module
                    obj = fn.value
                    if isinstance(obj, ast.Name) and obj.id in import_maps.get(mod, {}):
                        base = import_maps[mod][obj.id]
                        callees.add(f"{base}.{fn.attr}")
                    else:
                        # method on a local class instance — best-effort
                        if fn.attr in symbols.get(mod, {}):
                            callees |= symbols[mod][fn.attr]
            # filter to codebase-defined symbols only
            callees = {c for c in callees if c in all_defined_q}
            if callees:
                calls[mod][caller] = callees
    return calls

def render_cross_calls(calls: dict, root: str, start: Optional[str] = None) -> str:
    buf = io.StringIO()
    if start:
        buf.write(f"# cross-file calls in {start}\n")
        for caller, callees in sorted(calls.get(start, {}).items()):
            if callees:
                buf.write(f"  {caller}() -> {', '.join(sorted(callees))}\n")
        return buf.getvalue()
    buf.write("# cross-file call graph\n")
    total = sum(len(c) for c in calls.values())
    buf.write(f"{len(calls)} modules, {total} callers\n\n")
    for mod, funcs in sorted(calls.items()):
        for caller, callees in sorted(funcs.items()):
            if callees:
                buf.write(f"  {mod}.{caller}() -> {', '.join(sorted(callees))}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Symbol index + --search (inverted index of all symbols)
# --------------------------------------------------------------------------- #

def build_symbol_index(files: List[str], root: str) -> dict:
    """Build an inverted index: symbol_name -> list of {module, kind, line, snippet}.
    Indexes functions, classes, and methods across Python (via `ast`) and other
    languages (via regex). Each entry carries a context snippet so the agent
    doesn't have to open the file to see what the symbol does."""
    index: dict = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        mod = module_name_of(f, root)
        if ext == ".py":
            _index_python(f, mod, index)
        elif ext in CALL_LANG_RULES:
            _index_regex(f, mod, ext, index)
    return index

def _read_snippet(path: str, line: int, context: int = 1) -> str:
    """Read a few lines around `line` for context."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        start = max(0, line - 1 - context)
        end = min(len(lines), line + context)
        return "".join(lines[start:end]).strip()
    except OSError:
        return ""

def _index_python(path: str, mod: str, index: dict) -> None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (SyntaxError, OSError):
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            index.setdefault(node.name, []).append(
                {"module": mod, "kind": "function", "line": node.lineno,
                 "snippet": _read_snippet(path, node.lineno)})
        elif isinstance(node, ast.ClassDef):
            index.setdefault(node.name, []).append(
                {"module": mod, "kind": "class", "line": node.lineno,
                 "snippet": _read_snippet(path, node.lineno)})
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    index.setdefault(sub.name, []).append(
                        {"module": mod, "kind": "method", "line": sub.lineno,
                         "class": node.name, "snippet": _read_snippet(path, sub.lineno)})

def _index_regex(path: str, mod: str, ext: str, index: dict) -> None:
    """Index non-Python symbols via regex (best-effort)."""
    if ext not in CALL_LANG_RULES:
        return
    def_re, _ = CALL_LANG_RULES[ext]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(def_re, line)
        if m:
            name = next((g for g in m.groups() if g), None)
            if name:
                index.setdefault(name, []).append(
                    {"module": mod, "kind": "function", "line": i,
                     "snippet": line.strip()})

def search_symbols(index: dict, query: str, limit: int = 20) -> List[dict]:
    """Search the symbol index. Exact match first, then prefix, then substring.
    Returns definitions with context snippets."""
    q = query.lower()
    exact, prefix, sub = [], [], []
    for name, locs in index.items():
        nl = name.lower()
        if nl == q:
            exact.append((name, locs))
        elif nl.startswith(q):
            prefix.append((name, locs))
        elif q in nl:
            sub.append((name, locs))
    ranked = exact + prefix + sub
    out = []
    for name, locs in ranked:
        for loc in locs:
            out.append({"name": name, **loc})
            if len(out) >= limit:
                return out
    return out

def find_usages(files: List[str], root: str, symbol: str, limit: int = 20) -> List[dict]:
    """Find where a symbol is USED (not just defined), across Python files.
    Uses `ast` to find Name/Attribute references to the symbol."""
    usages = []
    for f in files:
        if not f.endswith(".py"):
            continue
        mod = module_name_of(f, root)
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            # skip the definition itself
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
               and node.name == symbol:
                continue
            if isinstance(node, ast.Name) and node.id == symbol:
                usages.append({"module": mod, "line": node.lineno,
                               "snippet": _read_snippet(f, node.lineno)})
            elif isinstance(node, ast.Attribute) and node.attr == symbol:
                usages.append({"module": mod, "line": node.lineno,
                               "snippet": _read_snippet(f, node.lineno)})
            if len(usages) >= limit:
                return usages
    return usages

def render_search(index: dict, query: str, limit: int = 20) -> str:
    results = search_symbols(index, query, limit)
    buf = io.StringIO()
    buf.write(f"# search: {query}\n")
    if not results:
        buf.write("No symbols found.\n")
        return buf.getvalue()
    buf.write(f"{len(results)} definition(s):\n\n")
    for r in results:
        cls = f" ({r['class']})" if r.get("class") else ""
        buf.write(f"  {r['name']}  [{r['kind']}{cls}]  {r['module']}:{r['line']}\n")
        if r.get("snippet"):
            buf.write(f"    {r['snippet']}\n")
    return buf.getvalue()

def render_usages(files: List[str], root: str, symbol: str, limit: int = 20) -> str:
    usages = find_usages(files, root, symbol, limit)
    buf = io.StringIO()
    buf.write(f"# usages: {symbol}\n")
    if not usages:
        buf.write("No usages found (only the definition).\n")
        return buf.getvalue()
    buf.write(f"{len(usages)} usage(s):\n\n")
    for u in usages:
        buf.write(f"  {u['module']}:{u['line']}\n")
        if u.get("snippet"):
            buf.write(f"    {u['snippet']}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Snippet-level code search (--grep): find exact code, ranked + context
# --------------------------------------------------------------------------- #

def grep_search(files: List[str], root: str, query: str, limit: int = 20) -> List[dict]:
    """Search file contents for a query string. Returns ranked matches with
    context lines. Ranking: exact-word matches > substring > case-insensitive.
    This is the 'find the exact snippet' capability (semble's core job)."""
    q = query.lower()
    # tokenize query for word-boundary ranking
    q_words = [w for w in re.findall(r"[a-zA-Z0-9_]+", q) if len(w) > 1]
    results = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in LANG_RULES and ext not in IMPORT_LANG_RULES:
            continue
        mod = module_name_of(f, root)
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            low = line.lower()
            if q not in low:
                continue
            # rank: exact word match > substring; more query words present = higher
            word_hits = sum(1 for w in q_words if w in low)
            exact = q in low
            score = word_hits * 10 + (5 if exact else 0)
            # context: 1 line before + the match + 1 after
            start = max(0, i - 1)
            end = min(len(lines), i + 2)
            snippet = "".join(lines[start:end]).rstrip()
            results.append({
                "module": mod, "path": f, "line": i + 1,
                "score": score, "snippet": snippet,
            })
    results.sort(key=lambda r: (-r["score"], r["module"], r["line"]))
    return results[:limit]

def render_grep(files: List[str], root: str, query: str, limit: int = 20) -> str:
    results = grep_search(files, root, query, limit)
    buf = io.StringIO()
    buf.write(f"# grep: {query}\n")
    if not results:
        buf.write("No matches found.\n")
        return buf.getvalue()
    buf.write(f"{len(results)} match(es):\n\n")
    for r in results:
        buf.write(f"  {r['module']}:{r['line']}\n")
        if r.get("snippet"):
            buf.write(f"    {r['snippet']}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --read: extract exact symbol source via AST (token-efficient retrieval)
# This is jcodemunch's core value — return the exact code the agent needs
# without reading the whole file — integrated with codeloom's task-orientation.
# --------------------------------------------------------------------------- #

def read_symbol(files: List[str], root: str, symbol: str) -> Optional[dict]:
    """Find a function/class/method by name and return its exact source.
    Python uses `ast`; other languages use tree-sitter (when a grammar is
    available) or a brace-matching fallback. Returns {module, kind, line,
    source} or None if not found."""
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        mod = module_name_of(f, root)
        if ext == ".py":
            result = _read_python(f, mod, symbol)
        elif ext in CALL_LANG_RULES:
            result = _read_other(f, mod, ext, symbol)
        else:
            continue
        if result:
            return result
    return None

def _read_python(path: str, mod: str, symbol: str) -> Optional[dict]:
    """Extract a Python symbol's source via `ast`."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        tree = ast.parse(text)
    except (SyntaxError, OSError):
        return None
    lines = text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
           and node.name == symbol:
            start = node.lineno - 1
            end = getattr(node, "end_lineno", node.lineno)
            source = "\n".join(lines[start:end])
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            return {"module": mod, "kind": kind, "line": node.lineno, "source": source}
    return None

def _read_other(path: str, mod: str, ext: str, symbol: str) -> Optional[dict]:
    """Extract a non-Python symbol's source. Uses tree-sitter when a grammar is
    available; otherwise a brace-matching fallback."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    lines = text.splitlines()

    # 1. tree-sitter fast-path (precise)
    ts_root = _ts_parse(path, ext)
    if ts_root is not None:
        # find the function/class node with the matching name
        def find(node):
            if node.type in ("function_definition", "function_declaration",
                             "method_definition", "class_declaration",
                             "struct_item", "impl_item", "func_declaration",
                             "method_declaration", "type_declaration"):
                for child in node.children:
                    if child.type in ("identifier", "name", "type_identifier",
                                      "field_identifier") and \
                       child.text.decode("utf-8", "replace") == symbol:
                        start = node.start_point[0]
                        end = node.end_point[0]
                        source = "\n".join(lines[start:end + 1])
                        kind = "class" if "class" in node.type or "struct" in node.type else "function"
                        return {"module": mod, "kind": kind, "line": start + 1, "source": source}
            for child in node.children:
                r = find(child)
                if r:
                    return r
            return None
        return find(ts_root)

    # 2. brace-matching fallback (best-effort)
    def_re, _ = CALL_LANG_RULES[ext]
    for i, line in enumerate(lines):
        m = re.match(def_re, line)
        if not m:
            continue
        name = next((g for g in m.groups() if g), None)
        if name != symbol:
            continue
        # find the opening brace and match it
        start = i
        depth = 0
        opened = False
        for j in range(i, len(lines)):
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                    opened = True
                elif ch == "}":
                    depth -= 1
            if opened and depth == 0:
                source = "\n".join(lines[start:j + 1])
                return {"module": mod, "kind": "function", "line": start + 1, "source": source}
        # no braces (single-line or no body) — return the def line
        return {"module": mod, "kind": "function", "line": start + 1, "source": line}
    return None

def render_read(files: List[str], root: str, symbol: str) -> str:
    result = read_symbol(files, root, symbol)
    buf = io.StringIO()
    buf.write(f"# read: {symbol}\n")
    if result is None:
        buf.write("Symbol not found.\n")
        return buf.getvalue()
    buf.write(f"{result['module']}:{result['line']}  [{result['kind']}]\n\n")
    buf.write(result["source"] + "\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Byte-offset symbol index + token-counted snippet API (token shaving)
# This is the "match jcodemunch's token-shaving retrieval" layer. Returns
# exact byte ranges + token estimates so agents request only what they need.
# --------------------------------------------------------------------------- #

def build_byte_index(files, root):
    """Build a symbol index with precise byte offsets + token estimates.
    Returns {symbol: [{module, kind, line, start_byte, end_byte, tokens,
    source}]}. Python uses ast (precise byte offsets); other languages use
    tree-sitter or brace-matching."""
    index = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        mod = module_name_of(f, root)
        if ext == ".py":
            _index_python_bytes(f, mod, index)
        elif ext in CALL_LANG_RULES:
            _index_other_bytes(f, mod, ext, index)
    return index

def _index_python_bytes(path, mod, index):
    """Index Python symbols with byte offsets via ast."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        tree = ast.parse(text)
    except (SyntaxError, OSError):
        return
    lines = text.splitlines(keepends=True)
    # build line -> byte offset map
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln.encode("utf-8")))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start_line = node.lineno - 1
            end_line = getattr(node, "end_lineno", node.lineno) - 1
            start_byte = offsets[start_line]
            end_byte = offsets[end_line] + len(lines[end_line].encode("utf-8"))
            source = "".join(lines[start_line:end_line + 1])
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            index.setdefault(node.name, []).append({
                "module": mod, "path": path, "kind": kind, "line": node.lineno,
                "start_byte": start_byte, "end_byte": end_byte,
                "tokens": estimate_tokens(source), "source": source,
            })

def _index_other_bytes(path, mod, ext, index):
    """Index non-Python symbols with byte offsets (tree-sitter or brace-match)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln.encode("utf-8")))
    # tree-sitter fast-path
    ts_root = _ts_parse(path, ext)
    if ts_root is not None:
        def walk(node):
            if node.type in ("function_definition", "function_declaration",
                             "method_definition", "class_declaration",
                             "struct_item", "impl_item", "func_declaration",
                             "method_declaration", "type_declaration"):
                for child in node.children:
                    if child.type in ("identifier", "name", "type_identifier",
                                      "field_identifier"):
                        name = child.text.decode("utf-8", "replace")
                        start_byte = node.start_byte
                        end_byte = node.end_byte
                        source = text[start_byte:end_byte]
                        kind = "class" if "class" in node.type or "struct" in node.type else "function"
                        index.setdefault(name, []).append({
                            "module": mod, "kind": kind, "line": node.start_point[0] + 1,
                            "start_byte": start_byte, "end_byte": end_byte,
                            "tokens": estimate_tokens(source), "source": source,
                        })
                        break
            for child in node.children:
                walk(child)
        walk(ts_root)
        return
    # brace-matching fallback
    import re as _re
    def_re, _ = CALL_LANG_RULES[ext]
    for i, line in enumerate(lines):
        m = _re.match(def_re, line)
        if not m:
            continue
        name = next((g for g in m.groups() if g), None)
        if not name:
            continue
        start_byte = offsets[i]
        depth = 0
        opened = False
        end_byte = offsets[i] + len(lines[i].encode("utf-8"))
        for j in range(i, len(lines)):
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                    opened = True
                elif ch == "}":
                    depth -= 1
            if opened and depth == 0:
                end_byte = offsets[j] + len(lines[j].encode("utf-8"))
                break
        source = text[start_byte:end_byte]
        index.setdefault(name, []).append({
            "module": mod, "kind": "function", "line": i + 1,
            "start_byte": start_byte, "end_byte": end_byte,
            "tokens": estimate_tokens(source), "source": source,
        })

def get_symbol(files, root, symbol, context_lines=2):
    """Return the smallest snippet needed to understand a symbol, with byte
    offsets + token estimate. context_lines adds surrounding lines."""
    index = build_byte_index(files, root)
    locs = index.get(symbol)
    if not locs:
        return None
    loc = locs[0]
    if context_lines > 0:
        # expand the byte range to include surrounding lines
        try:
            with open(loc["path"], "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            lines = text.splitlines(keepends=True)
            offsets = [0]
            for ln in lines:
                offsets.append(offsets[-1] + len(ln.encode("utf-8")))
            # find the line index of start_byte
            start_line = 0
            for i, off in enumerate(offsets):
                if off <= loc["start_byte"]:
                    start_line = i
            end_line = start_line
            for i, off in enumerate(offsets):
                if off <= loc["end_byte"]:
                    end_line = i
            new_start = max(0, start_line - context_lines)
            new_end = min(len(lines), end_line + context_lines + 1)
            loc["start_byte"] = offsets[new_start]
            loc["end_byte"] = offsets[new_end - 1] + len(lines[new_end - 1].encode("utf-8"))
            loc["source"] = "".join(lines[new_start:new_end])
            loc["tokens"] = estimate_tokens(loc["source"])
        except OSError:
            pass
    return loc

def get_snippet_by_offset(path, start_byte, end_byte):
    """Extract a byte-range snippet from a file. Returns {text, tokens, bytes}."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    snippet = text[start_byte:end_byte]
    return {"text": snippet, "tokens": estimate_tokens(snippet), "bytes": len(snippet.encode("utf-8"))}

def render_get_symbol(files, root, symbol, context_lines=2, summary=False, adaptive=False):
    loc = get_symbol(files, root, symbol, context_lines)
    buf = io.StringIO()
    buf.write(f"# get_symbol: {symbol}\n")
    if loc is None:
        buf.write("Symbol not found.\n")
        return buf.getvalue()
    # adaptive: return the actual implementation when it's cheap (small symbol),
    # summary only for large symbols. Strictly better than jcodemunch — you get
    # the implementation immediately for small symbols (like jcodemunch) PLUS
    # call context, and you don't get a token dump for huge symbols.
    if adaptive and loc.get("tokens", 0) <= ADAPTIVE_FULL_THRESHOLD:
        summary = False
    if summary:
        # summary-first: signature + docstring + call graph, NOT full source.
        # This is the 95%+ token-savings lever — huge symbols return a tiny
        # summary instead of the whole class/function.
        sig = _signature_shape(loc["source"])
        sig_str = sig[0] if sig else symbol
        # extract docstring
        import re as _re
        doc = _re.search(r'["\']{3}(.*?)["\']{3}', loc["source"], _re.DOTALL)
        doc_str = doc.group(1).strip().split("\n")[0][:120] if doc else "(no docstring)"
        # call graph: what it calls + what calls it
        calls = build_call_graph_multi(files, root)
        callees = set()
        for caller, cs in calls.get(loc["module"], {}).items():
            if caller == symbol:
                callees |= cs
        called_by = set()
        for cm, funcs in calls.items():
            for caller, cs in funcs.items():
                if symbol in cs:
                    called_by.add(f"{cm}.{caller}")
        summary_text = (
            f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
            f"~{estimate_tokens(sig_str + doc_str)} tokens (summary)\n\n"
            f"Signature: {sig_str}\n"
            f"Docstring: {doc_str}\n"
            f"Calls ({len(callees)}): {', '.join(sorted(callees)) or 'none'}\n"
            f"Called by ({len(called_by)}): {', '.join(sorted(called_by)) or 'none'}\n"
            f"\nUse `--get-symbol {symbol} --full` for the full source.\n"
        )
        buf.write(summary_text)
        return buf.getvalue()
    buf.write(f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
              f"bytes {loc['start_byte']}-{loc['end_byte']}  ~{loc['tokens']} tokens\n\n")
    buf.write(loc["source"] + "\n")
    # full-source PLUS call-graph context — beats jcodemunch's raw source,
    # which gives the implementation but not what it calls / what calls it.
    calls = build_call_graph_multi(files, root)
    callees = set()
    for caller, cs in calls.get(loc["module"], {}).items():
        if caller == symbol:
            callees |= cs
    called_by = set()
    for cm, funcs in calls.items():
        for caller, cs in funcs.items():
            if symbol in cs:
                called_by.add(f"{cm}.{caller}")
    if callees or called_by:
        buf.write(f"\n# call context\n")
        if callees:
            buf.write(f"# calls: {', '.join(sorted(callees))}\n")
        if called_by:
            buf.write(f"# called by: {', '.join(sorted(called_by))}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --explain: plain-English explanation of a symbol (AST + call graph, no LLM)
# --------------------------------------------------------------------------- #

def explain_symbol(files, root, symbol):
    """Generate a plain-English explanation of a symbol's role using its AST
    signature + call graph. Template-based, no LLM needed."""
    result = read_symbol(files, root, symbol)
    if result is None:
        return None
    mod = result["module"]
    calls = build_call_graph_multi(files, root)
    callees = set()
    for caller, cs in calls.get(mod, {}).items():
        if caller == symbol:
            callees |= cs
    called_by = set()
    for caller_mod, funcs in calls.items():
        for caller, cs in funcs.items():
            if symbol in cs:
                called_by.add(f"{caller_mod}.{caller}")
    source = result["source"]
    kind = result["kind"]
    summary = ""
    lines = [l.strip() for l in source.splitlines() if l.strip()]
    if lines:
        import re as _re
        doc = _re.search(r'["\']{3}(.*?)["\']{3}', source, _re.DOTALL)
        if doc:
            summary = doc.group(1).strip().split("\n")[0][:120]
        else:
            summary = lines[0][:120]
    return {
        "module": mod, "kind": kind, "line": result["line"],
        "summary": summary, "callees": sorted(callees), "called_by": sorted(called_by),
    }

def render_explain(files, root, symbol):
    info = explain_symbol(files, root, symbol)
    buf = io.StringIO()
    buf.write(f"# explain: {symbol}\n")
    if info is None:
        buf.write("Symbol not found.\n")
        return buf.getvalue()
    buf.write(f"{info['module']}:{info['line']}  [{info['kind']}]\n\n")
    buf.write(f"Summary: {info['summary'] or '(no docstring)'}\n")
    buf.write(f"\nCalls ({len(info['callees'])}):\n")
    for c in info["callees"]:
        buf.write(f"  {c}\n")
    buf.write(f"\nCalled by ({len(info['called_by'])}):\n")
    for c in info["called_by"]:
        buf.write(f"  {c}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --similar: find structurally similar functions/classes (for refactoring)
# --------------------------------------------------------------------------- #

def _signature_shape(source):
    """Extract a signature shape: (name, param_count)."""
    import re as _re
    m = _re.search(r"(?:def|class|func|function)\s+(\w+)\s*\(([^)]*)\)", source)
    if not m:
        return None
    name = m.group(1)
    params = [p.strip() for p in m.group(2).split(",") if p.strip()]
    params = [p for p in params if p not in ("self", "cls")]
    return (name, len(params))

def similar_symbols(files, root, symbol, limit=10):
    """Find functions/classes with a similar signature shape (same param count)."""
    target = read_symbol(files, root, symbol)
    if target is None:
        return []
    target_shape = _signature_shape(target["source"])
    if target_shape is None:
        return []
    _, target_params = target_shape
    candidates = []
    import re as _re
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in CALL_LANG_RULES:
            continue
        mod = module_name_of(f, root)
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for m in _re.finditer(r"(?:def|class|func|function)\s+(\w+)\s*\(([^)]*)\)", text):
            name = m.group(1)
            if name == symbol:
                continue
            params = [p.strip() for p in m.group(2).split(",") if p.strip()]
            params = [p for p in params if p not in ("self", "cls")]
            if len(params) == target_params:
                candidates.append({"module": mod, "name": name, "params": len(params)})
    return candidates[:limit]

def render_similar(files, root, symbol, limit=10):
    results = similar_symbols(files, root, symbol, limit)
    buf = io.StringIO()
    buf.write(f"# similar: {symbol}\n")
    if not results:
        buf.write("No structurally similar symbols found.\n")
        return buf.getvalue()
    buf.write(f"{len(results)} symbol(s) with the same signature shape:\n\n")
    for r in results:
        buf.write(f"  {r['module']}.{r['name']}  ({r['params']} params)\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --deadcode: find functions defined but never called
# --------------------------------------------------------------------------- #

def dead_code(files, root, texts=None, index=None, parallel=False, calls=None):
    """Find functions/classes defined in the codebase but never called.
    O(n) — builds a called-set once, then each symbol lookup is O(1).
    `index` is an optional persistent index (symbols) to avoid re-parsing.
    `parallel` parallelizes the call-edge scan (--parallel).
    `calls` is an optional precomputed call graph (from the knowledge graph)."""
    if calls is None:
        calls = build_call_graph_multi(files, root, texts=texts, parallel=parallel)
    defined = set()
    called = set()
    for mod, funcs in calls.items():
        for caller, callees in funcs.items():
            defined.add(f"{mod}.{caller}")
            # callees may be a set (fresh build) or list (from kg) — normalize
            called |= set(callees)
    if index is not None:
        # use the persistent index's symbols — no re-parse
        for name, locs in index.get("symbols", {}).items():
            for loc in locs:
                defined.add(f"{loc['module']}.{name}")
    else:
        index_syms = build_symbol_index(files, root)
        for name, locs in index_syms.items():
            for loc in locs:
                defined.add(f"{loc['module']}.{name}")
    # O(1) lookup: a symbol is dead if its bare name is never in the called set
    dead = []
    for d in sorted(defined):
        if d.split(".")[-1] not in called:
            dead.append({"symbol": d})
    return dead

def render_deadcode(files, root, texts=None, index=None, parallel=False, calls=None):
    dead = dead_code(files, root, texts=texts, index=index, parallel=parallel, calls=calls)
    buf = io.StringIO()
    buf.write(f"# dead code — {len(dead)} symbol(s) defined but never called\n")
    if not dead:
        buf.write("No dead code found.\n")
        return buf.getvalue()
    buf.write("\n")
    for d in dead:
        buf.write(f"  {d['symbol']}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Incremental / indexed mode (hash-based cache, no daemon)
# --------------------------------------------------------------------------- #

CACHE_VERSION = 2

def _file_hash(path: str) -> str:
    """Return a content hash for a file (mtime + size + quick hash)."""
    import hashlib
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            data = f.read(8192)  # sample first 8KB
        h = hashlib.sha256()
        h.update(str(st.st_mtime_ns).encode())
        h.update(str(st.st_size).encode())
        h.update(data)
        return h.hexdigest()
    except OSError:
        return ""

def _cache_path(root: str) -> str:
    return os.path.join(root, ".codeloom-cache.json")

def load_cache(root: str) -> dict:
    """Load the incremental cache if present and valid."""
    try:
        with open(_cache_path(root), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == CACHE_VERSION:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": CACHE_VERSION, "files": {}, "gitignore": {}}

def save_cache(root: str, cache: dict) -> None:
    """Persist the incremental cache."""
    try:
        with open(_cache_path(root), "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass

def _gitignore_hash(root: str) -> str:
    """Hash all .gitignore files under root (root + nested). A change here
    invalidates the cached file list."""
    import hashlib
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirpath or ".venv" in dirpath or "node_modules" in dirpath:
            continue
        if ".gitignore" in filenames:
            p = os.path.join(dirpath, ".gitignore")
            try:
                with open(p, "rb") as f:
                    h.update(p.encode())
                    h.update(f.read())
            except OSError:
                pass
    return h.hexdigest()

def changed_files(files: List[str], cache: dict, root: str = None) -> List[str]:
    """Return files whose content hash changed vs the cache (or are new).
    If a .gitignore changed, treat all files as changed (the file set may
    have shifted)."""
    if root is not None:
        gi_hash = _gitignore_hash(root)
        if cache.get("gitignore", {}).get("hash") != gi_hash:
            return list(files)  # .gitignore changed -> re-walk everything
    changed = []
    for f in files:
        h = _file_hash(f)
        if cache.get("files", {}).get(f, {}).get("hash") != h:
            changed.append(f)
    return changed

def update_cache(files: List[str], cache: dict, root: str = None) -> None:
    """Update the cache with current file hashes."""
    for f in files:
        cache.setdefault("files", {})[f] = {"hash": _file_hash(f)}
    if root is not None:
        cache.setdefault("gitignore", {})["hash"] = _gitignore_hash(root)

def cached_symbols(files: List[str], root: str, cache: dict) -> dict:
    """Build a symbol index, reusing cached parsed data for unchanged files.
    This is the real scale win: repeated runs on large repos skip re-parsing
    files that haven't changed."""
    index: dict = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        mod = module_name_of(f, root)
        h = _file_hash(f)
        entry = cache.get("files", {}).get(f)
        # reuse cached symbols if unchanged
        if entry and entry.get("hash") == h and "symbols" in entry:
            for name, locs in entry["symbols"].items():
                index.setdefault(name, []).extend(locs)
            continue
        # parse fresh
        if ext == ".py":
            _index_python(f, mod, index)
        elif ext in CALL_LANG_RULES:
            _index_regex(f, mod, ext, index)
        # store per-file symbols in cache
        file_symbols = {}
        for name, locs in index.items():
            for loc in locs:
                if loc.get("module") == mod:
                    file_symbols.setdefault(name, []).append(loc)
        cache.setdefault("files", {})[f] = {"hash": h, "symbols": file_symbols}
    return index

def render_incremental(files: List[str], root: str, max_files: int) -> str:
    """Show which files changed since the last run (incremental mode)."""
    cache = load_cache(root)
    changed = changed_files(files, cache, root)
    buf = io.StringIO()
    buf.write(f"# codeloom --incremental — {len(changed)} changed file(s) since last run\n")
    if not changed:
        buf.write("No changes. Run `codeloom` to refresh the full map.\n")
    else:
        buf.write("\n## Changed files\n")
        for c in sorted(changed):
            buf.write(f"  {os.path.relpath(c, root)}\n")
    # update cache so next run is incremental
    update_cache(files, cache, root)
    save_cache(root, cache)
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Persistent on-disk index (scale without a daemon).
# `codeloom --index` builds a full byte-offset symbol index once and saves it to
# .codeloom-index.json. Subsequent `--get-symbol`/`--search` load it in
# milliseconds instead of re-parsing — the scale win for large repos, with no
# background process. Incrementally refreshed via content hashes.
# --------------------------------------------------------------------------- #

INDEX_VERSION = 1

def _index_path(root: str) -> str:
    return os.path.join(root, ".codeloom-index.json")

def _index_bin_path(root: str) -> str:
    return os.path.join(root, ".codeloom-index.bin")

def build_persistent_index(files: List[str], root: str) -> dict:
    """Build a full byte-offset symbol index (all languages)."""
    return build_byte_index(files, root)

def build_knowledge_graph(files: List[str], root: str) -> dict:
    """Build the knowledge-graph edges (call + import) for the persistent index.
    This is what lets heavy ops (--cross, --deadcode) load from the index
    instead of re-parsing every file — daemon-speed, no daemon."""
    calls = build_call_graph_multi(files, root)
    graph = build_graph(files, root)  # import edges
    return {
        "calls": {m: {c: sorted(s) for c, s in funcs.items()} for m, funcs in calls.items()},
        "imports": {m: sorted(deps) for m, deps in graph.items()},
    }

def save_persistent_index(root: str, index: dict, files: List[str], kg: Optional[dict] = None) -> None:
    """Save the persistent index with per-file (mtime, size) for incremental
    refresh. Size is tracked because Windows mtime has ~2s resolution, so a
    quick append may not change mtime — but it always changes size.
    `kg` is the optional knowledge-graph edges (call + import).
    Writes a binary (marshal) copy for fast load at scale."""
    data = {
        "version": INDEX_VERSION,
        "root": root,
        "files": {f: (os.path.getmtime(f), os.path.getsize(f))
                  for f in files if os.path.isfile(f)},
        "symbols": index,
    }
    if kg:
        data["kg"] = kg
    try:
        with open(_index_path(root), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass
    # binary copy for fast load (marshal is stdlib, ~10x faster than json)
    try:
        import marshal
        with open(_index_bin_path(root), "wb") as f:
            marshal.dump(data, f)
    except OSError:
        pass

def load_persistent_index(root: str) -> Optional[dict]:
    """Load the persistent index if present and valid. Prefers the binary
    (marshal) copy for speed; falls back to JSON."""
    import marshal
    # binary first — much faster at scale
    try:
        with open(_index_bin_path(root), "rb") as f:
            data = marshal.load(f)
        if data.get("version") == INDEX_VERSION:
            return data
    except (OSError, ValueError, EOFError):
        pass
    try:
        with open(_index_path(root), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == INDEX_VERSION:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None

def index_is_fresh(root: str, pidx: dict, sample: int = 0) -> bool:
    """Check whether the persistent index is fresh (no files changed since).
    Compares (mtime, size) — size catches quick appends that Windows mtime
    (2s resolution) would miss. `sample` bounds the check to N files so
    monorepo queries stay fast (0 = check all, for --index-status)."""
    files = pidx.get("files", {})
    items = list(files.items())
    if sample > 0:
        items = items[:sample]
    for path, meta in items:
        if not os.path.isfile(path):
            return False
        try:
            cur = (os.path.getmtime(path), os.path.getsize(path))
            if cur != tuple(meta):
                return False
        except OSError:
            return False
    return True

def ensure_fresh_index(root: str, max_files: int) -> Optional[dict]:
    """Load the persistent index, rebuilding it if stale. Returns the index.
    Uses a bounded freshness sample (200 files) so monorepo queries are fast;
    --index-status does the full check."""
    pidx = load_persistent_index(root)
    if pidx is None:
        return None
    if index_is_fresh(root, pidx, sample=200):
        return pidx
    # stale — rebuild
    gi = os.path.join(root, ".gitignore")
    rules = parse_gitignore(gi) if os.path.isfile(gi) else []
    files: List[str] = []
    _walk(root, rules, max_files, files)
    index = build_persistent_index(files, root)
    save_persistent_index(root, index, files)
    return load_persistent_index(root)

def render_index(files: List[str], root: str, max_files: int) -> str:
    """Build and save the persistent index + knowledge graph. Returns a summary."""
    index = build_persistent_index(files, root)
    kg = build_knowledge_graph(files, root)
    save_persistent_index(root, index, files, kg=kg)
    n_syms = sum(len(v) for v in index.values())
    n_edges = sum(len(v) for v in kg["calls"].values()) + sum(len(v) for v in kg["imports"].values())
    buf = io.StringIO()
    buf.write(f"# codeloom --index — built persistent index + knowledge graph\n")
    buf.write(f"  {len(files)} files, {n_syms} symbols, {n_edges} call/import edges\n")
    buf.write(f"  saved to {_index_path(root)}\n")
    buf.write(f"  subsequent --get-symbol/--search/--cross/--deadcode load it in milliseconds\n")
    return buf.getvalue()

def render_index_status(root: str) -> str:
    """Show whether a persistent index exists and how fresh it is."""
    data = load_persistent_index(root)
    buf = io.StringIO()
    buf.write(f"# codeloom --index-status\n")
    if data is None:
        buf.write("  No persistent index. Run `codeloom --index` to build one.\n")
        return buf.getvalue()
    buf.write(f"  index present ({len(data.get('symbols', {}))} symbols)\n")
    # check freshness: any file changed since index?
    stale = 0
    for f, h in data.get("files", {}).items():
        if _file_hash(f) != h:
            stale += 1
    if stale:
        buf.write(f"  {stale} file(s) changed since index — run `codeloom --index` to refresh\n")
    else:
        buf.write("  index is fresh\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --verify: checksum for security (curl|chmod safety)
# --------------------------------------------------------------------------- #

def sha256_file(path: str) -> str:
    """Return the full SHA-256 of a file."""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""

def render_verify(path: str) -> str:
    """Print the SHA-256 of a file so users can verify a downloaded copy."""
    digest = sha256_file(path)
    buf = io.StringIO()
    buf.write(f"# codeloom --verify {path}\n")
    if not digest:
        buf.write("File not found or unreadable.\n")
        return buf.getvalue()
    buf.write(f"sha256: {digest}\n")
    buf.write("\nCompare this against the published checksum in the repo to confirm\n")
    buf.write("the file you downloaded is the official codeloom and not tampered with.\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --impact: blast-radius prediction (graph reachability)
# --------------------------------------------------------------------------- #

def impact_analysis(graph: dict, start: str) -> dict:
    """Predict the blast radius of changing a module: what depends on it
    (direct + transitive) and what it depends on. Uses graph reachability."""
    dependents = sorted(reachable(graph, start, "in"))
    deps = sorted(reachable(graph, start, "out"))
    # direct dependents (one hop) are the most likely to break
    direct_dependents = sorted(_dependents(graph, start))
    return {
        "module": start,
        "direct_dependents": direct_dependents,
        "all_dependents": dependents,
        "depends_on": deps,
        "risk": "high" if len(direct_dependents) > 5 else ("medium" if direct_dependents else "low"),
    }

def render_impact(graph: dict, root: str, start: str) -> str:
    imp = impact_analysis(graph, start)
    buf = io.StringIO()
    buf.write(f"# impact: {start}\n")
    buf.write(f"risk: {imp['risk']}\n")
    buf.write(f"\n## Direct dependents ({len(imp['direct_dependents'])}) — most likely to break\n")
    for d in imp["direct_dependents"]:
        buf.write(f"  {d}\n")
    buf.write(f"\n## All dependents ({len(imp['all_dependents'])}) — transitive blast radius\n")
    for d in imp["all_dependents"]:
        buf.write(f"  {d}\n")
    buf.write(f"\n## Depends on ({len(imp['depends_on'])})\n")
    for d in imp["depends_on"]:
        buf.write(f"  {d}\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --task: task-aware relevance ranking
# --------------------------------------------------------------------------- #

def _tokenize(text: str) -> set:
    """Lowercase, split on non-alphanumerics AND camelCase/snake_case boundaries,
    drop stopwords/short tokens. So 'loginBug' and 'login_bug' both yield 'login'."""
    stop = {"the", "and", "for", "with", "this", "that", "from", "into", "are",
            "was", "were", "have", "has", "had", "not", "but", "its", "it's",
            "you", "your", "our", "their", "them", "they", "will", "would",
            "should", "could", "can", "do", "does", "did", "is", "am", "are",
            "a", "an", "to", "of", "in", "on", "at", "by", "be", "been", "being"}
    # split on non-alphanumerics first (preserving case for camelCase split)
    words = re.findall(r"[a-zA-Z0-9_]+", text)
    out = set()
    for w in words:
        # split snake_case
        for part in w.split("_"):
            if part:
                out.add(part.lower())
        # split camelCase: loginBug -> login, bug (split on case boundaries)
        for m in re.finditer(r"[a-z]+|[A-Z][a-z]*|\d+", w):
            out.add(m.group(0).lower())
    return {w for w in out if len(w) > 2 and w not in stop}

def _module_tokens(path: str) -> set:
    """Extract identifier tokens from a source file (function/class/var names)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return set()
    return _tokenize(text)

def task_relevance(files: List[str], root: str, task: str, top: int = 10) -> List[dict]:
    """Rank modules by relevance to a task string. Score = token overlap (weighted
    by module-name matches) + graph centrality bonus. Uses the multi-language
    import graph so non-Python modules participate too.

    If an embedding backend is available (local sentence-transformers or an
    OpenAI-compatible API via CODELOOM_EMBED_* env vars), semantic similarity
    replaces token overlap for much better relevance ranking."""
    task_tokens = _tokenize(task)
    if not task_tokens:
        return []
    # build multi-language import graph for centrality
    graph = build_graph_multi(files, root)

    # optional embedding backend
    embed = _embedding_backend()
    embed_vectors = None
    if embed is not None:
        try:
            # embed the task + each module's first ~200 chars
            texts = [task] + [_module_preview(f) for f in files]
            vecs = embed(texts)
            task_vec = vecs[0]
            embed_vectors = {f: vecs[i + 1] for i, f in enumerate(files)}
        except Exception:
            embed_vectors = None

    scored = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in LANG_RULES and ext not in IMPORT_LANG_RULES:
            continue
        mod = module_name_of(f, root)
        toks = _module_tokens(f)
        overlap = len(task_tokens & toks)
        if overlap == 0 and embed_vectors is None:
            continue
        # module-name match bonus: if the module name itself matches a task token
        mod_tokens = _tokenize(mod)
        name_bonus = len(task_tokens & mod_tokens) * 3
        # centrality: how many modules depend on this one (transitively)
        centrality = len(reachable(graph, mod, "in"))
        if embed_vectors is not None:
            # semantic similarity dominates when embeddings are available
            sim = _cosine_sim(task_vec, embed_vectors.get(f, []))
            score = sim * 100 + name_bonus + min(centrality, 10)
        else:
            score = overlap * 2 + name_bonus + min(centrality, 10)
        scored.append({"module": mod, "path": f, "score": score,
                       "overlap": overlap, "centrality": centrality})
    scored.sort(key=lambda s: (-s["score"], s["module"]))
    return scored[:top]

def _module_preview(path: str, max_chars: int = 200) -> str:
    """Return a short preview of a module's content for embedding."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except OSError:
        return ""

def render_task(files: List[str], root: str, task: str, top: int = 10) -> str:
    results = task_relevance(files, root, task, top)
    buf = io.StringIO()
    buf.write(f"# task: {task}\n")
    if not results:
        buf.write("No modules matched the task. Try different keywords.\n")
        return buf.getvalue()
    buf.write(f"Top {len(results)} relevant modules (by token overlap + graph centrality):\n\n")
    for i, r in enumerate(results, 1):
        buf.write(f"{i}. {r['module']}  (score {r['score']}, {r['overlap']} keyword hits, "
                  f"{r['centrality']} dependents)\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --plan: agent-native prioritized reading plan
# --------------------------------------------------------------------------- #

def build_plan(files: List[str], root: str, task: str, top: int = 8) -> str:
    """Emit a prioritized 'read these files, in this order' plan for a task."""
    results = task_relevance(files, root, task, top)
    buf = io.StringIO()
    buf.write(f"# plan: {task}\n\n")
    if not results:
        buf.write("No relevant modules found. Refine the task description.\n")
        return buf.getvalue()
    buf.write("Read these files, in this order, to understand the task:\n\n")
    for i, r in enumerate(results, 1):
        buf.write(f"{i}. {r['path']}\n")
        buf.write(f"   why: {r['overlap']} keyword match(es), {r['centrality']} module(s) depend on it\n")
    buf.write("\nThen run `codeloom --impact <file>` on the file you plan to change.\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --pack: single-shot context window optimizer (the moat feature)
# --------------------------------------------------------------------------- #

def render_pack(files: List[str], root: str, task: str, top: int = 8,
                include_symbols: bool = True) -> str:
    """Emit a single compact context file for a task: the modules that matter,
    in reading order, with impact analysis and a symbol index — all pre-computed
    in one shot. An agent pastes this once and has everything it needs, with
    zero per-query retrieval during the session.

    This is the feature jcodemunch can't do: it's retrieval-only, so it can't
    prioritize. codeloom turns a query tool into a context engine."""
    results = task_relevance(files, root, task, top)
    buf = io.StringIO()
    buf.write(f"# codeloom --pack: {task}\n")
    buf.write(f"# Single-shot context for the agent. {len(results)} relevant module(s).\n\n")

    if not results:
        buf.write("No modules matched the task. Refine the task description.\n")
        return buf.getvalue()

    # 1. Reading order (the plan)
    buf.write("## Reading order (most relevant first)\n")
    for i, r in enumerate(results, 1):
        buf.write(f"{i}. {r['path']}  (score {r['score']}, {r['overlap']} keyword hits, "
                  f"{r['centrality']} dependents)\n")
    buf.write("\n")

    # 2. Impact analysis for each relevant module
    buf.write("## Impact (what breaks if you change each)\n")
    for r in results:
        mod = r["module"]
        try:
            impact = render_impact(build_graph_multi(files, root), root, mod)
            # extract just the risk + dependents lines
            for line in impact.splitlines():
                if line.startswith("risk:") or line.startswith("  src") or line.startswith("  tests"):
                    buf.write(f"  {mod}: {line.strip()}\n")
        except Exception:
            pass
    buf.write("\n")

    # 3. Symbol index for the relevant modules (definitions + snippets)
    if include_symbols:
        buf.write("## Symbols in the relevant modules\n")
        for r in results:
            path = r["path"]
            ext = os.path.splitext(path)[1].lower()
            if ext not in LANG_RULES:
                continue
            mod = r["module"]
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            # find top-level defs/classes via the language rules
            def_re, _ = CALL_LANG_RULES.get(ext, (None, None))
            if def_re is None:
                continue
            for m in re.finditer(def_re, text, re.MULTILINE):
                name = next((g for g in m.groups() if g), None)
                if name:
                    line = text[:m.start()].count("\n") + 1
                    buf.write(f"  {mod}.{name}  [{ext[1:]} :{line}]\n")
        buf.write("\n")

    buf.write("## How to use\n")
    buf.write("This file is the complete context for the task. Read it once, then work.\n")
    buf.write("For the full source of any symbol, run `codeloom --get-symbol <name> --full`.\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --diff: git-aware, structure of changed files
# --------------------------------------------------------------------------- #

def git_changed_files(root: str) -> List[str]:
    """Return paths of files changed vs HEAD (tracked + untracked), root-relative."""
    changed: List[str] = []
    try:
        import subprocess
        # tracked changes (staged + unstaged) vs HEAD
        r = subprocess.run(
            ["git", "-C", root, "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            changed += [l for l in r.stdout.splitlines() if l.strip()]
        # untracked files
        r2 = subprocess.run(
            ["git", "-C", root, "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10,
        )
        if r2.returncode == 0:
            changed += [l for l in r2.stdout.splitlines() if l.strip()]
    except Exception:
        pass
    # dedupe, keep only existing files
    seen = set()
    out = []
    for c in changed:
        c = c.strip()
        if c and c not in seen and os.path.isfile(os.path.join(root, c)):
            seen.add(c)
            out.append(c)
    return out

def render_diff(root: str, max_files: int) -> str:
    """Show the structure of only the files changed vs HEAD."""
    changed = git_changed_files(root)
    if not changed:
        return "# codeloom --diff\nNo changes vs HEAD.\n"
    # build a tree from just the changed files
    files = [os.path.join(root, c) for c in changed]
    tree = build_tree(files, root, want_outline=True)
    buf = io.StringIO()
    buf.write(f"# codeloom --diff — {len(changed)} changed file(s)\n")
    buf.write("## Changed files\n")
    for c in sorted(changed):
        buf.write(f"  {c}\n")
    buf.write("\n## Structure of changes\n")
    for line in render_tree(tree):
        buf.write(line + "\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Multi-language call graph (lightweight, zero-dep)
# --------------------------------------------------------------------------- #

# regex-based function definition + call detection per language.
# Less precise than tree-sitter, but zero-dependency and good enough for
# structural understanding. Keys are file extensions.
CALL_LANG_RULES: dict = {
    ".py":   (r"^\s*(?:async\s+)?def\s+(\w+)", r"\b(\w+)\s*\("),
    ".js":   (r"^\s*(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()", r"\b(\w+)\s*\("),
    ".ts":   (r"^\s*(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()", r"\b(\w+)\s*\("),
    ".jsx":  (r"^\s*(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()", r"\b(\w+)\s*\("),
    ".tsx":  (r"^\s*(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()", r"\b(\w+)\s*\("),
    ".go":   (r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)", r"\b(\w+)\s*\("),
    ".rs":   (r"^\s*(?:pub\s+)?fn\s+(\w+)", r"\b(\w+)\s*\("),
    ".java": (r"^\s*(?:public|private|protected|static|final|synchronized|abstract|native|transient|volatile|default|strictfp|)\s+[\w<>\[\],\s]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".c":    (r"^\s*(?:static\s+)?[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".h":    (r"^\s*(?:static\s+)?[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".cpp":  (r"^\s*(?:static\s+)?[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".hpp":  (r"^\s*(?:static\s+)?[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".cs":   (r"^\s*(?:public|private|protected|internal|static|async|virtual|override|sealed|abstract|readonly|)\s+[\w<>\[\],\s]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".rb":   (r"^\s*def\s+(\w+)", r"\b(\w+)\s*\("),
    ".php":  (r"^\s*(?:public|private|protected|static|function)\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".swift":(r"^\s*(?:public|private|internal|fileprivate|static|func)\s+func\s+(\w+)", r"\b(\w+)\s*\("),
    ".kt":   (r"^\s*(?:public|private|internal|protected|fun)\s+fun\s+(\w+)", r"\b(\w+)\s*\("),
    ".sh":   (r"^\s*(\w+)\s*\(\)\s*\{", r"\b(\w+)\s*\("),
    ".lua":  (r"^\s*(?:local\s+)?function\s+(\w+)", r"\b(\w+)\s*\("),
    ".dart": (r"^\s*(?:void|int|String|bool|double|List|Map|dynamic|Future|Stream|final|var|const|)\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
}

# String/comment-aware scanner: strips strings and comments (preserving line
# structure) so regexes only match REAL code, not text inside strings/comments.
# This is the precision win over raw regex — it eliminates false positives like
# `helper()` appearing inside a string literal or a comment.
def _strip_strings_comments(text: str, ext: str) -> str:
    """Return a copy of `text` with string literals and comments replaced by
    spaces (same length, same newlines), so regexes match only real code."""
    out = list(text)
    i = 0
    n = len(text)
    hash_comment = ext in (".py", ".rb", ".sh", ".lua", ".go", ".rs", ".dart", ".php", ".kt", ".swift")
    slash_comment = ext in (".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".kt", ".swift", ".dart", ".php")
    while i < n:
        c = text[i]
        if c == "#" and hash_comment:
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/" and slash_comment:
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*" and slash_comment:
            out[i] = " "; out[i + 1] = " "
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                out[i] = " "
                i += 1
            if i + 1 < n:
                out[i] = " "; out[i + 1] = " "
                i += 2
            continue
        if c in ("'", '"'):
            quote = c
            out[i] = " "
            i += 1
            while i < n:
                if text[i] == "\\":
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "
                        i += 2
                    continue
                if text[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                out[i] = " "
                i += 1
            continue
        if c == "`" and ext in (".js", ".ts", ".jsx", ".tsx"):
            out[i] = " "
            i += 1
            while i < n:
                if text[i] == "\\":
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "
                        i += 2
                    continue
                if text[i] == "`":
                    out[i] = " "
                    i += 1
                    break
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)

def _scan_calls(text: str, ext: str, all_defined: set) -> set:
    """Find calls to repo-defined functions in `text`, skipping strings/comments."""
    clean = _strip_strings_comments(text, ext)
    call_re = r"\b(\w+)\s*\("
    found = set()
    for m in re.finditer(call_re, clean):
        callee = m.group(1)
        if callee in all_defined:
            found.add(callee)
    return found

def _scan_defs(text: str, ext: str) -> set:
    """Find function/class definition names in `text`, skipping strings/comments."""
    clean = _strip_strings_comments(text, ext)
    def_re, _ = CALL_LANG_RULES[ext]
    found = set()
    for m in re.finditer(def_re, clean, re.MULTILINE):
        name = next((g for g in m.groups() if g), None)
        if name:
            found.add(name)
    return found

def build_call_graph_multi(files: List[str], root: str, texts: Optional[dict] = None, parallel: bool = False) -> dict:
    """Multi-language call graph. {module: {func: set(called_funcs)}}.
    Uses tree-sitter for precise AST parsing when a grammar is available;
    falls back to a string/comment-aware scanner (more precise than raw regex)
    otherwise. Only reports calls to functions defined in the codebase
    (builtins filtered). `texts` is an optional {path: content} cache so
    callers can pre-read files in parallel (--parallel). `parallel` parallelizes
    the call-edge scan across processes (stdlib multiprocessing)."""
    # First pass: collect defined function names per module.
    defined: dict = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in CALL_LANG_RULES:
            continue
        mod = module_name_of(f, root)
        defined[mod] = set()
        # tree-sitter fast-path for precise function names
        ts_root = _ts_parse(f, ext)
        if ts_root is not None:
            defined[mod] = _ts_function_names(ts_root)
            continue
        text = texts.get(f) if texts else None
        if text is None:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
        def_re, _ = CALL_LANG_RULES[ext]
        for m in re.finditer(def_re, text, re.MULTILINE):
            name = next((g for g in m.groups() if g), None)
            if name:
                defined[mod].add(name)

    all_defined: set = set()
    for s in defined.values():
        all_defined |= s

    # Second pass: find calls, keep only repo-defined callees.
    calls: dict = {}
    if parallel and len(files) >= 50:
        import multiprocessing as mp
        # pre-read files (or reuse texts cache)
        if texts is None:
            texts = read_files_parallel(files, parallel=True)
        args_list = [(f, texts.get(f), root, all_defined) for f in files]
        with mp.Pool() as pool:
            results = pool.map(_scan_calls_worker, args_list)
        for mod, edges in results:
            if mod is not None and edges:
                calls[mod] = edges
        return calls
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in CALL_LANG_RULES:
            continue
        mod = module_name_of(f, root)
        calls[mod] = {}
        # tree-sitter fast-path for precise call edges
        ts_root = _ts_parse(f, ext)
        if ts_root is not None:
            for caller, callee in _ts_call_edges(ts_root):
                if callee in all_defined and callee != caller:
                    calls[mod].setdefault(caller, set()).add(callee)
            # drop empty callers
            calls[mod] = {k: v for k, v in calls[mod].items() if v}
            continue
        text = texts.get(f) if texts else None
        if text is None:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
        def_re, _ = CALL_LANG_RULES[ext]
        # find each function body and the calls within it, using the
        # string/comment-aware scanner (no false positives from strings/comments)
        clean = _strip_strings_comments(text, ext)
        clean_lines = clean.splitlines()
        current_func = None
        for line, clean_line in zip(text.splitlines(), clean_lines):
            dm = re.match(def_re, clean_line)
            if dm:
                name = next((g for g in dm.groups() if g), None)
                current_func = name
                calls[mod].setdefault(current_func, set())
                # also scan the def line itself for calls (e.g. one-liners)
                for cm in re.finditer(r"\b(\w+)\s*\(", clean_line):
                    callee = cm.group(1)
                    if callee in all_defined and callee != current_func:
                        calls[mod][current_func].add(callee)
                continue
            if current_func:
                for cm in re.finditer(r"\b(\w+)\s*\(", clean_line):
                    callee = cm.group(1)
                    if callee in all_defined and callee != current_func:
                        calls[mod][current_func].add(callee)
    # drop empty callers
    for mod in list(calls):
        calls[mod] = {k: v for k, v in calls[mod].items() if v}
    return calls

# --------------------------------------------------------------------------- #
# --install-agents: write/update AGENTS.md
# --------------------------------------------------------------------------- #

AGENTS_BLOCK = """<!-- codeloom: auto-generated. Run `codeloom` to refresh. -->
Before editing anything, run `codeloom` and read the output to build a mental model of the repo.
"""

def install_agents(root: str) -> str:
    """Write or update AGENTS.md with a codeloom instruction block."""
    path = os.path.join(root, "AGENTS.md")
    block = AGENTS_BLOCK
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if "codeloom: auto-generated" in content:
            # replace existing block
            import re as _re
            content = _re.sub(r"<!-- codeloom: auto-generated.*?-->\n.*?\n", block, content, flags=_re.DOTALL)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"updated {path}"
        else:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n" + block)
            return f"appended to {path}"
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(block)
        return f"created {path}"

# --------------------------------------------------------------------------- #
# Token-cost reporting
# --------------------------------------------------------------------------- #

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars/token for code, ~4.5 for prose."""
    return max(1, len(text) // 4)

def render_token_report(m: dict, text: str) -> str:
    """Append a token-cost line to the map output."""
    tokens = estimate_tokens(text)
    buf = io.StringIO()
    buf.write(f"\n## Cost\n")
    buf.write(f"  ~{tokens} tokens (~{len(text)} bytes) — vs ~40k+ tokens for grep+read on a large repo\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Session telemetry (--session, --session-report) — local observability
# --------------------------------------------------------------------------- #
# jcodemunch has session_yield/telemetry/cost accounting. codeloom's answer is
# LOCAL, no-network, no-daemon session telemetry: each invocation logs its
# command, tokens, and cost to a JSONL file; --session-report summarizes it.
# This gives the agent/developer observability without a daemon or telemetry
# that phones home — it stays on your machine.

SESSION_LOG = ".codeloom-session.jsonl"

def _session_path(root: str) -> str:
    return os.path.join(root, SESSION_LOG)

def log_session(root: str, command: str, text: str) -> None:
    """Append one invocation to the local session log (JSONL)."""
    import json as _json
    import time as _time
    entry = {
        "ts": _time.time(),
        "cmd": command,
        "tokens": estimate_tokens(text),
        "bytes": len(text),
    }
    try:
        with open(_session_path(root), "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry) + "\n")
    except OSError:
        pass

def render_session_report(root: str) -> str:
    """Summarize the local session log: total calls, tokens, cost estimate."""
    import json as _json
    path = _session_path(root)
    buf = io.StringIO()
    buf.write(f"# codeloom --session-report\n")
    if not os.path.isfile(path):
        buf.write("  No session log yet. Run `codeloom --session` to start logging.\n")
        return buf.getvalue()
    total_tokens = 0
    total_calls = 0
    cmd_counts = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                total_tokens += e.get("tokens", 0)
                total_calls += 1
                cmd = e.get("cmd", "?")
                cmd_counts[cmd] = cmd_counts.get(cmd, 0) + 1
    except OSError:
        buf.write("  Could not read session log.\n")
        return buf.getvalue()
    # cost estimate: ~$15/MTok input (Claude Opus), conservative
    est_cost = total_tokens / 1_000_000 * 15
    buf.write(f"  {total_calls} call(s), {total_tokens} token(s) logged\n")
    buf.write(f"  est. input cost (Opus ~$15/MTok): ${est_cost:.4f}\n\n")
    buf.write("## By command\n")
    for cmd, n in sorted(cmd_counts.items(), key=lambda x: -x[1]):
        buf.write(f"  {cmd}: {n}\n")
    return buf.getvalue()

def render_text(m: dict) -> str:
    ep = m["entry_points"]
    buf = io.StringIO()
    buf.write(f"# codeloom — {m['root']}\n")
    buf.write(f"{m['file_count']} files\n")
    if ep:
        buf.write("\n## Entry points\n")
        for e in ep:
            buf.write(f"  {os.path.relpath(e, m['root'])}\n")
    buf.write("\n## Structure\n")
    for line in render_tree(m["tree"]):
        buf.write(line + "\n")
    return buf.getvalue()

def tree_to_json(node: Node) -> dict:
    return {
        "name": node.name,
        "dir": node.is_dir,
        "outline": node.outline,
        "children": [tree_to_json(c) for c in node.children],
    }

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="codeloom", description=__doc__)
    p.add_argument("root", nargs="?", default=".", help="repo path (default: cwd)")
    p.add_argument("--write", metavar="FILE", help="write map to FILE too")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--no-outline", action="store_true", help="skip per-file outlines (faster)")
    p.add_argument("--max-files", type=int, default=5000, help="cap traversal (default 5000)")
    p.add_argument("--parallel", action="store_true", help="parallelize file parsing for heavy ops (--cross/--deadcode/--calls) on large repos")
    p.add_argument("--graph", action="store_true", help="show Python import dependency graph")
    p.add_argument("--focus", metavar="MODULE", help="show deps/dependents of one module (with --graph)")
    p.add_argument("--calls", action="store_true", help="show function-level call graph (multi-language)")
    p.add_argument("--diff", action="store_true", help="show structure of files changed vs HEAD (git)")
    p.add_argument("--install-agents", action="store_true", help="write/update AGENTS.md with a codeloom block")
    p.add_argument("--cost", action="store_true", help="append token-cost estimate to output")
    p.add_argument("--session", action="store_true", help="log this invocation to the local session log (JSONL)")
    p.add_argument("--session-report", action="store_true", help="summarize the local session log (calls, tokens, cost)")
    p.add_argument("--impact", metavar="MODULE", help="predict blast radius of changing a module")
    p.add_argument("--task", metavar="TEXT", help="rank modules relevant to a task description")
    p.add_argument("--plan", metavar="TEXT", help="emit a prioritized reading plan for a task")
    p.add_argument("--pack", metavar="TEXT", help="emit a single-shot context file for a task (reading order + impact + symbols)")
    p.add_argument("--cross", action="store_true", help="show cross-file call graph (resolved across modules)")
    p.add_argument("--search", metavar="SYMBOL", help="search the symbol index for a function/class/method")
    p.add_argument("--usages", metavar="SYMBOL", help="find where a symbol is used (not just defined)")
    p.add_argument("--grep", metavar="QUERY", help="search file contents for a snippet (ranked + context)")
    p.add_argument("--read", metavar="SYMBOL", help="extract exact source of a function/class/method (token-efficient)")
    p.add_argument("--explain", metavar="SYMBOL", help="plain-English explanation of a symbol (AST + call graph)")
    p.add_argument("--similar", metavar="SYMBOL", help="find structurally similar functions/classes (refactoring)")
    p.add_argument("--deadcode", action="store_true", help="find functions defined but never called")
    p.add_argument("--get-symbol", metavar="SYMBOL", help="token-counted symbol snippet (byte offsets + token estimate)")
    p.add_argument("--summary", action="store_true", help="summary-first retrieval (signature+docstring+call graph, not full source)")
    p.add_argument("--full", action="store_true", help="with --get-symbol, return the full source (default is summary)")
    p.add_argument("--snippet", nargs=3, metavar=("PATH", "START", "END"), help="extract a byte-range snippet from a file")
    p.add_argument("--incremental", action="store_true", help="show files changed since last run (hash-based cache)")
    p.add_argument("--verify", metavar="FILE", help="print SHA-256 of a file (security check)")
    p.add_argument("--trace", nargs="+", metavar="CMD", help="run a command under sys.settrace, record runtime call edges")
    p.add_argument("--force", action="store_true", help="acknowledge --trace executes code (isolation warning)")
    p.add_argument("--install-grammars", action="store_true", help="install tree-sitter language grammars (opt-in precision)")
    p.add_argument("--yes", action="store_true", help="with --install-grammars, actually run pip install")
    p.add_argument("--index", action="store_true", help="build + save a persistent byte-offset index (scale)")
    p.add_argument("--index-status", action="store_true", help="show persistent index status/freshness")
    p.add_argument("--framework", action="store_true", help="detect the web/app framework and surface its structure (routes, models, config, conventions)")
    p.add_argument("--version", action="version", version=f"codeloom {VERSION}")
    args = p.parse_args(argv)

    root = os.path.abspath(args.root)

    # --session: log this invocation to the local session log (every command)
    if args.session:
        log_session(root, " ".join(sys.argv[1:]), " ".join(sys.argv[1:]))

    # --install-grammars: opt-in tree-sitter grammar installer
    if args.install_grammars:
        print(install_grammars(do_install=args.yes))
        return 0

    # --index-status: show persistent index status
    if args.index_status:
        print(render_index_status(root))
        return 0

    # --framework: detect the web/app framework and surface its structure
    if args.framework:
        print(render_framework(root, args.max_files))
        return 0

    # --session-report: summarize the local session log
    if args.session_report:
        print(render_session_report(root))
        return 0

    # --index: build + save the persistent index
    if args.index:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_index(files, root, args.max_files))
        return 0

    # --get-symbol / --search: fast-path from the persistent index (no walk)
    # --full also uses the index (it stores the full source) — no repo re-scan.
    if (args.get_symbol or args.search):
        pidx = ensure_fresh_index(root, args.max_files)
        if pidx is not None:
            if args.get_symbol:
                locs = pidx.get("symbols", {}).get(args.get_symbol)
                if locs:
                    loc = locs[0]
                    if args.full:
                        # full source straight from the index — no re-parse
                        print(f"# get_symbol: {args.get_symbol}\n"
                              f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                              f"bytes {loc.get('start_byte',0)}-{loc.get('end_byte',0)}  "
                              f"~{loc.get('tokens',0)} tokens\n\n"
                              f"{loc.get('source','')}\n")
                        return 0
                    # adaptive: small symbols return the implementation by default
                    if loc.get("tokens", 0) <= ADAPTIVE_FULL_THRESHOLD:
                        print(f"# get_symbol: {args.get_symbol}\n"
                              f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                              f"bytes {loc.get('start_byte',0)}-{loc.get('end_byte',0)}  "
                              f"~{loc.get('tokens',0)} tokens\n\n"
                              f"{loc.get('source','')}\n")
                        return 0
                    # render summary directly from the index loc (no re-parse)
                    sig = loc.get("source", "").split("\n")[0][:60] or args.get_symbol
                    print(f"# get_symbol: {args.get_symbol}\n"
                          f"{loc['module']}:{loc['line']}  [{loc['kind']}]  ~10 tokens (summary)\n\n"
                          f"Signature: {sig}\n"
                          f"Use `--get-symbol {args.get_symbol} --full` for the full source.\n")
                    return 0
                # symbol not in index — return fast, don't scan the whole repo
                print(f"# get_symbol: {args.get_symbol}\nSymbol not found in index. "
                      f"Run `codeloom --index` to refresh, or use --full to scan.\n")
                return 0
            if args.search:
                print(render_search(pidx.get("symbols", {}), args.search))
                return 0

    # --trace: runtime call edges (static blind spots)
    if args.trace:
        if not args.force:
            print("WARNING: --trace EXECUTES the given command. It may have side "
                  "effects (network, files, etc.).\n"
                  "Run in an isolated sandbox/container/CI job, or re-run with "
                  "--force to acknowledge and proceed.\n"
                  "Aborting (use --force to override).")
            return 1
        print(render_trace(args.trace, root))
        return 0

    # --verify: checksum for security
    if args.verify:
        print(render_verify(args.verify))
        return 0

    # --snippet: byte-range extraction
    if args.snippet:
        path, start, end = args.snippet
        path = os.path.join(root, path) if not os.path.isabs(path) else path
        try:
            s = get_snippet_by_offset(path, int(start), int(end))
        except ValueError:
            print("Error: START and END must be integers.")
            return 1
        if s is None:
            print(f"Error: cannot read {path}")
            return 1
        print(f"# snippet: {path} bytes {start}-{end}  ~{s['tokens']} tokens  {s['bytes']} bytes\n")
        print(s["text"])
        return 0

    # --install-agents: write/update AGENTS.md
    if args.install_agents:
        print(install_agents(root))
        return 0

    # --diff: git-aware, structure of changed files
    if args.diff:
        print(render_diff(root, args.max_files))
        return 0

    # --incremental: hash-based cache, no daemon
    if args.incremental:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_incremental(files, root, args.max_files))
        return 0

    # --cross / --search / --usages / --grep / --read / --explain / --similar / --deadcode / --get-symbol
    if args.cross or args.search or args.usages or args.grep or args.read \
       or args.explain or args.similar or args.deadcode or args.get_symbol:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)

        if args.get_symbol:
            # summary-first by default; --full returns the full source
            use_summary = not args.full
            # adaptive: small symbols return the implementation by default
            if not args.full:
                use_summary = "adaptive"
            # use persistent index if present (fast path), else build fresh
            pidx = load_persistent_index(root)
            if pidx is not None:
                locs = pidx.get("symbols", {}).get(args.get_symbol)
                if locs:
                    loc = locs[0]
                    if use_summary == "adaptive":
                        # adaptive: full source if small, summary if large
                        if loc.get("tokens", 0) <= ADAPTIVE_FULL_THRESHOLD:
                            print(f"# get_symbol: {args.get_symbol}\n"
                                  f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                                  f"bytes {loc['start_byte']}-{loc['end_byte']}  ~{loc['tokens']} tokens\n\n"
                                  f"{loc['source']}\n")
                        else:
                            print(render_get_symbol(files, root, args.get_symbol, summary=True))
                    elif use_summary:
                        print(render_get_symbol(files, root, args.get_symbol, summary=True))
                    else:
                        print(f"# get_symbol: {args.get_symbol}\n"
                              f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                              f"bytes {loc['start_byte']}-{loc['end_byte']}  ~{loc['tokens']} tokens\n\n"
                              f"{loc['source']}\n")
                    return 0
            print(render_get_symbol(files, root, args.get_symbol, summary=(use_summary is True), adaptive=(use_summary == "adaptive")))
            return 0

        if args.search:
            # use persistent index if present (fast path), else build fresh
            pidx = load_persistent_index(root)
            if pidx is not None:
                print(render_search(pidx.get("symbols", {}), args.search))
                return 0
            cache = load_cache(root)
            index = cached_symbols(files, root, cache)
            save_cache(root, cache)
            print(render_search(index, args.search))
            return 0

        if args.usages:
            print(render_usages(files, root, args.usages))
            return 0

        if args.grep:
            print(render_grep(files, root, args.grep))
            return 0

        if args.read:
            print(render_read(files, root, args.read))
            return 0

        if args.explain:
            print(render_explain(files, root, args.explain))
            return 0

        if args.similar:
            print(render_similar(files, root, args.similar))
            return 0

        if args.deadcode:
            pidx = ensure_fresh_index(root, args.max_files)
            # load call edges from the knowledge graph if present (no re-parse)
            kg_calls = None
            if pidx and pidx.get("kg"):
                kg_calls = pidx["kg"].get("calls")
            if kg_calls:
                print(render_deadcode(files, root, index=pidx, calls=kg_calls))
            elif args.parallel:
                texts = read_files_parallel(files, parallel=True)
                print(render_deadcode(files, root, texts=texts, index=pidx, parallel=True))
            else:
                print(render_deadcode(files, root, index=pidx))
            return 0

        if args.cross:
            calls = build_cross_call_graph(files, root)
            focus = None
            if args.focus:
                focus = args.focus
                focus_path = os.path.join(root, focus) if not os.path.isabs(focus) else focus
                if os.path.isdir(focus_path):
                    focus = module_name_of(focus_path, root)
                elif focus.endswith(".py") or os.path.isfile(focus_path) or os.path.isfile(focus_path + ".py"):
                    focus = module_name_of(focus_path + (".py" if os.path.isfile(focus_path + ".py") else ""), root)
                if focus not in calls:
                    fsegs = focus.split(".")
                    match = None
                    for mod in calls:
                        msegs = mod.split(".")
                        if len(msegs) >= len(fsegs) and msegs[-len(fsegs):] == fsegs:
                            if match is None or len(msegs) < len(match.split(".")):
                                match = mod
                    if match is not None:
                        focus = match
                    else:
                        print(f"module not found: {args.focus}", file=sys.stderr)
                        return 1
            print(render_cross_calls(calls, root, start=focus))
            return 0

    # --impact / --task / --plan / --pack: task-aware intelligence
    if args.impact or args.task or args.plan or args.pack:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)

        if args.impact:
            graph = build_graph(files, root)
            target = args.impact
            target_path = os.path.join(root, target) if not os.path.isabs(target) else target
            if os.path.isdir(target_path):
                target = module_name_of(target_path, root)
            elif target.endswith(".py") or os.path.isfile(target_path) or os.path.isfile(target_path + ".py"):
                target = module_name_of(target_path + (".py" if os.path.isfile(target_path + ".py") else ""), root)
            if target not in graph:
                # suffix match
                fsegs = target.split(".")
                match = None
                for mod in graph:
                    msegs = mod.split(".")
                    if len(msegs) >= len(fsegs) and msegs[-len(fsegs):] == fsegs:
                        if match is None or len(msegs) < len(match.split(".")):
                            match = mod
                if match is not None:
                    target = match
                else:
                    print(f"module not found: {args.impact}", file=sys.stderr)
                    return 1
            print(render_impact(graph, root, target))
            return 0

        if args.task:
            print(render_task(files, root, args.task))
            return 0

        if args.plan:
            print(build_plan(files, root, args.plan))
            return 0

        if args.pack:
            print(render_pack(files, root, args.pack))
            return 0

    # Call-graph mode (multi-language)
    if args.graph or args.calls:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.calls:
            calls = build_call_graph_multi(files, root)
            focus = None
            if args.focus:
                focus = args.focus
                focus_path = os.path.join(root, focus) if not os.path.isabs(focus) else focus
                if os.path.isdir(focus_path):
                    focus = module_name_of(focus_path, root)
                elif focus.endswith(".py") or os.path.isfile(focus_path) or os.path.isfile(focus_path + ".py"):
                    focus = module_name_of(focus_path + (".py" if os.path.isfile(focus_path + ".py") else ""), root)
                if focus not in calls:
                    # suffix match (e.g. 'core.engine' matches 'src.core.engine')
                    fsegs = focus.split(".")
                    match = None
                    for mod in calls:
                        msegs = mod.split(".")
                        if len(msegs) >= len(fsegs) and msegs[-len(fsegs):] == fsegs:
                            if match is None or len(msegs) < len(match.split(".")):
                                match = mod
                    if match is not None:
                        focus = match
                    else:
                        print(f"module not found: {args.focus}", file=sys.stderr)
                        return 1
            text = render_calls(calls, root, start=focus)
            if args.cost:
                text += render_token_report({}, text)
            print(text)
            return 0
        graph = build_graph_multi(files, root)
        if args.focus:
            # accept file path, directory (package), or dotted module name
            focus = args.focus
            focus_path = os.path.join(root, focus) if not os.path.isabs(focus) else focus
            if os.path.isdir(focus_path):
                focus = module_name_of(focus_path, root)
            elif focus.endswith(".py") or os.path.isfile(focus_path):
                focus = module_name_of(focus_path, root)
            if focus not in graph:
                # try suffix match (e.g. 'main' matches 'src.main')
                fsegs = focus.split(".")
                match = None
                for mod in graph:
                    msegs = mod.split(".")
                    if len(msegs) >= len(fsegs) and msegs[-len(fsegs):] == fsegs:
                        if match is None or len(msegs) < len(match.split(".")):
                            match = mod
                if match is not None:
                    focus = match
                else:
                    print(f"module not found: {args.focus}", file=sys.stderr)
                    return 1
            text = render_graph_multi(graph, root, start=focus)
        else:
            text = render_graph_multi(graph, root)
        if args.cost:
            text += render_token_report({}, text)
        print(text)
        return 0

    m = build_map(args.root, not args.no_outline, args.max_files)

    if args.json:
        payload = {
            "version": VERSION,
            "root": m["root"],
            "file_count": m["file_count"],
            "entry_points": [os.path.relpath(e, m["root"]) for e in m["entry_points"]],
            "tree": tree_to_json(m["tree"]),
        }
        print(json.dumps(payload, indent=2))
    else:
        text = render_text(m)
        if args.cost:
            text += render_token_report(m, text)
        print(text)
        if args.write:
            with open(args.write, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"\n[written to {args.write}]", file=sys.stderr)

    return 0

if __name__ == "__main__":
    sys.exit(main())
