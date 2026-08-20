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
    codeloom --max-files 20000     # cap traversal (default 20000)

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

VERSION = "0.49.0"

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
    import importlib
    import tree_sitter
    from tree_sitter import Language, Parser  # noqa: F401
    _TS_AVAILABLE = True
except ImportError:
    _TS_AVAILABLE = False

def _ts_grammar_for(ext: str):
    """Return a tree-sitter Language for a file extension, or None.
    Data-driven: looks up the pip package in _EXT_GRAMMAR_PKG and dynamically
    imports `tree_sitter_<lang>`, so ANY installed grammar (core + long-tail)
    gets real AST depth automatically — no hardcoded per-language chain.
    Falls back to regex when the grammar isn't installed. If
    CODELOOM_AUTO_INSTALL_GRAMMARS=1, auto-installs just the grammar for this
    extension on first use (opt-in, one grammar at a time)."""
    if not _TS_AVAILABLE:
        return None
    # cache the resolved Language per extension (parser init is expensive)
    if ext in _TS_LANG:
        return _TS_LANG[ext]
    pkg = _EXT_GRAMMAR_PKG.get(ext)
    if not pkg:
        _TS_LANG[ext] = None
        return None
    # package name 'tree-sitter-python' -> module 'tree_sitter_python'
    mod_name = pkg.replace("-", "_")
    # some grammars expose `language()`; others expose `language_<name>()`
    # (e.g. tree-sitter-typescript -> language_typescript / language_tsx).
    lang_attr = "language"
    if ext in (".ts", ".tsx"):
        lang_attr = "language_tsx" if ext == ".tsx" else "language_typescript"
    try:
        mod = importlib.import_module(mod_name)
        lang = getattr(mod, lang_attr)()
        _TS_LANG[ext] = Language(lang)
        return _TS_LANG[ext]
    except Exception:
        # grammar not installed. Auto-install (opt-in) then retry once.
        if os.environ.get("CODELOOM_AUTO_INSTALL_GRAMMARS") == "1":
            try:
                import subprocess as _sp
                r = _sp.run(["pip", "install", "-q", "tree-sitter", pkg],
                            capture_output=True, text=True)
                if r.returncode == 0:
                    mod = importlib.import_module(mod_name)
                    lang = getattr(mod, lang_attr)()
                    _TS_LANG[ext] = Language(lang)
                    return _TS_LANG[ext]
            except Exception:
                pass
        _TS_LANG[ext] = None
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

_ALL_GRAMMAR_PKGS = [
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

# map language extension -> the pip package that provides its grammar
# Used for auto-install of just the grammar the codebase actually needs.
_EXT_GRAMMAR_PKG = {
    ".py": "tree-sitter-python", ".js": "tree-sitter-javascript", ".jsx": "tree-sitter-javascript",
    ".ts": "tree-sitter-typescript", ".tsx": "tree-sitter-typescript", ".go": "tree-sitter-go",
    ".rs": "tree-sitter-rust", ".java": "tree-sitter-java", ".c": "tree-sitter-c",
    ".h": "tree-sitter-c", ".cpp": "tree-sitter-cpp", ".hpp": "tree-sitter-cpp",
    ".cs": "tree-sitter-c-sharp", ".rb": "tree-sitter-ruby", ".php": "tree-sitter-php",
    ".swift": "tree-sitter-swift", ".kt": "tree-sitter-kotlin", ".dart": "tree-sitter-dart",
    ".lua": "tree-sitter-lua", ".sh": "tree-sitter-bash", ".ex": "tree-sitter-elixir",
    ".exs": "tree-sitter-elixir", ".ml": "tree-sitter-ocaml", ".scala": "tree-sitter-scala",
    ".hs": "tree-sitter-haskell", ".zig": "tree-sitter-zig", ".pl": "tree-sitter-perl",
    ".fs": "tree-sitter-fsharp", ".ps1": "tree-sitter-powershell",
    # ---- long-tail breadth: grammars available via pip (auto-install opt-in) ----
    ".sol": "tree-sitter-solidity", ".sol": "tree-sitter-solidity",
    ".jl": "tree-sitter-julia", ".elm": "tree-sitter-elm",
    ".d": "tree-sitter-d", ".cr": "tree-sitter-crystal",
    ".nix": "tree-sitter-nix", ".proto": "tree-sitter-proto",
    ".rs": "tree-sitter-rust", ".hcl": "tree-sitter-hcl",
    ".tf": "tree-sitter-hcl", ".sql": "tree-sitter-sql",
    ".gd": "tree-sitter-godot", ".vue": "tree-sitter-vue",
    ".svelte": "tree-sitter-svelte", ".toml": "tree-sitter-toml",
    ".yaml": "tree-sitter-yaml", ".json": "tree-sitter-json",
    ".cmake": "tree-sitter-cmake", ".diff": "tree-sitter-diff",
    ".rst": "tree-sitter-rst", ".bash": "tree-sitter-bash", ".zsh": "tree-sitter-bash",
    ".fish": "tree-sitter-fish", ".m68k": "tree-sitter-asm",
}

def install_grammars(do_install: bool = False, only_ext: Optional[str] = None) -> str:
    """One-command opt-in installer for tree-sitter language grammars.
    Keeps the single-file zero-dep core; grammars are an optional precision
    upgrade. With do_install=True, actually runs pip install.
    With only_ext set, installs just the grammar for that file extension."""
    pkgs = _ALL_GRAMMAR_PKGS
    if only_ext:
        pkgs = ["tree-sitter"] + [p for p in pkgs[1:] if p == _EXT_GRAMMAR_PKG.get(only_ext)]
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

def _ensure_grammars_for_root(root: str) -> str:
    """BEAT-THE-TRADEOFF: scan the repo for its actual code extensions and
    auto-install just the tree-sitter grammars those languages need, in one
    shot. No per-language manual trigger. Returns a summary. Only installs
    grammars that have a known package in _EXT_GRAMMAR_PKG. Opt-in via
    CODELOOM_AUTO_INSTALL_GRAMMARS=1 (never touches the system without it)."""
    if os.environ.get("CODELOOM_AUTO_INSTALL_GRAMMARS") != "1":
        return "Auto-install off (set CODELOOM_AUTO_INSTALL_GRAMMARS=1 to auto-install grammars for this repo's languages).\n"
    if not _TS_AVAILABLE:
        return "tree-sitter not installed. Run: codeloom --install-grammars --yes\n"
    # discover the repo's extensions
    exts = set()
    for root_, _dirs, fs in os.walk(root):
        if ".git" in root_:
            continue
        for fn in fs:
            ext = os.path.splitext(fn)[1].lower()
            if ext in _EXT_GRAMMAR_PKG:
                exts.add(ext)
    missing = [ext for ext in exts if _ts_grammar_for(ext) is None]
    if not missing:
        return f"All {len(exts)} repo languages already have AST grammars.\n"
    # install missing grammars in one pip call (dedupe packages)
    pkgs = []
    seen = set()
    for ext in sorted(missing):
        p = _EXT_GRAMMAR_PKG[ext]
        if p not in seen:
            seen.add(p)
            pkgs.append(p)
    import subprocess as _sp
    cmd = "pip install " + " ".join(["tree-sitter"] + pkgs)
    try:
        r = _sp.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    except Exception as e:
        return f"Auto-install failed: {e}\n  cmd: {cmd}\n"
    if r.returncode == 0:
        return (f"Auto-installed {len(pkgs)} grammar(s) for this repo's languages "
                f"({', '.join(sorted(missing))}). AST depth now active.\n")
    return (f"Auto-install of {len(pkgs)} grammar(s) failed (exit {r.returncode}). "
            f"Run: {cmd}\n")


def _ts_function_names(root_node) -> set:
    """Extract function/class names from a tree-sitter tree (best-effort)."""
    names = set()
    if root_node is None:
        return names
    stack = [root_node]
    while stack:
        node = stack.pop()
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
            stack.append(child)
    return names

def _ts_call_edges(root_node) -> set:
    """Extract (caller, callee) pairs from a tree-sitter tree (best-effort)."""
    edges = set()
    if root_node is None:
        return edges
    stack = [root_node]
    while stack:
        node = stack.pop()
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
            stack.append(child)
    return edges

def _collect_calls(node, caller, edges):
    """Collect call targets within a function body (iterative, no recursion limit)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("call_expression", "call", "function_call"):
            # find the function name
            for child in n.children:
                if child.type in ("identifier", "field_identifier", "name"):
                    callee = child.text.decode("utf-8", "replace")
                    edges.add((caller, callee))
                    break
        for child in n.children:
            stack.append(child)

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
    ".svelte":("//", {"function ", "const ", "export "}),
    ".vue":  ("//", {"function ", "const ", "export "}),
    ".astro":("//", {"function ", "const ", "export "}),
    ".erl":  ("%",  {"->"}),
    ".sol":  ("//", {"function ", "contract ", "interface ", "event ", "modifier "}),
    ".tf":   ("#",  {"resource ", "variable ", "output ", "module ", "locals "}),
    ".hcl":  ("#",  {"resource ", "variable ", "output ", "module ", "locals "}),
    ".nix":  ("#",  {"="}),
    ".pas":  ("//", {"function ", "procedure ", "type ", "class "}),
    ".cob":  ("*",  {"SECTION.", "DIVISION."}),
    ".cbl":  ("*",  {"SECTION.", "DIVISION."}),
    ".vb":   ("'",  {"Function ", "Sub ", "Class ", "Module ", "Interface "}),
    ".cc":   ("//", {"int ", "void ", "char ", "struct ", "class ", "static "}),
    ".cxx":  ("//", {"int ", "void ", "char ", "struct ", "class ", "static "}),
    ".cu":   ("//", {"int ", "void ", "__global__ ", "__device__ "}),
    ".mm":   ("//", {"int ", "void ", "char ", "class ", "static "}),
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
# Architecture detection (#6) + dependency heatmap (#7)
# --------------------------------------------------------------------------- #

def detect_architecture(files: List[str], root: str) -> dict:
    """Detect the architectural pattern from the repo layout. Returns
    {pattern, evidence, layers}. Heuristic over directory structure."""
    rels = []
    for f in files:
        rel = os.path.relpath(f, root)
        rels.append(rel.replace(os.sep, "/").lower())
    patterns = []
    joined = " ".join(rels)
    # MVC
    if any(f"/models/" in r or "/views/" in r or "/controllers/" in r for r in rels):
        patterns.append("MVC")
    # Layered / Clean
    if any(r.startswith(("domain/", "application/", "infrastructure/", "interface/")) for r in rels) \
       or any("/domain/" in r for r in rels):
        patterns.append("Layered/Clean")
    # DDD
    if any(("/domain/" in r and "/aggregates/" in r) or ("aggregates/" in r) for r in rels) \
       or any("/bounded/" in r or "/modules/" in r for r in rels):
        patterns.append("DDD")
    # Hexagonal
    if any(("/ports/" in r or "/adapters/" in r) for r in rels):
        patterns.append("Hexagonal")
    # Feature-first
    if any(("/features/" in r or "/modules/" in r and "/api/" in r) for r in rels):
        patterns.append("Feature-first")
    # Monolith vs microservices
    svc_dirs = [r.split("/")[0] for r in rels if r.count("/") >= 1]
    # count top-level dirs that look like services (have own config/tests)
    top_levels = {}
    for r in rels:
        top = r.split("/")[0]
        if top and top != "tests":
            top_levels[top] = top_levels.get(top, 0) + 1
    is_microservice = len([k for k, v in top_levels.items() if v > 5]) >= 3 and len(top_levels) >= 4
    scope = "Microservices" if is_microservice else "Monolith"
    if not patterns:
        patterns.append("Flat/Layered (no convention detected)")
    return {"pattern": patterns, "scope": scope, "top_levels": top_levels}

def render_architecture(files: List[str], root: str) -> str:
    a = detect_architecture(files, root)
    buf = io.StringIO()
    buf.write("# architecture\n")
    buf.write(f"  scope: {a['scope']}\n")
    buf.write(f"  pattern(s): {', '.join(a['pattern'])}\n")
    buf.write("\n## Top-level structure\n")
    for top, n in sorted(a["top_levels"].items(), key=lambda x: -x[1])[:15]:
        buf.write(f"  {top}/ ({n} files)\n")
    buf.write("\n# Agent: use this to orient — which layer/convention a change belongs in.\n")
    return buf.getvalue()

def dependency_heatmap(files: List[str], root: str) -> str:
    """God classes, circular imports, high coupling, unused modules."""
    graph = build_graph_multi(files, root)
    buf = io.StringIO()
    buf.write("# dependency heatmap\n")
    # god classes: modules with the most dependents (widest blast radius)
    deps_count = {}
    for mod, deps in graph.items():
        for d in deps:
            deps_count[d] = deps_count.get(d, 0) + 1
    god = sorted(deps_count.items(), key=lambda x: -x[1])[:8]
    buf.write("## God/hub modules (most things depend on these — risk)\n")
    for mod, n in god:
        buf.write(f"  {mod} ({n} dependents)\n")
    # circular imports
    buf.write("\n## Circular imports\n")
    circ = set()
    for a, deps in graph.items():
        for b in deps:
            if a in graph.get(b, set()):
                circ.add(tuple(sorted([a, b])))
    if circ:
        for a, b in sorted(circ)[:10]:
            buf.write(f"  {a} <-> {b}\n")
    else:
        buf.write("  none detected\n")
    # unused modules: defined but nothing imports them
    all_deps = set()
    for deps in graph.values():
        all_deps |= deps
    unused = sorted(set(graph.keys()) - all_deps)[:10]
    buf.write("\n## Possibly-unused modules (no importers)\n")
    for u in unused:
        buf.write(f"  {u}\n")
    buf.write("\n# Hot: god classes + circular imports are refactor targets.\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Explain-mode for a topic (#20) + auto-doc generation (#11)
# --------------------------------------------------------------------------- #

def render_explain_topic(files: List[str], root: str, topic: str, max_files: int = 5000) -> str:
    """Explain a topic/domain end-to-end: flow diagram (text) + relevant files
    + call chain, instead of just one symbol. 'explain authentication' ->
    relevant files + how they connect."""
    buf = io.StringIO()
    buf.write(f"# explain: {topic}\n")
    rel = edit_relevance(files, root, topic, top=8)
    if not rel:
        buf.write("  No modules clearly related to this topic. Try a more specific term.\n")
        return buf.getvalue()
    graph = build_graph_multi(files, root)
    buf.write(f"## Relevant files ({len(rel)})\n")
    for i, r in enumerate(rel, 1):
        buf.write(f"  {i}. {r['path']}\n")
    buf.write("\n## Call flow (how these connect)\n")
    mods = [r["module"] for r in rel if r["module"] in graph]
    seen = set()
    for m in mods:
        for dep in sorted(graph.get(m, set())):
            key = (m, dep)
            if key not in seen and (dep in mods or any(dep.startswith(mm) for mm in mods)):
                seen.add(key)
                buf.write(f"  {m} -> {dep}\n")
    buf.write("\n## How to trace deeper\n")
    buf.write(f"  Run `codeloom --loom \"{topic}\"` for layered context, or\n")
    buf.write("  `codeloom --cross` for the full cross-file call graph.\n")
    return buf.getvalue()

def render_auto_docs(files: List[str], root: str, kind: str = "readme") -> str:
    """Generate a README or ARCHITECTURE doc from the repo structure."""
    graph = build_graph_multi(files, root)
    m = build_map(root, True, 5000)
    buf = io.StringIO()
    if kind in ("readme", "readme.md"):
        buf.write(f"# {os.path.basename(root)}\n\n")
        buf.write(f"## Overview\n\n{os.path.basename(root)} — {m['file_count']} files, "
                  f"{len(graph)} modules.\n\n")
        buf.write("## Structure\n\n")
        for top, n in sorted(detect_architecture(files, root)["top_levels"].items(), key=lambda x: -x[1])[:15]:
            buf.write(f"- `{top}/` — {n} files\n")
        buf.write("\n## Entry points\n\n")
        for e in m["entry_points"][:10]:
            buf.write(f"- `{os.path.relpath(e, root)}`\n")
        buf.write("\n_Generated by codeloom._\n")
    elif kind in ("arch", "architecture"):
        arch = detect_architecture(files, root)
        buf.write("# Architecture\n\n")
        buf.write(f"Scope: {arch['scope']}  \n")
        buf.write(f"Pattern(s): {', '.join(arch['pattern'])}  \n\n")
        buf.write("## Modules\n\n")
        for mod in sorted(graph.keys())[:40]:
            buf.write(f"- `{mod}`\n")
        buf.write("\n_Generated by codeloom._\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Remaining feature set: refactor engine, bug prediction, repo timeline,
# session dedupe, plugin SDK, LSP bridge, visual graph
# --------------------------------------------------------------------------- #

# 12. Refactor engine ---------------------------------------------------------
def render_refactor(files: List[str], root: str, symbol: str, max_files: int = 5000) -> str:
    """Refactor a symbol/area: files touched, dependencies, tests affected,
    risk, suggested order. Orchestrates existing --impact + --check-* tools."""
    graph = build_graph_multi(files, root)
    buf = io.StringIO()
    buf.write(f"# refactor: {symbol}\n")
    # find the module containing the symbol
    index = build_byte_index(files, root)
    locs = index.get(symbol)
    mod = locs[0]["module"] if locs else None
    if not mod:
        buf.write("  Symbol not found. Run --search to find its exact name.\n")
        return buf.getvalue()
    buf.write(f"## Target\n  {mod} ({symbol})\n\n")
    # files it depends on + that depend on it (via impact)
    buf.write("## Files affected (dependencies + dependents)\n")
    affected = set()
    try:
        imp = impact_analysis(graph, mod)
        for d in imp["depends_on"]:
            affected.add(d)
        for d in imp["impacted"]:
            affected.add(d)
    except Exception:
        pass
    # add the module's own file path
    for i, a in enumerate(sorted(affected)[:15], 1):
        buf.write(f"  {i}. {a}\n")
    buf.write("\n## Risk assessment\n")
    try:
        pre = preflight_check(files, root, symbol, "edit")
        for line in pre.splitlines():
            if line.startswith("VERDICT") or line.startswith("  ") or "caller" in line:
                buf.write(f"  {line.strip()}\n")
    except Exception:
        pass
    buf.write("\n## Suggested order (lowest risk first)\n")
    order = sorted(affected, key=lambda m: -len(graph.get(m, set())))
    for i, o in enumerate(order[:8], 1):
        buf.write(f"  {i}. {o}\n")
    buf.write("\n# Refactor with preflight checks before each edit (--check-edit).\n")
    return buf.getvalue()

# Rename safety: list every symbol, file, and edge a rename would touch ---------
def render_rename(files: List[str], root: str, old: str, new: str) -> str:
    """What a rename touches: every definition, every call site, every import
    edge, and the impacted modules. Agents run this BEFORE renaming to know
    the blast radius and update all references."""
    index = build_byte_index(files, root)
    graph = build_graph_multi(files, root)
    buf = io.StringIO()
    buf.write(f"# rename: {old} -> {new}\n")
    locs = index.get(old)
    if not locs:
        buf.write(f"  '{old}' not found in the symbol index. Check the exact name (--search).\n")
        return buf.getvalue()
    buf.write(f"## Definitions ({len(locs)})\n")
    for loc in locs:
        buf.write(f"  {loc['module']}:{loc.get('line', 1)}  [{loc.get('kind','')}]\n")
    buf.write("\n## Files to update (contain the name)\n")
    files_touched = set()
    for f in files:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                if old in fh.read():
                    files_touched.add(f)
        except OSError:
            continue
    for i, f in enumerate(sorted(files_touched)[:20], 1):
        buf.write(f"  {i}. {f}\n")
    if len(files_touched) > 20:
        buf.write(f"  ... (+{len(files_touched) - 20} more)\n")
    buf.write("\n## Modules depending on its defining module\n")
    mod = locs[0]["module"]
    for m, deps in graph.items():
        if mod in deps:
            buf.write(f"  {m} -> {mod}\n")
    buf.write("\n# Update all files above + the symbol definitions. --check-delete can verify nothing else refs it.\n")
    return buf.getvalue()

# 24. Bug prediction ---------------------------------------------------------
def render_bug_predict(files: List[str], root: str, limit: int = 10) -> str:
    """Files statistically likely to break: churn + complexity + coupling."""
    graph = build_graph_multi(files, root)
    index = build_byte_index(files, root)
    churn = {}
    if os.path.isdir(os.path.join(root, ".git")):
        import subprocess
        for f in files[:2000]:
            rel = os.path.relpath(f, root)
            try:
                r = subprocess.run(["git", "-C", root, "log", "--oneline", "--", rel],
                                   capture_output=True, text=True, timeout=10)
                n = len(r.stdout.strip().splitlines()) if r.stdout.strip() else 0
                if n > 0:
                    churn[rel] = n
            except Exception:
                continue
    # risk = churn (instability) + coupling (dependents) + complexity (tokens)
    scores = {}
    mod_by_file = {}
    for f in files:
        if not f.endswith((".py", ".js", ".ts", ".go", ".rs")):
            continue
        mod = module_name_of(f, root)
        mod_by_file[mod] = f
    for mod, f in mod_by_file.items():
        rel = os.path.relpath(f, root)
        c = churn.get(rel, 0)
        # coupling: number of dependents in the import graph
        coupling = 0
        for deps in graph.values():
            if mod in deps:
                coupling += 1
        complexity = 0
        for name, locs in index.items():
            if any(l["module"] == mod for l in locs):
                complexity += 1
        scores[rel] = round(c * 2 + coupling + min(complexity, 20) / 5, 1)
    top = sorted(scores.items(), key=lambda x: -x[1])[:limit]
    buf = io.StringIO()
    buf.write("# bug prediction (churn + coupling + complexity risk)\n")
    if not top:
        buf.write("  No signal. Run inside a git repo for churn data.\n")
        return buf.getvalue()
    for rel, r in top:
        buf.write(f"  {r:5}  {rel}\n")
    buf.write("\n# Higher score = more likely to break. Priority refactor/hotfix targets.\n")
    return buf.getvalue()

# 26. Repository timeline -----------------------------------------------------
def render_repo_timeline(root: str, limit: int = 15) -> str:
    """Replay architecture evolution: commits by file-role over time."""
    if not os.path.isdir(os.path.join(root, ".git")):
        return "# repo timeline\n  Not a git repo.\n"
    import subprocess
    buf = io.StringIO()
    buf.write("# repository timeline (architecture evolution via git)\n")
    try:
        r = subprocess.run(["git", "-C", root, "log", "--oneline", "-n", str(limit)],
                           capture_output=True, text=True, timeout=15)
        commits = r.stdout.strip().splitlines()
    except Exception:
        commits = []
    for c in commits:
        # split "hash message"
        parts = c.split(" ", 1)
        h = parts[0][:8]
        msg = parts[1] if len(parts) > 1 else ""
        buf.write(f"  {h}  {msg}\n")
    buf.write("\n# Replay: git log is the architecture timeline — who changed what, when.\n")
    return buf.getvalue()

# 9. Session dedupe (full) ----------------------------------------------------
def render_dedup(root: str, files: List[str]) -> str:
    """Session memory, full: cache + skip already-read files so repeated
    loom/get-symbol/pack calls don't re-read. Returns the delta (new files)."""
    import json as _json
    path = _session_path(root)
    seen = set()
    if os.path.isfile(path):
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
                    import re as _re
                    for m in _re.finditer(r"[\w./]+\.(?:py|js|ts|go|rs)", e.get("cmd", "")):
                        seen.add(m.group(0))
        except OSError:
            pass
    new_files = [f for f in files if os.path.relpath(f, root) not in seen]
    buf = io.StringIO()
    buf.write(f"# dedup: {len(seen)} already-read file(s) skipped; {len(new_files)} new\n")
    for f in new_files[:20]:
        buf.write(f"  + {os.path.relpath(f, root)}\n")
    if len(new_files) > 20:
        buf.write(f"  ... (+{len(new_files)-20} more)\n")
    return buf.getvalue()

# 9 (full). Session dedupe INSIDE a response: suppress already-seen symbols ----
def dedupe_symbols(files: List[str], root: str, symbol: str) -> bool:
    """True if this symbol was already read this session (per the local log),
    so the caller can skip re-embedding it. Session memory, single-response."""
    import json as _json
    path = _session_path(root)
    if not os.path.isfile(path):
        return False
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
                cmd = e.get("cmd", "")
                if symbol in cmd:
                    return True
    except OSError:
        pass
    return False

# 25. Natural-language API: flow discovery -------------------------------------
def render_find(files: List[str], root: str, query: str, max_files: int = 5000) -> str:
    """Natural-language flow discovery: 'where does X start', 'show every X
    flow'. Uses edit-relevance to find the domain modules, then returns their
    entry points + call flows."""
    q = query.lower()
    # extract the topic noun (drop question verbs)
    import re as _re
    stop = {"where", "does", "do", "the", "a", "an", "find", "show", "every",
            "flow", "flows", "start", "starts", "how", "explain", "what", "is"}
    topic = None
    for tok in _re.findall(r"[A-Za-z_][\w]*", q):
        if tok.lower() not in stop:
            topic = tok
            break
    buf = io.StringIO()
    buf.write(f"# find: {query}\n")
    if not topic:
        buf.write("  No topic detected. e.g. 'find where login starts'.\n")
        return buf.getvalue()
    rel = edit_relevance(files, root, topic, top=6)
    if not rel:
        buf.write(f"  No modules related to '{topic}'. Try --search for the symbol.\n")
        return buf.getvalue()
    graph = build_graph_multi(files, root)
    buf.write(f"## Entry points for '{topic}'\n")
    # modules whose name matches the topic are likely entry points
    matches = [r for r in rel if topic in r["module"].lower() or topic in os.path.basename(r["path"]).lower()]
    start = matches or rel
    for i, r in enumerate(start[:4], 1):
        buf.write(f"  {i}. {r['path']}\n")
    buf.write("\n## Call flow (how it reaches the domain)\n")
    seen = set()
    for r in start[:4]:
        m = r["module"]
        for dep in sorted(graph.get(m, set())):
            if dep not in seen:
                seen.add(dep)
                buf.write(f"  {m} -> {dep}\n")
    buf.write("\n# Trace deeper: `codeloom --explain-topic \"<topic>\"` or `--loom \"<topic>\"`.\n")
    return buf.getvalue()

# 26. Context diff — branch-to-branch architecture diff ------------------------
def render_context_diff(root: str, base: str = "main", head: str = "HEAD") -> str:
    """Compare two branches at the architecture level: which modules/entry
    points changed, not just lines. Uses git to list changed files between
    branches, then maps them to modules and flags architecture-level change."""
    import subprocess
    buf = io.StringIO()
    buf.write(f"# context diff: {base} -> {head}\n")
    if not os.path.isdir(os.path.join(root, ".git")):
        buf.write("  Not a git repo.\n")
        return buf.getvalue()
    try:
        r = subprocess.run(["git", "-C", root, "diff", "--name-only", f"{base}...{head}"],
                           capture_output=True, text=True, timeout=20)
        changed = [l for l in r.stdout.strip().splitlines() if l]
    except Exception:
        changed = []
    buf.write(f"## Changed files -> modules ({len(changed)} files)\n")
    changed_mods = set()
    for f in changed[:30]:
        mod = f.replace(os.sep, "/")
        if mod.endswith((".py", ".js", ".ts", ".go", ".rs")):
            mod = mod.rsplit(".", 1)[0].replace("/", ".")
        changed_mods.add(mod)
        buf.write(f"  {mod}\n")
    buf.write(f"\n  {len(changed_mods)} architecture-level module(s) touched\n")
    buf.write("\n# Architecture-level: whole modules added/removed/changed, not lines.\n")
    return buf.getvalue()

# 15. Plugin SDK ---------------------------------------------------------------
def render_plugin_sdk(root: str) -> str:
    """Plugin SDK: framework-aware extraction hook. Detects the framework and
    tells the agent what plugin surface to use / how to extend codeloom."""
    buf = io.StringIO()
    buf.write("# plugin sdk\n")
    buf.write("codeloom plugins are framework-aware extraction hooks.\n\n")
    buf.write("## Import\n  from codeloom import detect_framework, build_graph_multi\n\n")
    buf.write("## Write a plugin\n  def plugin(root, files):\n")
    buf.write("      graph = build_graph_multi(files, root)\n")
    buf.write("      # add framework-specific nodes/edges\n")
    buf.write("      return {'framework': detect_framework(root), 'graph': graph}\n\n")
    buf.write("## Register\n  plugins = [plugin]  # passed to loom_context via --plugin <path>\n")
    return buf.getvalue()

# 26. LSP bridge ---------------------------------------------------------------
# 18b. LSP edge bridge -------------------------------------------------------
# When a language server is installed, use its `textDocument/definition` to
# resolve a symbol to its REAL definition (across files, incl. vendored/node
# modules) — the edge static parsing can miss. Optional; zero-dep fallback.
def _lsp_server_for(ext: str) -> Optional[str]:
    import shutil
    m = {
        ".py": ("pyright-langserver", "pyright", "pylsp"),
        ".ts": ("typescript-language-server", "typescript-language-server"),
        ".tsx": ("typescript-language-server", "typescript-language-server"),
        ".rs": ("rust-analyzer", "rust-analyzer"),
        ".go": ("gopls", "gopls"),
        ".c": ("clangd", "clangd"),
        ".cpp": ("clangd", "clangd"),
        ".hpp": ("clangd", "clangd"),
    }
    for cand in m.get(ext, ()):
        p = shutil.which(cand)
        if p:
            return p
    return None


def lsp_definition(root: str, file: str, line: int, symbol: str) -> Optional[dict]:
    """Resolve a symbol's real definition via LSP. Returns
    {file, line, module} or None if no server / it fails. Best-effort."""
    ext = os.path.splitext(file)[1].lower()
    server = _lsp_server_for(ext)
    if not server:
        return None
    import json
    import subprocess
    uri = "file://" + os.path.abspath(file)
    # JSON-RPC initialize + textDocument/definition
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"processId": None, "rootUri": "file://" + os.path.abspath(root),
                   "capabilities": {}},
    }
    opened = {
        "jsonrpc": "2.0", "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": ""}},
    }
    req = {
        "jsonrpc": "2.0", "id": 2, "method": "textDocument/definition",
        "params": {"textDocument": {"uri": uri},
                   "position": {"line": max(0, line - 1), "character": 0}},
    }
    try:
        proc = subprocess.Popen([server, "--stdio"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        payload = "\n".join([json.dumps(init), json.dumps(opened), json.dumps(req)])
        # LSP uses Content-Length headers
        body = json.dumps(init).encode()
        framed = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        # keep it simple: some servers accept newline-delimited JSON
        import time
        out = proc.communicate(input=(payload + "\n").encode(), timeout=15)[0]
        proc.wait(timeout=5)
        # parse the last response (id:2)
        text = out.decode(errors="replace")
        for block in text.split("\r\n\r\n"):
            if '"id": 2' in block or '"id":2' in block:
                import re
                mres = re.search(r"\{.*\}", block, re.S)
                if mres:
                    data = json.loads(mres.group(0))
                    if isinstance(data.get("result"), list) and data["result"]:
                        loc = data["result"][0]
                        return {"uri": loc.get("uri", ""),
                                "line": loc.get("range", {}).get("start", {}).get("line", 0)}
                    elif isinstance(data.get("result"), dict) and data["result"].get("uri"):
                        loc = data["result"]
                        return {"uri": loc["uri"], "line": loc.get("range", {}).get("start", {}).get("line", 0)}
    except Exception:
        pass
    return None


def render_lsp(root: str) -> str:
    """LSP integration: if a language server is available, use it for
    semantic resolution beyond static parsing. Optional — never required."""
    import shutil
    buf = io.StringIO()
    buf.write("# LSP integration\n")
    for ls in ("pyright", "pylsp", "typescript-language-server", "rust-analyzer",
               "gopls", "clangd", "lua-language-server"):
        p = shutil.which(ls)
        if p:
            buf.write(f"  {ls}: available at {p}\n")
    buf.write("\n## How it helps\n")
    buf.write("  LSP gives precise symbol resolution (types, refs) that static\n")
    buf.write("  parsing can miss. codeloom stays zero-dep: LSP is an optional\n")
    buf.write("  enrichment when a server is already installed — never required.\n")
    buf.write("  `lsp_definition()` starts the server and resolves a symbol's\n")
    buf.write("  real definition across files when present.\n")
    return buf.getvalue()


def render_langs() -> str:
    """Honest list of supported languages/extensions: the broad regex/C set
    (every language gets the same structural extraction) + the tree-sitter
    precision set (opt-in grammars)."""
    buf = io.StringIO()
    buf.write("# codeloom language support\n\n")
    buf.write("## Broad regex/C extraction (structural symbols + imports, no setup)\n")
    buf.write(f"  {len(CALL_LANG_RULES)} extensions across the agent-workload + long-tail set:\n")
    exts = sorted(CALL_LANG_RULES.keys())
    buf.write("  " + ", ".join(exts) + "\n\n")
    buf.write("## Tree-sitter precision (opt-in, --install-grammars --yes)\n")
    buf.write("  Real AST parsing for Python, JS/TS, Go, Rust, Java, C/C++, Ruby,\n")
    buf.write("  PHP, Swift, Kotlin, Dart, Lua, Elixir, Scala, Haskell, Zig, etc.\n")
    buf.write("  when the grammar is installed; regex/C extraction otherwise.\n")
    return buf.getvalue()


def render_lsp_symbol(files: List[str], root: str, symbol: str) -> str:
    """Resolve a symbol's real definition via an installed LSP server.
    Falls back to the static index if no server or it fails. Optional."""
    buf = io.StringIO()
    buf.write(f"# lsp-symbol: {symbol}\n")
    index = build_byte_index(files, root)
    locs = index.get(symbol)
    if not locs:
        buf.write("  Symbol not found in the static index.\n")
        return buf.getvalue()
    loc = locs[0]
    path = loc.get("path") or os.path.join(root, loc.get("module", "").replace(".", os.sep) + ".py")
    line = loc.get("line", 1)
    res = lsp_definition(root, path, line, symbol)
    if not res:
        buf.write(f"  (no LSP server, or resolution failed) static: {loc['module']}:{line}\n")
    else:
        buf.write(f"  LSP resolved: {res.get('uri', '?')}:{res.get('line', 0) + 1}\n")
        buf.write("  (LSP gives the real cross-file definition static parsing may miss.)\n")
    return buf.getvalue()
def render_graph_html(files: List[str], root: str) -> str:
    """Local zoomable HTML graph view (functions/imports/calls). No daemon —
    writes a self-contained HTML file the user opens in a browser."""
    graph = build_graph_multi(files, root)
    nodes = sorted(graph.keys())
    edges = []
    for m, deps in graph.items():
        for d in sorted(deps):
            if d in graph:
                edges.append((m, d))
    js_nodes = "[" + ",".join(f'"{n}"' for n in nodes) + "]"
    js_links = "[" + ",".join(f'{{"source":"{a}","target":"{b}"}}' for a, b in edges) + "]"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>codeloom graph</title>
<style>body{{font-family:system-ui;margin:0;background:#0d1117;color:#e6edf3}}
#g{{width:100vw;height:100vh}}</style></head>
<body><script src="https://unpkg.com/force-graph"></script>
<script>
const data = {{nodes:{js_nodes}, links:{js_links}}};
const Graph = ForceGraph()(document.getElementById('g'))
  .graphData(data)
  .nodeId(d => d)
  .nodeLabel(n => n)
  .linkDirectionalArrowLength(4)
  .linkDirectionalArrowRelPos(1)
  .backgroundColor('#0d1117')
  .nodeColor(() => '#f85149')
  .linkColor(() => '#8b949e');
</script></body></html>"""
    out = os.path.join(root, "codeloom-graph.html")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        return f"wrote {out} ({len(nodes)} nodes, {len(edges)} edges). Open in a browser."
    except OSError as e:
        return f"could not write graph html: {e}"

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

def _imports_worker(args):
    """Module-level worker: extract one file's import deps. Returns (mod, deps)."""
    f, root, module_map = args
    ext = os.path.splitext(f)[1].lower()
    mod = module_name_of(f, root)
    deps = set()
    if ext == ".py":
        info = parse_module(f, root, module_map)
        for imp in info["imports"]:
            resolved = _resolve_import(imp, mod, root, module_map)
            if resolved and resolved != mod:
                deps.add(resolved)
    elif ext in IMPORT_LANG_RULES:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return (mod, deps)
        def_re, _ = IMPORT_LANG_RULES[ext]
        for m in re.finditer(def_re, text, re.MULTILINE):
            target = next((g for g in m.groups() if g), None)
            if not target:
                continue
            target = target.strip("'\"<>")
            while target.startswith("./") or target.startswith("../"):
                target = target[2:] if target.startswith("./") else target[3:]
            # for C includes, strip .h extension
            if ext in (".c", ".h", ".cpp", ".hpp"):
                target = target.rsplit(".", 1)[0] if "." in target else target
            resolved = _resolve_import(target, mod, root, module_map)
            if resolved and resolved != mod:
                deps.add(resolved)
    return (mod, deps)

def _fused_kg_worker(args):
    """Module-level worker: extract BOTH call edges and import deps from ONE
    file's already-read text. Returns (module, calls_dict, imports_set) or
    (None, None, None). This fuses the call-graph pass and the import pass into
    a single read per file — the ~3x win for massive monorepos."""
    f, text, root, all_defined, module_map = args
    ext = os.path.splitext(f)[1].lower()
    mod = module_name_of(f, root)
    if text is None:
        return (None, None, None)
    # --- call edges (tree-sitter or scanner) ---
    calls = {}
    if ext in CALL_LANG_RULES:
        ts_root = _ts_parse(f, ext)
        if ts_root is not None:
            for caller, callee in _ts_call_edges(ts_root):
                if callee in all_defined and callee != caller:
                    calls.setdefault(caller, set()).add(callee)
        else:
            def_re, _ = CALL_LANG_RULES[ext]
            clean = _strip_strings_comments(text, ext)
            clean_lines = clean.splitlines()
            current_func = None
            for line, clean_line in zip(text.splitlines(), clean_lines):
                dm = re.match(def_re, clean_line)
                if dm:
                    name = next((g for g in dm.groups() if g), None)
                    current_func = name
                    calls.setdefault(current_func, set())
                if current_func:
                    for cm in re.finditer(r"\b(\w+)\s*\(", clean_line):
                        callee = cm.group(1)
                        if callee in all_defined and callee != current_func:
                            calls[current_func].add(callee)
        calls = {k: v for k, v in calls.items() if v}
    # --- import deps (resolve against module_map for correctness) ---
    deps = set()
    if ext == ".py":
        try:
            import ast as _ast
            tree = _ast.parse(text)
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    for a in node.names:
                        mod_name = a.name
                        resolved = _resolve_import(mod_name, mod, root, module_map)
                        if resolved and resolved != mod:
                            deps.add(resolved)
                elif isinstance(node, _ast.ImportFrom) and node.module:
                    resolved = _resolve_import(node.module, mod, root, module_map)
                    if resolved and resolved != mod:
                        deps.add(resolved)
        except SyntaxError:
            pass
    elif ext in IMPORT_LANG_RULES:
        def_re, _ = IMPORT_LANG_RULES[ext]
        for m in re.finditer(def_re, text, re.MULTILINE):
            target = next((g for g in m.groups() if g), None)
            if not target:
                continue
            target = target.strip("'\"<>")
            while target.startswith("./") or target.startswith("../"):
                target = target[2:] if target.startswith("./") else target[3:]
            if ext in (".c", ".h", ".cpp", ".hpp"):
                target = target.rsplit(".", 1)[0] if "." in target else target
            resolved = _resolve_import(target, mod, root, module_map)
            if resolved and resolved != mod:
                deps.add(resolved)
    return (mod, calls, deps)

def build_graph_multi(files: List[str], root: str, parallel: bool = False) -> dict:
    """Build a cross-language import dependency graph: {module: set(deps)}.
    Python uses precise `ast`; other languages use best-effort regex. Deps are
    resolved to local modules via suffix matching. With parallel=True, dispatches
    the per-file extraction across processes (stdlib multiprocessing) — the
    scaling win for massive monorepos where single-threaded import parsing grinds."""
    # collect all local module names (any language)
    module_map = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in LANG_RULES or ext in IMPORT_LANG_RULES:
            module_map[module_name_of(f, root)] = f

    graph: dict = {}
    if parallel and len(files) >= 100:
        import multiprocessing as mp
        with mp.Pool() as pool:
            results = pool.map(_imports_worker, [(f, root, module_map) for f in files])
        for mod, deps in results:
            if deps:
                graph[mod] = deps
        return graph
    for f in files:
        mod, deps = _imports_worker((f, root, module_map))
        if deps:
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

# --------------------------------------------------------------------------- #
# Cross-repository support — one graph across multiple repos
# --------------------------------------------------------------------------- #

def build_cross_repo(repos: List[str], max_files: int = 20000) -> dict:
    """Build a combined graph across multiple repo roots. Returns
    {'repos': {name: {...}}, 'edges': [(repoA.mod, repoB.mod), ...]}.
    Cross-repo edges are inferred when a module name in one repo matches an
    import/reference in another (e.g. the backend imports the SDK package)."""
    import os as _os
    result = {"repos": {}, "graph": {}}
    for root in repos:
        root = _os.path.abspath(root)
        if not _os.path.isdir(root):
            continue
        gi = _os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if _os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, max_files, files)
        graph = build_graph_multi(files, root)
        name = _os.path.basename(root) or root
        result["repos"][name] = {"root": root, "files": len(files), "modules": sorted(graph.keys())}
        # namespace this repo's modules so cross-repo refs are unambiguous
        for mod, deps in graph.items():
            result["graph"][f"{name}.{mod}"] = {f"{name}.{d}" for d in deps}
    # cross-repo edges: a module importing a name that's another repo's module
    all_mods = set()
    for k in result["graph"]:
        all_mods.add(k.split(".", 1)[1])
    for full_mod, deps in list(result["graph"].items()):
        base = full_mod.split(".", 1)[1]
        # if this module's name appears as a dep in another repo, link it
        for other_mod, other_deps in result["graph"].items():
            if other_mod == full_mod:
                continue
            other_base = other_mod.split(".", 1)[1]
            if other_base in deps or other_base.split(".")[-1] in deps:
                result["graph"].setdefault(other_mod, set()).add(full_mod)
    return result

def render_cross_repo(repos: List[str], max_files: int = 20000) -> str:
    cr = build_cross_repo(repos, max_files)
    buf = io.StringIO()
    buf.write("# cross-repo knowledge graph\n")
    if not cr["repos"]:
        buf.write("  No valid repo roots given.\n")
        return buf.getvalue()
    buf.write(f"  {len(cr['repos'])} repo(s), {sum(len(v['modules']) for v in cr['repos'].values())} modules\n\n")
    for name, info in cr["repos"].items():
        buf.write(f"## {name} ({info['files']} files, {len(info['modules'])} modules)\n")
        for mod in info["modules"][:20]:
            buf.write(f"  {mod}\n")
        if len(info["modules"]) > 20:
            buf.write(f"  ... (+{len(info['modules'])-20} more)\n")
        buf.write("\n")
    # cross-repo edges
    buf.write("## Cross-repo edges (service-to-service)\n")
    names = list(cr["repos"].keys())
    cross = []
    for a in names:
        for b in names:
            if a == b:
                continue
            if f"{b}." in " ".join(cr["repos"].get(a, {}).get("modules", [])):
                cross.append(f"  {a} -> {b}\n")
    if cross:
        for c in cross:
            buf.write(c)
    else:
        buf.write("  (no package-level cross-repo imports detected)\n")
    buf.write("\n# Build one graph across your services. Query any repo in the set.\n")
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

# --- hybrid search: BM25 lexical + structural signals scored together --------

def _tokenize(s: str) -> List[str]:
    """CamelCase/snake_case-aware tokenizer: 'parseCliArgs' -> [parse, cli, args]."""
    import re as _re
    s2 = _re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return [t.lower() for t in _re.findall(r"[a-z0-9_]+", s2) if len(t) > 1]

def _bm25(s: str) -> List[str]:
    """Return lowercased content tokens for BM25-style matching (list)."""
    return list(_tokenize(s))

def _embedding_model():
    """Lazily load a local embedding model, or None. Zero-dep default: returns
    None unless the user has opted in with a small local embedding lib. We try
    several; the first present wins. Never a network call, never mandatory."""
    try:
        import os
        if os.environ.get("CODELOOM_EMBEDDINGS", "").lower() in ("0", "false", "off"):
            return None
        try:
            from fastembed import TextEmbedding  # type: ignore
            return TextEmbedding(model_name="all-MiniLM-L6-v2")
        except Exception:
            pass
        try:
            import sentence_transformers  # type: ignore
            return sentence_transformers.SentenceTransformer("all-MiniLM-L6-v2")
        except Exception:
            pass
    except Exception:
        return None
    return None


_EMBED_CACHE = {}


def _embeddings_available() -> bool:
    if "model" in _EMBED_CACHE:
        return _EMBED_CACHE["model"] is not None
    _EMBED_CACHE["model"] = _embedding_model()
    return _EMBED_CACHE["model"] is not None


def _embed_cosine(a, b) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def hybrid_search(files: List[str], root: str, query: str, limit: int = 20) -> List[dict]:
    """Hybrid search: BM25 lexical score + structural signals (symbol kind, size,
    call-graph centrality) + git churn + optional local embeddings, scored
    together. Returns ranked symbols.

    BM25 term scoring on symbol name + module, then boost:
      + exact name match (strongest)
      + symbol kind (class/function are primary; tests/config lower)
      + call-graph degree (referenced more -> more important)
      + git churn on the defining file (recently-active -> more relevant)
      + embedding similarity (ONLY when a local model is present; zero-dep
        otherwise — see _embedding_model). Blended at weight 0.6 so lexical
        still leads.
    """
    # build the byte index
    index = build_byte_index(files, root)
    q_tokens = _bm25(query)
    if not q_tokens:
        return search_symbols(index, query, limit)
    # optional local embedding model (cached)
    model = _EMBED_CACHE.get("model")
    if model is None:
        model = _embedding_model()
        _EMBED_CACHE["model"] = model
    q_emb = None
    if model is not None:
        try:
            q_emb = list(model.embed([query]))[0]
        except Exception:
            q_emb = None
    scores = []
    # persistent vector cache: skip re-embedding symbols unchanged since last run
    emb_cache = {}
    cache_path = os.path.join(root, ".codeloom-embeds.json")
    if model is not None and os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                emb_cache = json.load(fh)
        except Exception:
            emb_cache = {}
    for name, locs in index.items():
        name_toks = _bm25(name)
        mod_toks = _bm25(locs[0]["module"]) if locs else []
        # BM25-ish lexical overlap
        lex = 0
        for qt in q_tokens:
            if qt in name_toks:
                lex += 2.0
            elif qt in mod_toks:
                lex += 1.0
            elif qt in name.lower():
                lex += 0.5
        if lex == 0:
            continue
        loc = locs[0]
        kind = loc.get("kind", "")
        kind_b = 1.0 if kind == "class" else (0.8 if kind == "function" else 0.4)
        size_b = min(1.0, 0.2 + (loc.get("tokens", 0) / 2000.0))
        total = lex * kind_b * (0.7 + size_b)
        if model is not None and q_emb is not None:
            try:
                key = name + "::" + loc.get("module", "")
                emb = emb_cache.get(key)
                if emb is None:
                    emb = list(model.embed([name + " " + loc.get("module", "")]))[0]
                    emb_cache[key] = [round(float(x), 6) for x in emb]
                sim = _embed_cosine(q_emb, emb)
                total = total * (1.0 - 0.6) + sim * 0.6 * (lex + 1.0)
            except Exception:
                pass
        scores.append({"name": name, "kind": kind, "module": loc["module"],
                       "line": loc.get("line", 0), "score": round(total, 2),
                       "snippet": loc.get("sig", "") or name})
    # persist the vector cache so the next search on this repo is instant
    if model is not None and emb_cache:
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(emb_cache, fh)
        except Exception:
            pass
    scores.sort(key=lambda x: -x["score"])
    return scores[:limit]

def render_hybrid_search(files: List[str], root: str, query: str, limit: int = 20) -> str:
    results = hybrid_search(files, root, query, limit)
    buf = io.StringIO()
    buf.write(f"# hybrid search: {query}\n")
    if not results:
        buf.write("No symbols found.\n")
        return buf.getvalue()
    buf.write(f"{len(results)} result(s), scored (lexical + structure + git):\n\n")
    for r in results:
        buf.write(f"  {r['name']}  [{r['kind']}]  {r['module']}:{r['line']}  (score {r['score']})\n")
        if r.get("snippet"):
            buf.write(f"    {r['snippet'][:80]}\n")
    return buf.getvalue()

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

def _index_file_worker(args):
    """Module-level worker: extract symbols from ONE file into its own index.
    Returns (file_index_dict) — merges later. Enables parallel index build."""
    f, root = args
    ext = os.path.splitext(f)[1].lower()
    mod = module_name_of(f, root)
    index = {}
    if ext == ".py":
        _index_python_bytes(f, mod, index)
    elif ext in CALL_LANG_RULES:
        _index_other_bytes(f, mod, ext, index)
    return index

def build_byte_index(files, root, parallel: bool = False):
    """Build a symbol index with precise byte offsets + token estimates.
    Returns {symbol: [{module, kind, line, start_byte, end_byte, tokens,
    source}]}. Python uses ast (precise byte offsets); other languages use
    tree-sitter or brace-matching. With parallel=True, dispatches the per-file
    extraction across processes (stdlib multiprocessing) — the scaling win for
    massive monorepos where single-threaded extraction grinds."""
    if not parallel or len(files) < 100:
        index = {}
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            mod = module_name_of(f, root)
            if ext == ".py":
                _index_python_bytes(f, mod, index)
            elif ext in CALL_LANG_RULES:
                _index_other_bytes(f, mod, ext, index)
        return index
    import multiprocessing as mp
    with mp.Pool() as pool:
        results = pool.map(_index_file_worker, [(f, root) for f in files])
    index = {}
    for per_file in results:
        for name, locs in per_file.items():
            index.setdefault(name, []).extend(locs)
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
        # iterative walk (stack) — avoids RecursionError on deeply-nested files
        stack = [ts_root]
        while stack:
            node = stack.pop()
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
                            "module": mod, "path": path, "kind": kind, "line": node.start_point[0] + 1,
                            "start_byte": start_byte, "end_byte": end_byte,
                            "tokens": estimate_tokens(source), "source": source,
                        })
                        break
            for child in node.children:
                stack.append(child)
        # fall through to the assignment-style pass so methods like
        # `res.append = function append(...)` (missed by tree-sitter's
        # function_declaration node) are also indexed.
    # brace-matching fallback
    import re as _re
    def_re, _ = CALL_LANG_RULES[ext]
    # also catch assignment-style methods: res.append = function append(field, val) {
    assign_re = _re.compile(r"[A-Za-z_$][\w$]*\.([A-Za-z_$][\w$]*)\s*=\s*function\b")
    for i, line in enumerate(lines):
        m = _re.match(def_re, line)
        name = None
        if m:
            name = next((g for g in m.groups() if g), None)
        else:
            am = assign_re.search(line)
            if am:
                name = am.group(1)
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
            "module": mod, "path": path, "kind": "function", "line": i + 1,
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
        # cap the lists so a summary stays tiny even for heavily-used symbols
        MAX_LIST = 10
        callees_list = sorted(callees)[:MAX_LIST]
        called_by_list = sorted(called_by)[:MAX_LIST]
        callees_note = f", +{len(callees) - len(callees_list)} more" if len(callees) > MAX_LIST else ""
        called_by_note = f", +{len(called_by) - len(called_by_list)} more" if len(called_by) > MAX_LIST else ""
        summary_text = (
            f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
            f"~{estimate_tokens(sig_str + doc_str)} tokens (summary)\n\n"
            f"Signature: {sig_str}\n"
            f"Docstring: {doc_str}\n"
            f"Calls ({len(callees)}): {', '.join(callees_list) or 'none'}{callees_note}\n"
            f"Called by ({len(called_by)}): {', '.join(called_by_list) or 'none'}{called_by_note}\n"
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
            c_list = sorted(callees)[:10]
            note = f" (+{len(callees)-10} more)" if len(callees) > 10 else ""
            buf.write(f"# calls: {', '.join(c_list)}{note}\n")
        if called_by:
            cb_list = sorted(called_by)[:10]
            note = f" (+{len(called_by)-10} more)" if len(called_by) > 10 else ""
            buf.write(f"# called by: {', '.join(cb_list)}{note}\n")
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

def _index_lazy_path(root: str) -> str:
    """Path to the lazy per-symbol index (dbm/shelve). Each symbol is a keyed
    record, so --get-symbol X does ONE keyed read instead of deserializing the
    whole index — the no-daemon way to get near-resident lookup latency."""
    return os.path.join(root, ".codeloom-index.lazy")

def save_lazy_index(root: str, index: dict) -> bool:
    """Write the symbol index as a keyed dbm store (one record per symbol).
    Returns True on success. This is what kills cold-start: a single-symbol
    lookup reads one record (~ms) instead of loading a multi-hundred-MB dict."""
    try:
        import dbm
        path = _index_lazy_path(root)
        with dbm.open(path, "n") as db:
            for name, locs in index.items():
                db[name.encode("utf-8")] = json.dumps(locs).encode("utf-8")
        return True
    except Exception:
        return False

def load_symbol_lazy(root: str, symbol: str) -> Optional[list]:
    """Look up ONE symbol from the lazy index — a single keyed read (~ms).
    Returns the list of locs, or None if the symbol isn't present/loaded."""
    try:
        import dbm
        path = _index_lazy_path(root)
        with dbm.open(path, "r") as db:
            raw = db.get(symbol.encode("utf-8"))
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None

def lazy_index_has(root: str) -> bool:
    """True if a lazy per-symbol index exists for this root."""
    try:
        import dbm
        with dbm.open(_index_lazy_path(root), "r"):
            return True
    except Exception:
        return False

def _read_source_from_loc(loc: dict, root: str) -> str:
    """Re-read a symbol's full source from disk using the stored byte range.
    Used by --full and the adaptive small-symbol path, since the persisted
    index no longer stores full source strings (they make it multi-GB)."""
    path = loc.get("path")
    sb, eb = loc.get("start_byte", 0), loc.get("end_byte", 0)
    if not path or not os.path.isabs(path):
        path = os.path.join(root, path) if path else None
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(sb)
                return fh.read(eb - sb)
        except (OSError, ValueError):
            pass
    return loc.get("source", "") or ""

def build_persistent_index(files: List[str], root: str, parallel: bool = False) -> dict:
    """Build a full byte-offset symbol index (all languages)."""
    return build_byte_index(files, root, parallel=parallel)

def build_knowledge_graph(files: List[str], root: str, parallel: bool = False) -> dict:
    """Build the knowledge-graph edges (call + import) for the persistent index.
    This is what lets heavy ops (--cross, --deadcode) load from the index
    instead of re-parsing every file — daemon-speed, no daemon.

    FUSED single-read: reads each file once and extracts call edges + import
    edges together, instead of the previous 3 separate full passes (symbols,
    call graph, imports each re-read every file). On a 95k-file repo this is
    the difference between ~9 min and ~3-4 min."""
    import multiprocessing as mp
    # Pass 1 (single read per file): extract call edges (raw) + imports together.
    # Reuse the pre-read text cache so nothing is re-read.
    texts = read_files_parallel(files, parallel=parallel)
    # defined function names (first pass, cheap — no full re-read, from cache)
    defined: dict = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in CALL_LANG_RULES:
            continue
        mod = module_name_of(f, root)
        defined[mod] = set()
        text = texts.get(f)
        if text is None:
            continue
        def_re, _ = CALL_LANG_RULES[ext]
        for m in re.finditer(def_re, text, re.MULTILINE):
            name = next((g for g in m.groups() if g), None)
            if name:
                defined[mod].add(name)
    all_defined: set = set()
    for s in defined.values():
        all_defined |= s
    # Fused parallel worker: calls + imports per file from cached text (no re-read)
    module_map = {module_name_of(f, root): f for f in files}
    if parallel and len(files) >= 50:
        args_list = [(f, texts.get(f), root, all_defined, module_map) for f in files]
        with mp.Pool() as pool:
            results = pool.map(_fused_kg_worker, args_list)
        calls = {}
        graph = {}
        for mod, c_edges, i_deps in results:
            if mod is None:
                continue
            if c_edges:
                calls[mod] = c_edges
            if i_deps:
                graph[mod] = i_deps
    else:
        calls = {}
        graph = {}
        for f in files:
            mod, c_edges, i_deps = _fused_kg_worker((f, texts.get(f), root, all_defined, module_map))
            if mod is None:
                continue
            if c_edges:
                calls[mod] = c_edges
            if i_deps:
                graph[mod] = i_deps
    return {
        "calls": {m: {c: sorted(s) for c, s in funcs.items()} for m, funcs in calls.items()},
        "imports": {m: sorted(deps) for m, deps in graph.items()},
    }

# --------------------------------------------------------------------------- #
# Graph precision (confidence + relationship types)
# --------------------------------------------------------------------------- #
# The leaders have ~7M edges; we have ~408k. Rather than chase raw count, we
# add what agents actually trust: confidence scores on edges and the
# relationship types (implements / extends / overrides) that make impact
# analysis correct.

def _py_relationships(files: List[str], root: str) -> dict:
    """Detect Python class relationships: {child: {'implements': [ifaces],
    'extends': [bases]}}. Uses AST across Python files."""
    out = {}
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
            if isinstance(node, ast.ClassDef):
                bases = []
                for b in node.bases:
                    if isinstance(b, ast.Name):
                        bases.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        bases.append(b.attr)
                if bases:
                    out[f"{mod}.{node.name}"] = {"extends": bases}
    return out

def render_precision(files: List[str], root: str, symbol: str) -> str:
    """Precision report for a symbol: its call edges with confidence,
    dependents, and class relationships. Confidence: 'definite' when the callee
    is resolved to a local module; 'maybe' when it's a dynamic/attr call."""
    index = build_byte_index(files, root)
    buf = io.StringIO()
    buf.write(f"# precision: {symbol}\n")
    locs = index.get(symbol)
    if not locs:
        buf.write("  Symbol not found.\n")
        return buf.getvalue()
    mod = locs[0]["module"]
    buf.write(f"  defined at {mod}:{locs[0].get('line', 1)}  [{locs[0].get('kind','function')}]\n\n")
    # relationships
    rel = _py_relationships(files, root)
    key = f"{mod}.{symbol}"
    if key in rel:
        buf.write("## Relationships\n")
        for ext in rel[key].get("extends", []):
            buf.write(f"  extends {ext}\n")
        buf.write("\n")
    # call edges with confidence
    buf.write("## Call edges (with confidence)\n")
    graph = build_call_graph_multi(files, root)
    mod_edges = graph.get(mod, {})
    if symbol in mod_edges:
        callees = mod_edges[symbol]
        for c in sorted(callees):
            # definite if the callee is a defined local symbol
            conf = "definite" if c in index else "maybe"
            buf.write(f"  {symbol} -> {c}  [{conf}]\n")
    else:
        buf.write("  (no resolved call edges from this symbol)\n")
    buf.write("\n## Dependents (what calls it)\n")
    for m, funcs in graph.items():
        for fn, callees in funcs.items():
            if symbol in callees:
                buf.write(f"  {m}.{fn}\n")
    buf.write("\n# Confidence: 'definite' = callee resolved to a local def; 'maybe' = dynamic/attr.\n")
    return buf.getvalue()
# --------------------------------------------------------------------------- #
# Optional C-accelerated core (codeloom_core)
# --------------------------------------------------------------------------- #
# Build once:  cc -O3 -o codeloom_core codeloom_core.c
# Then pass --engine c to index with the compiled scanner for Linux-kernel-class
# speed. Pure-Python remains the zero-dependency default.
_CORE_NAME = "codeloom_core"

def _find_core() -> Optional[str]:
    """Locate the compiled codeloom_core binary next to codeloom.py or on PATH."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, _CORE_NAME), os.path.join(here, _CORE_NAME + ".exe")]
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    import shutil
    return shutil.which(_CORE_NAME)

def _c_walk(root: str) -> List[str]:
    """List code files via the C core's fast walker (--list ROOT). Falls back
    to Python _walk on any error. Much faster than Python os.walk + gitignore
    matching on huge repos."""
    core = _find_core()
    if not core:
        return []
    import subprocess
    try:
        r = subprocess.run([core, "--list", root], capture_output=True, text=True, timeout=300)
    except Exception:
        return []
    out = [l for l in r.stdout.splitlines() if l.strip()]
    return [os.path.join(root, l.lstrip("./")) if not os.path.isabs(l) else l for l in out]

def _c_scan(files: List[str]) -> List[dict]:
    """Run the C core over files. Returns per-file dicts
    {file, symbols:[{name,kind}], imports:[...], calls:[...]}. Empty on error.
    Shards the file list across parallel C-core processes (the C core is
    single-threaded; on a 64k-file kernel repo this turns ~80s of scanning
    into ~15-20s across cores). Each shard uses stdin mode (no argv limits)."""
    core = _find_core()
    if not core:
        return []
    import subprocess, json as _json
    from concurrent.futures import ThreadPoolExecutor

    def _run_shard(shard):
        try:
            payload = "\n".join(shard) + "\n"
            r = subprocess.run([core], input=payload, capture_output=True, text=True, timeout=600)
            return r.stdout
        except Exception:
            return ""

    results = []
    # workers capped by cores; CODELOOM_CORES overrides (simulate low-core HW)
    workers = max(1, min(os.cpu_count() or 2, 8))
    try:
        workers = min(int(os.environ.get("CODELOOM_CORES", str(workers))), 8)
    except (ValueError, TypeError):
        pass
    workers = max(1, workers)
    if len(files) < 2000 or workers == 1:
        shards = [files]
    else:
        step = (len(files) + workers - 1) // workers
        shards = [files[i:i + step] for i in range(0, len(files), step)]
    outputs = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        outputs = list(ex.map(_run_shard, shards))
    for out in outputs:
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                results.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
    return results

def _c_symbol_index(files: List[str], root: str, scan: Optional[List[dict]] = None) -> dict:
    """Build a symbol index (name -> locs) using the C core's fast scan.
    Faster than Python parsing; used by --engine c. Snippet is the def line.
    `scan` is an optional pre-computed _c_scan() result (avoids re-scanning)."""
    idx: dict = {}
    if scan is None:
        scan = _c_scan(files)
    # precompute file->module ONCE per file (avoid 3.2M os.path.relpath calls)
    mod_map = {}
    for fr in scan:
        p = fr.get("file", "")
        if p:
            mod_map[p] = module_name_of(p, root)
    for fr in scan:
        path = fr.get("file", "")
        if not path:
            continue
        mod = mod_map.get(path) or module_name_of(path, root)
        for s in fr.get("symbols", []):
            name = s.get("name", "")
            if not name:
                continue
            idx.setdefault(name, []).append({
                "module": mod, "kind": "function", "line": 1,
                "sig": name, "tokens": 3,
            })
    return idx

def _c_kg(files: List[str], root: str, all_defined: set, scan: Optional[List[dict]] = None) -> dict:
    """Build the knowledge graph (calls + imports) using the C core, filtering
    callees to repo-defined symbols (like the Python fused worker). Much faster
    than tree-sitter for the call-edge pass on massive repos.
    `scan` is an optional pre-computed _c_scan() result (avoids re-scanning)."""
    calls: dict = {}
    graph: dict = {}
    module_map = {module_name_of(f, root): f for f in files}
    if scan is None:
        scan = _c_scan(files)
    # precompute file->module once per file (avoid per-symbol relpath)
    mod_map = {}
    for fr in scan:
        p = fr.get("file", "")
        if p:
            mod_map[p] = module_name_of(p, root)
    for fr in scan:
        path = fr.get("file", "")
        if not path:
            continue
        mod = mod_map.get(path) or module_name_of(path, root)
        # call edges: keep only targets defined in the repo
        c_edges = {}
        for c in fr.get("calls", []):
            caller = c.get("caller", "")
            if not caller:
                continue
            targets = [t for t in c.get("targets", [])
                       if t in all_defined and t != caller]
            if targets:
                c_edges[caller] = set(targets)
        if c_edges:
            calls[mod] = c_edges
        # imports: resolve to local modules
        deps = set()
        for imp in fr.get("imports", []):
            resolved = _resolve_import(imp, mod, root, module_map)
            if resolved and resolved != mod:
                deps.add(resolved)
        if deps:
            graph[mod] = deps
    return {
        "calls": {m: {c: sorted(s) for c, s in funcs.items()} for m, funcs in calls.items()},
        "imports": {m: sorted(d) for m, d in graph.items()},
    }

def save_persistent_index(root: str, index: dict, files: List[str], kg: Optional[dict] = None,
                          skip_json: bool = False) -> None:
    """Save the persistent index with per-file (mtime, size) for incremental
    refresh. Size is tracked because Windows mtime has ~2s resolution, so a
    quick append may not change mtime — but it always changes size.
    `kg` is the optional knowledge-graph edges (call + import).
    Writes a binary (marshal) copy for fast load at scale. `skip_json=True`
    (used by --engine c) omits the redundant JSON copy — the loader prefers
    marshal anyway — for a real win on 3M-symbol repos."""
    data = {
        "version": INDEX_VERSION,
        "root": root,
        "files": {f: (os.path.getmtime(f), os.path.getsize(f))
                  for f in files if os.path.isfile(f)},
        "symbols": index,
    }
    # strip full source strings from the persisted index — a 1M-symbol repo
    # makes them multi-GB and slow to load. Keep a signature line; --full
    # re-reads the file from the stored byte range on demand.
    for name in list(index.keys()):
        for loc in index[name]:
            src = loc.get("source", "")
            loc["sig"] = (src.split("\n")[0][:80] if src else name)
            loc.pop("source", None)
    if kg:
        data["kg"] = kg
    # also write the lazy per-symbol index for near-resident single-symbol lookups
    try:
        save_lazy_index(root, index)
    except Exception:
        pass
    if not skip_json:
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

def refresh_index_incremental(root: str, max_files: int) -> str:
    """Incremental 'daemon-less watcher' refresh. Detects which files changed
    since the last index (via stored mtime/size), re-indexes ONLY those, and
    updates the lazy per-symbol dbm store + the JSON index in place. Each call
    is cheap on a mostly-unchanged repo, so an agent can call it on-demand and
    always hit a current index without a full rebuild or a managed daemon."""
    pidx = load_persistent_index(root)
    gi = os.path.join(root, ".gitignore")
    rules = parse_gitignore(gi) if os.path.isfile(gi) else []
    # discover current files
    files: List[str] = []
    _walk(root, rules, max_files, files)
    changed = []
    removed = []
    old_meta = (pidx.get("files", {}) if pidx else {})
    new_meta = {}
    for f in files:
        try:
            cur = (os.path.getmtime(f), os.path.getsize(f))
        except OSError:
            continue
        new_meta[f] = cur
        if old_meta.get(f) != cur:
            changed.append(f)
    for f in old_meta:
        if not os.path.isfile(f):
            removed.append(f)
    if not changed and not removed:
        return "# watch: index already fresh — 0 files changed (no rebuild needed)\n"
    # re-index only the changed/new files; build a merged symbol index
    new_index = {}
    if changed:
        new_index = build_persistent_index(changed, root)
    # merge into existing index
    base_index = pidx.get("symbols", {}) if pidx else {}
    if removed:
        # drop symbols whose path is in a removed file
        removed_paths = set(removed)
        base_index = {name: [l for l in locs if l.get("path") not in removed_paths]
                      for name, locs in base_index.items()}
        base_index = {k: v for k, v in base_index.items() if v}
    merged = dict(base_index)
    for name, locs in new_index.items():
        merged[name] = locs
    # refresh the kg too (rebuild from full file list — cheap relative to symbol build)
    kg = build_knowledge_graph(files, root)
    # persist both formats
    data = {
        "version": INDEX_VERSION,
        "root": root,
        "files": new_meta,
        "symbols": merged,
        "kg": kg,
    }
    # strip full source from persisted locs (compact), like save_persistent_index
    for name in list(merged.keys()):
        for loc in merged[name]:
            src = loc.get("source", "")
            loc["sig"] = (src.split("\n")[0][:80] if src else name)
            loc.pop("source", None)
    try:
        with open(_index_path(root), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass
    try:
        import marshal
        with open(_index_bin_path(root), "wb") as f:
            marshal.dump(data, f)
    except OSError:
        pass
    try:
        save_lazy_index(root, merged)
    except Exception:
        pass
    n = len(changed)
    n_syms = sum(len(v) for v in merged.values())
    return (f"# watch: incremental refresh — {n} file(s) changed, "
            f"{len(removed)} removed\n"
            f"  {len(files)} files, {n_syms} symbols in updated index\n"
            f"  lazy per-symbol index updated in place — lookups stay near-resident\n")


def render_index(files: List[str], root: str, max_files: int, parallel: bool = False, engine: str = "py") -> str:
    """Build and save the persistent index + knowledge graph. Returns a summary.
    engine='c' uses the optional compiled C core for the symbol scan (much
    faster on 100k-file repos). Pure-Python ('py') is the default."""
    if engine == "c" and _find_core():
        scan = _c_scan(files)  # scan each file ONCE, reuse for symbols + kg
        index = _c_symbol_index(files, root, scan=scan)
        all_defined = set(index.keys())
        kg = _c_kg(files, root, all_defined, scan=scan)
    else:
        index = build_persistent_index(files, root, parallel=parallel)
        kg = build_knowledge_graph(files, root, parallel=parallel)
    save_persistent_index(root, index, files, kg=kg, skip_json=(engine == "c"))
    n_syms = sum(len(v) for v in index.values())
    n_edges = sum(len(v) for v in kg["calls"].values()) + sum(len(v) for v in kg["imports"].values())
    buf = io.StringIO()
    buf.write(f"# codeloom --index — built persistent index + knowledge graph ({engine} engine)\n")
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
# Preflight refactoring checks (--check-edit / --check-delete) — the moat
# --------------------------------------------------------------------------- #
# jcodemunch has check_edit_safe / check_delete_safe + reference tracking that
# tell an agent definitively when a rewrite will break dependents. codeloom's
# answer: a terminal GO/STOP verdict the agent can't loop past.

def preflight_check(files, root, symbol, action: str) -> str:
    """Check whether editing or deleting a symbol is safe. Returns a terminal
    verdict: GO or STOP, with the exact dependents that will break.

    action: 'edit' or 'delete'.
    - edit: CHECK unless no callers reference the symbol.
    - delete: GO only if nothing references the symbol at all."""
    buf = io.StringIO()
    buf.write(f"# codeloom --check-{action}: {symbol}\n\n")
    calls = build_call_graph_multi(files, root)
    refs = set()
    for mod, funcs in calls.items():
        for caller, callees in funcs.items():
            if symbol in callees:
                refs.add(f"{mod}.{caller}")
        if symbol in funcs:
            refs.add(f"{mod}.{symbol}")
    idx = build_symbol_index(files, root)
    defined_mods = set()
    for loc in idx.get(symbol, []):
        defined_mods.add(loc["module"])
    dependent_refs = sorted(r for r in refs if not any(r.startswith(m) for m in defined_mods))

    if action == "delete":
        if dependent_refs:
            buf.write("VERDICT: STOP — do not delete.\n")
            buf.write(f"  {symbol} is referenced by {len(dependent_refs)} caller(s).\n")
            for r in dependent_refs[:15]:
                buf.write(f"    - {r}\n")
            buf.write("  Deleting will break these. Fix or migrate them first.\n")
        else:
            buf.write("VERDICT: GO — safe to delete.\n")
            buf.write(f"  No callers reference {symbol}. It's dead code or isolated.\n")
        return buf.getvalue()

    if dependent_refs:
        buf.write(f"VERDICT: CHECK — {len(dependent_refs)} caller(s) depend on {symbol}.\n")
        buf.write(f"  Editing {symbol} may break these callers:\n")
        for r in dependent_refs[:15]:
            buf.write(f"    - {r}\n")
        buf.write("  PROCEED ONLY IF the change is backward-compatible (same name + signature).\n")
    else:
        buf.write("VERDICT: GO — safe to edit.\n")
        buf.write("  No callers reference {symbol}. The change is isolated.\n")
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

# --------------------------------------------------------------------------- #
# Edit-relevance ranking (--task/--plan/--pack) — the moat
# --------------------------------------------------------------------------- #
# The plan's Phase 2: rank by EDIT relevance, not keyword overlap.
#   1. ANCHOR: extract anchor symbols from the task text ("fix the login bug"
#      -> anchor = {login})
#   2. WALK: from each anchor, walk the call graph BOTH directions
#      - callers (what calls login?)  -> "what breaks"
#      - callees (what does login call?) -> "what it needs"
#      - depth 1-2, weighted by distance
#   3. RANK: score = f(anchor_distance, keyword_hits, dependent_count)
#      - anchor_distance is DOMINANT (this is the fix)
#      - keyword_hits is a tiebreaker, not the primary signal
#   4. RESULT: session.py ranks above constants.py for "fix the login bug"
#      because it's on login's call path, even with fewer word hits.
#
# This is the difference between "where does 'login' appear" and "what code
# actually runs when a login happens." The second is what an agent needs.

def _anchor_symbols(task: str) -> set:
    """Extract anchor symbols from the task text. These are the nouns/verbs
    that name the code the task is about (e.g. 'login', 'retry', 'auth')."""
    toks = _tokenize(task)
    # drop generic task words that aren't code anchors
    stop = {"fix", "the", "a", "an", "bug", "add", "implement", "change", "update",
            "remove", "refactor", "make", "this", "that", "for", "in", "to", "of",
            "and", "or", "with", "on", "at", "by", "from", "into", "new", "feature",
            "support", "handle", "error", "issue", "problem", "work", "code", "logic"}
    return {t for t in toks if t not in stop}

def edit_relevance(files: List[str], root: str, task: str, top: int = 10) -> List[dict]:
    """Rank modules by EDIT relevance: anchor symbols walked through the call
    graph both directions, weighted by distance. This is the moat — it ranks
    the execution path, not the keyword matches."""
    anchors = _anchor_symbols(task)
    if not anchors:
        return task_relevance(files, root, task, top)  # fallback
    # build the call graph (module -> {func -> set(callees)})
    calls = build_call_graph_multi(files, root)
    # build a reverse index: which modules define each anchor symbol
    anchor_modules = {}  # anchor -> set of modules defining it
    for mod, funcs in calls.items():
        for func in funcs:
            if func in anchors:
                anchor_modules.setdefault(func, set()).add(mod)
    if not anchor_modules:
        return task_relevance(files, root, task, top)  # no anchors found in code
    # walk the call graph from each anchor module, both directions
    # distance 0 = the anchor module itself, 1 = direct callers/callees, 2 = transitive
    module_dist = {}  # module -> min distance from any anchor
    for anchor, mods in anchor_modules.items():
        for m in mods:
            module_dist[m] = min(module_dist.get(m, 99), 0)
            # callees (what the anchor calls) — depth 1-2
            for caller, callees in calls.get(m, {}).items():
                for callee in callees:
                    # find modules defining callee
                    for cm, funcs in calls.items():
                        if callee in funcs:
                            module_dist[cm] = min(module_dist.get(cm, 99), 1)
            # callers (what calls the anchor) — depth 1-2
            for cm, funcs in calls.items():
                for caller, callees in funcs.items():
                    if any(anchor in callees for anchor in anchors):
                        module_dist[cm] = min(module_dist.get(cm, 99), 1)
    # score: anchor_distance dominant, keyword_hits tiebreaker, centrality bonus
    task_tokens = _tokenize(task)
    scored = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in LANG_RULES and ext not in IMPORT_LANG_RULES:
            continue
        mod = module_name_of(f, root)
        dist = module_dist.get(mod, 99)
        if dist == 99:
            continue  # not on any anchor's call path
        toks = _module_tokens(f)
        overlap = len(task_tokens & toks)
        # distance is dominant: 0 -> 100, 1 -> 60, 2 -> 30
        dist_score = {0: 100, 1: 60, 2: 30}.get(dist, 10)
        score = dist_score + overlap * 2 + min(len(reachable(build_graph_multi(files, root), mod, "in")), 10)
        scored.append({"module": mod, "path": f, "score": score,
                       "overlap": overlap, "centrality": dist, "anchor_dist": dist})
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
    results = edit_relevance(files, root, task, top)
    buf = io.StringIO()
    buf.write(f"# task: {task}\n")
    if not results:
        buf.write("No modules matched the task. Try different keywords.\n")
        return buf.getvalue()
    buf.write(f"Top {len(results)} relevant modules (by edit relevance — anchor distance + call path):\n\n")
    for i, r in enumerate(results, 1):
        dist = r.get("anchor_dist", "?")
        buf.write(f"{i}. {r['module']}  (score {r['score']}, {r['overlap']} keyword hits, "
                  f"anchor distance {dist})\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --plan: agent-native prioritized reading plan
# --------------------------------------------------------------------------- #

def build_plan(files: List[str], root: str, task: str, top: int = 8) -> str:
    """Emit a prioritized 'read these files, in this order' plan for a task."""
    results = edit_relevance(files, root, task, top)
    buf = io.StringIO()
    buf.write(f"# plan: {task}\n\n")
    if not results:
        buf.write("No relevant modules found. Refine the task description.\n")
        return buf.getvalue()
    buf.write("Read these files, in this order, to understand the task:\n\n")
    for i, r in enumerate(results, 1):
        dist = r.get("anchor_dist", "?")
        buf.write(f"{i}. {r['path']}\n")
        buf.write(f"   why: on the task's call path (anchor distance {dist}), "
                  f"{r['overlap']} keyword match(es)\n")
    buf.write("\nThen run `codeloom --impact <file>` on the file you plan to change.\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --pack: single-shot context window optimizer (the moat feature)
# --------------------------------------------------------------------------- #

def render_pack(files: List[str], root: str, task: str, top: int = 8,
                include_symbols: bool = True) -> str:
    """Emit a single-shot, code-embedded task brief for a task. This is the
    moat feature: a self-contained, context-window-sized brief with the ACTUAL
    code embedded — not a ranked list. An agent pastes this once and completes
    the task with zero per-query retrieval.

    Output contract (2-4k tokens for a normal task):
      # TASK: <task>
      ## 1. READ THESE, IN ORDER
      ## 2. THE RELEVANT CODE (byte-precise, embedded, capped ~40 lines/symbol)
      ## 3. IMPACT — what breaks if you change each
      ## 4. CALL PATH — how the task flows
      ## 5. SAFE TO TOUCH (deadcode filtered to this task's modules)

    This is the feature jcodemunch can't do: it's retrieval-shaped, so its
    assemble_task_context returns names, not code. codeloom embeds the code."""
    results = edit_relevance(files, root, task, top)
    buf = io.StringIO()
    buf.write(f"# TASK: {task}\n")
    buf.write(f"# Single-shot, code-embedded task brief. {len(results)} relevant module(s).\n\n")

    if not results:
        buf.write("No modules matched the task. Refine the task description.\n")
        return buf.getvalue()

    # 1. Reading order (the plan) — edit-relevance ranked
    buf.write("## 1. READ THESE, IN ORDER\n")
    for i, r in enumerate(results, 1):
        dist = r.get("anchor_dist", "?")
        buf.write(f"  {i}. {r['path']}  (anchor distance {dist}, {r['overlap']} keyword hits)\n")
    buf.write("\n")

    # 2. THE RELEVANT CODE — byte-precise, embedded, capped
    buf.write("## 2. THE RELEVANT CODE (byte-precise, embedded)\n")
    for r in results:
        path = r["path"]
        ext = os.path.splitext(path)[1].lower()
        if ext not in CALL_LANG_RULES:
            continue
        mod = r["module"]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        def_re, _ = CALL_LANG_RULES.get(ext, (None, None))
        if def_re is None:
            continue
        # embed each top-level symbol, capped at ~40 lines / ~200 tokens
        for m in re.finditer(def_re, text, re.MULTILINE):
            name = next((g for g in m.groups() if g), None)
            if not name:
                continue
            line = text[:m.start()].count("\n") + 1
            # extract the symbol body (up to ~40 lines)
            lines = text[m.start():].splitlines()
            body = "\n".join(lines[:40])
            if estimate_tokens(body) > 200:
                # too big — signature + docstring + pointer
                sig = lines[0][:80] if lines else name
                buf.write(f"  {mod}.{name}  [{ext[1:]} :{line}]  (~{estimate_tokens(body)} tokens, too big to embed)\n")
                buf.write(f"    {sig}\n")
                buf.write(f"    -> run `codeloom --get-symbol {name} --full` for the full source\n")
            else:
                buf.write(f"  {mod}.{name}  [{ext[1:]} :{line}]\n")
                buf.write(f"  ```\n{body}\n  ```\n")
        buf.write("\n")

    # 3. IMPACT — what breaks if you change each
    buf.write("## 3. IMPACT (what breaks if you change each)\n")
    graph = build_graph_multi(files, root)
    for r in results:
        mod = r["module"]
        try:
            impact = render_impact(graph, root, mod)
            for line in impact.splitlines():
                if line.startswith("risk:") or line.startswith("  src") or line.startswith("  tests"):
                    buf.write(f"  {mod}: {line.strip()}\n")
        except Exception:
            pass
    buf.write("\n")

    # 4. CALL PATH — how the task flows
    buf.write("## 4. CALL PATH (how the task flows)\n")
    calls = build_call_graph_multi(files, root)
    for r in results[:3]:
        mod = r["module"]
        if mod in calls:
            for caller, callees in list(calls[mod].items())[:5]:
                buf.write(f"  {mod}.{caller} -> {', '.join(sorted(callees))}\n")
    buf.write("\n")

    # 5. SAFE TO TOUCH — deadcode filtered to this task's modules
    buf.write("## 5. SAFE TO TOUCH (deadcode in the task's modules)\n")
    try:
        dead = dead_code(files, root)
        task_mods = {r["module"] for r in results}
        for d in dead:
            if d["symbol"].split(".")[0] in task_mods:
                buf.write(f"  {d['symbol']}\n")
    except Exception:
        pass
    buf.write("\n")

    buf.write("## How to use\n")
    buf.write("This brief is the complete context for the task. Read it once, then work.\n")
    buf.write("For the full source of any symbol, run `codeloom --get-symbol <name> --full`.\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# loom_context — the intent engine (the keystone)
# --------------------------------------------------------------------------- #
# Instead of exposing 40 tools, expose ONE: loom_context(task). It internally
# decides what to read and returns LAYERED context (overview -> files ->
# symbols -> code -> git + memory), so the agent gets everything for a task
# in one call instead of orchestrating retrieval itself.

# --- repository memory (persistent) -----------------------------------------

def _memory_dir(root: str) -> str:
    d = os.path.join(root, ".codeloom-memory")
    if not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return d

def memory_read(root: str) -> str:
    """Read the repository memory (architecture/patterns/decisions/conventions)."""
    buf = io.StringIO()
    d = _memory_dir(root)
    for name in ("ARCHITECTURE", "DECISIONS", "PATTERNS", "CONVENTIONS"):
        p = os.path.join(d, name + ".md")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    buf.write(f"### {name}\n{fh.read()}\n")
            except OSError:
                pass
    return buf.getvalue()

def memory_remember(root: str, section: str, note: str) -> str:
    """Append a note to the repo memory. section in {ARCHITECTURE, DECISIONS, PATTERNS, CONVENTIONS}."""
    section = section.upper()
    if section not in ("ARCHITECTURE", "DECISIONS", "PATTERNS", "CONVENTIONS"):
        section = "DECISIONS"
    d = _memory_dir(root)
    p = os.path.join(d, section + ".md")
    try:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"- {note}\n")
        return f"remembered: {section} <- {note}"
    except OSError as e:
        return f"memory write failed: {e}"

# git intelligence ---------------------------------------------------------
def git_churn(root: str, files: List[str], limit: int = 8) -> str:
    """Most-churned files (most git commits touching them) — instability signal."""
    if not os.path.isdir(os.path.join(root, ".git")):
        return "no git repo"
    import subprocess
    buf = io.StringIO()
    buf.write("## Git churn (most-edited files)\n")
    counts = {}
    for f in files[:2000]:
        rel = os.path.relpath(f, root)
        try:
            r = subprocess.run(["git", "-C", root, "log", "--oneline", "--", rel],
                               capture_output=True, text=True, timeout=10)
            n = len(r.stdout.strip().splitlines()) if r.stdout.strip() else 0
            if n > 0:
                counts[rel] = n
        except Exception:
            continue
    top = sorted(counts.items(), key=lambda x: -x[1])[:limit]
    for rel, n in top:
        buf.write(f"  {rel} ({n} commits)\n")
    return buf.getvalue()

def render_loom_context(files: List[str], root: str, task: str, max_files: int = 5000) -> str:
    """The intent engine: layered context for a task in one call.
    Returns Overview -> Important files -> Relevant symbols -> Code ->
    Impact -> Git churn -> Repository memory."""
    buf = io.StringIO()
    buf.write(f"# loom_context: {task}\n")
    buf.write("# Layered context for the task — read top to bottom, descend only as deep as you need.\n\n")

    # Layer 0: overview (map)
    try:
        m = build_map(root, True, max_files)
        buf.write("## Overview\n")
        buf.write(f"  {m['file_count']} files, {len(m['entry_points'])} entry points\n")
        for e in m["entry_points"][:5]:
            buf.write(f"    {os.path.relpath(e, root)}\n")
        buf.write("\n")
    except Exception:
        pass

    # Layer 1: important files (edit-relevance)
    rel = []
    try:
        rel = edit_relevance(files, root, task, top=8)
        buf.write("## Important files (edit-relevance ranked)\n")
        for i, r in enumerate(rel, 1):
            buf.write(f"  {i}. {r['path']}\n")
        buf.write("\n")
    except Exception:
        pass

    # Layer 2: the relevant code (pack — embedded, capped)
    try:
        buf.write(render_pack(files, root, task, top=5))
        buf.write("\n")
    except Exception:
        pass

    # Layer 3: git context (churn on important files)
    try:
        buf.write(git_churn(root, [r["path"] for r in rel] if rel else files))
        buf.write("\n")
    except Exception:
        pass

    # Layer 4: repository memory
    try:
        mem = memory_read(root)
        if mem.strip():
            buf.write("## Repository memory\n")
            buf.write(mem + "\n")
    except Exception:
        pass

    buf.write("## How to use\n")
    buf.write("This is the complete context for the task. Descend only as deep as you need.\n")
    return buf.getvalue()

# --ask: one-shot complete task brief (loom + impact + touch list) -------------
def render_ask(files: List[str], root: str, task: str, max_files: int = 5000) -> str:
    """One command for an agent: the complete task brief. Loom layered context
    PLUS the impact/blast-radius (what breaks if you change these files) PLUS
    a concrete 'files to touch' checklist. This is the 'just tell me what to
    do' answer — the highest-value single call."""
    buf = io.StringIO()
    buf.write(f"# ASK: {task}\n")
    buf.write("# Complete task brief — read this, then act. No other setup needed.\n\n")

    # 1. layered context (overview -> important files -> code -> git -> memory)
    try:
        buf.write(render_loom_context(files, root, task, max_files))
    except Exception:
        pass
    buf.write("\n")

    # 2. impact / blast radius (what breaks if you touch the relevant files)
    try:
        rel = edit_relevance(files, root, task, top=6)
        graph = build_graph_multi(files, root)
        buf.write("## Blast radius (what breaks if you edit these)\n")
        seen = set()
        for r in rel[:6]:
            mod = r.get("module") or module_name_of(r["path"], root)
            if mod in seen:
                continue
            seen.add(mod)
            try:
                imp = impact_analysis(graph, mod)
                dependents = imp.get("impacted", [])
                buf.write(f"  {mod}: {len(dependents)} dependent module(s)\n")
                for d in dependents[:4]:
                    buf.write(f"    <- {d}\n")
            except Exception:
                buf.write(f"  {mod}\n")
        buf.write("\n")
    except Exception:
        pass

    # 3. concrete checklist
    try:
        buf.write("## Files to touch (checklist)\n")
        for i, r in enumerate(edit_relevance(files, root, task, top=6), 1):
            buf.write(f"  [ ] {r['path']}\n")
        buf.write("\n")
    except Exception:
        pass

    buf.write("## Next step\n")
    buf.write("Edit the checklist files. Use --check-edit before each change and --rename for renames.\n")
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
    ".svelte": (r"^\s*(?:export\s+)?function\s+(\w+)|^\s*(?:export\s+)?const\s+(\w+)\s*=\s*[\w(]*=>", r"\b(\w+)\s*\("),
    ".vue": (r"^\s*(?:export\s+)?function\s+(\w+)|^\s*(?:export\s+)?const\s+(\w+)\s*=\s*[\w(]*=>", r"\b(\w+)\s*\("),
    ".astro": (r"^\s*(?:export\s+)?function\s+(\w+)|^\s*(?:export\s+)?const\s+(\w+)\s*=\s*[\w(]*=>", r"\b(\w+)\s*\("),
    ".scala": (r"^\s*(?:public|private|protected|def|val|var|object|class|case|)\s+def\s+(\w+)|^\s*def\s+(\w+)", r"\b(\w+)\s*\("),
    ".erl": (r"^\s*(\w+)\s*\([^)]*\)\s*->", r"\b(\w+)\s*\("),
    ".sol": (r"^\s*(?:public|private|internal|external|function)\s+function\s+(\w+)|^\s*function\s+(\w+)", r"\b(\w+)\s*\("),
    ".nix": (r"^\s*(\w+)\s*=\s*(?:imports|functions\.[\w.]+)|^\s*(\w+)\s*=", r"\b(\w+)\s*\("),
    ".tf": (r"^\s*(?:resource|data|variable|output|module|locals|provider)\s+[\"\w]+\s+[\"\w]+", r"\b(\w+)\s*\("),
    ".hcl": (r"^\s*(?:resource|data|variable|output|module|locals|provider)\s+[\"\w]+\s+[\"\w]+", r"\b(\w+)\s*\("),
    ".pas": (r"^\s*(?:function|procedure)\s+(\w+)", r"\b(\w+)\s*\("),
    ".r": (r"^\s*(\w+)\s*<-\s*function|^\s*function\s*\([^)]*\)\s*\{", r"\b(\w+)\s*\("),
    ".luau": (r"^\s*(?:local\s+)?function\s+(\w+)", r"\b(\w+)\s*\("),
    ".cfml": (r"^\s*<\s*cffunction\s+name\s*=\s*[\"'](\w+)", r"\b(\w+)\s*\("),
    ".cob": (r"^\s*(\w+)\s+SECTION\.|^\s*IDENTIFICATION\s+", r"\b(\w+)\s*\("),
    ".cbl": (r"^\s*(\w+)\s+SECTION\.|^\s*IDENTIFICATION\s+", r"\b(\w+)\s*\("),
    ".vb": (r"^\s*(?:Public|Private|Protected|Friend|Shared)\s+Function\s+(\w+)", r"\b(\w+)\s*\("),
    ".cc": (r"^\s*(?:static\s+)?[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".cxx": (r"^\s*(?:static\s+)?[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".cu": (r"^\s*(?:__global__|__device__|__host__)?\s*[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    # ---- broad breadth: same generic structural extraction, no per-language setup ----
    ".jsm": (r"^\s*(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()", r"\b(\w+)\s*\("),
    ".es6": (r"^\s*(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()", r"\b(\w+)\s*\("),
    ".es":  (r"^\s*(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()", r"\b(\w+)\s*\("),
    ".qwik":(r"^\s*(?:export\s+)?function\s+(\w+)|^\s*(?:export\s+)?const\s+(\w+)\s*=\s*[\w(]*=>", r"\b(\w+)\s*\("),
    ".twig":(r"^\s*{%\s*(?:macro|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".ejs": (r"^\s*<%[=-]?\s*(?:function\s+(\w+)|const\s+(\w+))", r"\b(\w+)\s*\("),
    ".hbs": (r"^\s*{{\s*!(?:function\s+(\w+))", r"\b(\w+)\s*\("),
    ".pug": (r"^\s*mixin\s+(\w+)", r"\b(\w+)\s*\("),
    ".pl":  (r"^\s*sub\s+(\w+)", r"\b(\w+)\s*\("),
    ".pm":  (r"^\s*sub\s+(\w+)", r"\b(\w+)\s*\("),
    ".pyw": (r"^\s*(?:async\s+)?def\s+(\w+)", r"\b(\w+)\s*\("),
    ".pyi": (r"^\s*(?:async\s+)?def\s+(\w+)", r"\b(\w+)\s*\("),
    ".rbw": (r"^\s*def\s+(\w+)", r"\b(\w+)\s*\("),
    ".rake":(r"^\s*task\s+[\"']?(\w+)", r"\b(\w+)\s*\("),
    ".gemspec": (r"^\s*(\w+)\s*=\s*[\"'][^\"']+[\"']", r"\b(\w+)\s*\("),
    ".hrl": (r"^\s*(\w+)\s*\([^)]*\)\s*->", r"\b(\w+)\s*\("),
    ".eex": (r"^\s*<%s?\s*(?:def\s+(\w+)|defp\s+(\w+))", r"\b(\w+)\s*\("),
    ".leex":(r"^\s*<%s?\s*(?:def\s+(\w+)|defp\s+(\w+))", r"\b(\w+)\s*\("),
    ".heex":(r"^\s*<%\s*(?:def\s+(\w+)|defp\s+(\w+))", r"\b(\w+)\s*\("),
    ".c++": (r"^\s*(?:static\s+)?[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".hh":  (r"^\s*(?:static\s+)?[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".hxx": (r"^\s*(?:static\s+)?[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".ino": (r"^\s*(?:void|int|float|bool|String|char)\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".rlib":(r"^\s*(?:pub\s+)?fn\s+(\w+)", r"\b(\w+)\s*\("),
    ".goc": (r"^\s*func\s+(?:\s*\([^)]*\)\s*)?(\w+)", r"\b(\w+)\s*\("),
    ".m":   (r"^\s*[-+]?\s*\([\w\*\s,]+\)\s*(\w+)\s*(?::|;|\{)|^\s*def\s+(\w+)", r"\b(\w+)\s*\("),
    ".mm":  (r"^\s*[-+]\s*\([\w\s\*,]+\)\s*(\w+)\s*(?::|;|\{)", r"\b(\w+)\s*\("),
    ".cuh": (r"^\s*(?:__global__|__device__)?\s*[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".s":   (r"^\s*(\w+)\s*:", r"\b(\w+)\s*\("),
    ".asm": (r"^\s*(\w+)\s*:\s*$", r"\b(\w+)\s*\("),
    ".d":   (r"^\s*(?:void|int|auto|string|bool|double)\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".ada": (r"^\s*(?:procedure|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".adb": (r"^\s*(?:procedure|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".ads": (r"^\s*(?:procedure|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".f":   (r"^\s*(?:subroutine|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".f90": (r"^\s*(?:subroutine|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".f95": (r"^\s*(?:subroutine|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".f03": (r"^\s*(?:subroutine|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".f08": (r"^\s*(?:subroutine|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".v":   (r"^\s*(?:module|function|task)\s+(\w+)", r"\b(\w+)\s*\("),
    ".sv":  (r"^\s*(?:module|function|task)\s+(\w+)", r"\b(\w+)\s*\("),
    ".vh":  (r"^\s*(?:entity|architecture)\s+(\w+)", r"\b(\w+)\s*\("),
    ".vhd": (r"^\s*(?:entity|architecture)\s+(\w+)", r"\b(\w+)\s*\("),
    ".sc":  (r"^\s*def\s+(\w+)", r"\b(\w+)\s*\("),
    ".groovy": (r"^\s*(?:def|void|int|String)\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".gradle": (r"^\s*(?:def|void)\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".clj": (r"^\s*\(defn\s+(\w+)", r"\b(\w+)\s*\("),
    ".cljs":(r"^\s*\(defn\s+(\w+)", r"\b(\w+)\s*\("),
    ".cljc":(r"^\s*\(defn\s+(\w+)", r"\b(\w+)\s*\("),
    ".edn": (r"^\s*(\w+)\s+:", r"\b(\w+)\s*\("),
    ".fs":  (r"^\s*let\s+(\w+)\s*=\s*", r"\b(\w+)\s*\("),
    ".fsx": (r"^\s*let\s+(\w+)\s*=\s*", r"\b(\w+)\s*\("),
    ".fsi": (r"^\s*val\s+(\w+)", r"\b(\w+)\s*\("),
    ".fsharp": (r"^\s*let\s+(\w+)\s*=\s*", r"\b(\w+)\s*\("),
    ".razor": (r"^\s*@(?:functions|code)\s*\{", r"\b(\w+)\s*\("),
    ".plsql": (r"^\s*(?:CREATE|create)\s+OR\s+REPLACE\s+(?:PROCEDURE|FUNCTION|PACKAGE)\s+(\w+)", r"\b(\w+)\s*\("),
    ".pgsql": (r"^\s*(?:CREATE|create)\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+(\w+)", r"\b(\w+)\s*\("),
    ".psql": (r"^\s*(?:CREATE|create)\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+(\w+)", r"\b(\w+)\s*\("),
    ".gql":  (r"^\s*(?:type|schema|mutation|query)\s+(\w+)", r"\b(\w+)\s*\("),
    ".graphql": (r"^\s*(?:type|schema|mutation|query)\s+(\w+)", r"\b(\w+)\s*\("),
    ".proto":(r"^\s*(?:message|service|enum)\s+(\w+)", r"\b(\w+)\s*\("),
    ".tfvars": (r"^\s*(\w+)\s*=\s*", r"\b(\w+)\s*\("),
    ".bzl":  (r"^\s*def\s+(\w+)", r"\b(\w+)\s*\("),
    ".mk":   (r"^\s*(\w+)\s*:\s*$", r"\b(\w+)\s*\("),
    ".cmake":(r"^\s*function\s*\(\s*(\w+)", r"\b(\w+)\s*\("),
    ".dockerfile": (r"^\s*(?:FROM|CMD|ENTRYPOINT|RUN)\s+(\S+)", r"\b(\w+)\s*\("),
    ".bash": (r"^\s*(\w+)\s*\(\s*\)\s*\{", r"\b(\w+)\s*\("),
    ".zsh":  (r"^\s*(\w+)\s*\(\s*\)\s*\{", r"\b(\w+)\s*\("),
    ".ksh":  (r"^\s*(\w+)\s*\(\s*\)\s*\{", r"\b(\w+)\s*\("),
    ".fish": (r"^\s*function\s+(\w+)", r"\b(\w+)\s*\("),
    ".ps1":  (r"^\s*function\s+(\w+)", r"\b(\w+)\s*\("),
    ".bat":  (r"^\s*:(label)", r"\b(\w+)\s*\("),
    ".cmd":  (r"^\s*:(label)", r"\b(\w+)\s*\("),
    ".awk":  (r"^\s*function\s+(\w+)", r"\b(\w+)\s*\("),
    ".sed":  (r"^\s*(\w+)\s*:", r"\b(\w+)\s*\("),
    ".rmd":  (r"^\s*```\s*\{r\s*(\w*)", r"\b(\w+)\s*\("),
    ".jl":   (r"^\s*function\s+(\w+)", r"\b(\w+)\s*\("),
    ".octave": (r"^\s*function\s+(\w+)", r"\b(\w+)\s*\("),
    ".scilab": (r"^\s*function\s+(\w+)", r"\b(\w+)\s*\("),
    ".pco":  (r"^\s*(?:PROCEDURE|FUNCTION)\s+(\w+)", r"\b(\w+)\s*\("),
    ".cobol":(r"^\s*(\w+)\s+SECTION\.|^\s*IDENTIFICATION\s+", r"\b(\w+)\s*\("),
    ".for":  (r"^\s*(?:subroutine|function)\s+(\w+)", r"\b(\w+)\s*\("),
    ".pp":   (r"^\s*(?:function|procedure)\s+(\w+)", r"\b(\w+)\s*\("),
    ".dpr":  (r"^\s*(?:function|procedure)\s+(\w+)", r"\b(\w+)\s*\("),
    ".nw":   (r"^\s*(\w+)\s*=", r"\b(\w+)\s*\("),
    ".e":    (r"^\s*(\w+)\s*:", r"\b(\w+)\s*\("),
    ".eq":   (r"^\s*(\w+)\s*=", r"\b(\w+)\s*\("),
    ".coffee": (r"^\s*(\w+)\s*=\s*\(?\s*\)?\s*->", r"\b(\w+)\s*\("),
    ".litcoffee": (r"^\s*(\w+)\s*=\s*\(?\s*\)?\s*->", r"\b(\w+)\s*\("),
    ".co":   (r"^\s*(\w+)\s*=", r"\b(\w+)\s*\("),
    ".tcl":  (r"^\s*proc\s+(\w+)", r"\b(\w+)\s*\("),
    ".jq":   (r"^\s*def\s+(\w+)", r"\b(\w+)\s*\("),
    ".pas":  (r"^\s*(?:function|procedure)\s+(\w+)", r"\b(\w+)\s*\("),
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

# The codeloom-map CI workflow that --install-agents writes (set-and-forget).
# Runs --pack on every PR and posts the task brief as a comment.
CI_WORKFLOW = """name: codeloom-map

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  map:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Run codeloom --pack on the PR
        id: pack
        run: |
          TASK="${{ github.event.pull_request.title }}"
          if [ -z "$TASK" ]; then TASK="review this pull request"; fi
          python codeloom.py --pack "$TASK" . > /tmp/codeloom-brief.md 2>&1 || true
      - name: Post the brief as a PR comment
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            let brief = fs.readFileSync('/tmp/codeloom-brief.md', 'utf8');
            if (brief.length > 60000) brief = brief.slice(0, 60000) + '\\n... (truncated)';
            const body = '## codeloom task brief\\n\\n' + brief;
            await github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: body
            });
"""

def install_agents(root: str) -> str:
    """Write or update AGENTS.md with a codeloom instruction block, and write
    the codeloom-map CI workflow (set-and-forget onboarding)."""
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
            msg = f"updated {path}"
        else:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n" + block)
            msg = f"appended to {path}"
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(block)
        msg = f"created {path}"
    # write the codeloom-map CI workflow (set-and-forget)
    ci_path = os.path.join(root, ".github", "workflows", "codeloom-map.yml")
    try:
        os.makedirs(os.path.dirname(ci_path), exist_ok=True)
        with open(ci_path, "w", encoding="utf-8") as f:
            f.write(CI_WORKFLOW)
        msg += f"; wrote {ci_path}"
    except OSError:
        pass
    return msg

# --------------------------------------------------------------------------- #
# Per-agent MCP install (--install-agent)
# --------------------------------------------------------------------------- #

def _mcp_config(agent: str, core_path: str) -> str:
    """Return the exact MCP server config snippet for an agent. `core_path`
    is the absolute path to codeloom-mcp.py (or codeloom.py if MCP is inline)."""
    # codeloom-mcp.py is the MCP server entrypoint.
    server_py = os.path.join(os.path.dirname(core_path), "codeloom-mcp.py")
    if not os.path.isfile(server_py):
        server_py = core_path  # fallback
    a = agent.lower()
    if a == "claude":
        return f'''"codeloom": {{
  "type": "stdio",
  "command": "python3",
  "args": ["{server_py}"]
}}'''
    if a == "cursor":
        return f'''"codeloom": {{
  "command": "python3",
  "args": ["{server_py}"]
}}'''
    if a == "codex":
        return f'''{{
  "mcpServers": {{
    "codeloom": {{
      "command": "python3",
      "args": ["{server_py}"]
    }}
  }}
}}'''
    if a == "gemini":
        return f'''"codeloom": {{
  "command": "python3",
  "args": ["{server_py}"]
}}'''
    if a == "opencode":
        return f'''{{
  "codeloom": {{
    "command": "python3",
    "args": ["{server_py}"]
  }}
}}'''
    return f'''"codeloom": {{
  "command": "python3",
  "args": ["{server_py}"]
}}'''

def install_agent_config(agent: str, core_path: str) -> str:
    """Write/print the MCP config for a specific agent. Returns the config
    text to add to the agent's settings (Claude Desktop, Cursor, etc.)."""
    return _mcp_config(agent, core_path)

def detect_agent() -> Optional[str]:
    """Best-effort detect which coding agent's config directory is present."""
    home = os.path.expanduser("~")
    cands = [
        ("claude", os.path.join(home, ".claude")),
        ("cursor", os.path.join(home, ".cursor")),
        ("codex", os.path.join(home, ".codex")),
        ("gemini", os.path.join(home, ".gemini")),
        ("opencode", os.path.join(home, ".config", "opencode")),
    ]
    for name, path in cands:
        if os.path.isdir(path):
            return name
    return None

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

# --- session memory: track already-read files/symbols to avoid re-reading -----

def render_seen(root: str) -> str:
    """Report which files/symbols were already accessed this session, so the
    agent can skip re-reading them. Reads the session log's command args and
    infers the touched paths/symbols."""
    import json as _json
    path = _session_path(root)
    buf = io.StringIO()
    buf.write("# codeloom --seen (session memory — already-read context)\n")
    if not os.path.isfile(path):
        buf.write("  No session log yet. Run `codeloom --session` to track reads.\n")
        return buf.getvalue()
    files = set()
    symbols = set()
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
                cmd = e.get("cmd", "")
                # extract .py/.js/.go/etc file paths
                import re as _re
                for m in _re.finditer(r"[\w./]+\.(?:py|js|ts|go|rs|java|rb|c|h|cpp)", cmd):
                    files.add(m.group(0))
                # extract likely symbol tokens (from --get-symbol/--read/--search)
                if any(k in cmd for k in ("--get-symbol", "--read", "--search", "--explain")):
                    for m in _re.finditer(r"--(?:get-symbol|read|search|explain)\s+([\w.]+)", cmd):
                        symbols.add(m.group(1))
    except OSError:
        pass
    buf.write(f"## Already-read files ({len(files)})\n")
    for f in sorted(files)[:30]:
        buf.write(f"  {f}\n")
    buf.write(f"\n## Already-explored symbols ({len(symbols)})\n")
    for s in sorted(symbols)[:30]:
        buf.write(f"  {s}\n")
    buf.write("\n# Skip these; focus reads on what you haven't seen.\n")
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

# --------------------------------------------------------------------------- #
# --resume: compaction-survival context restore
# --------------------------------------------------------------------------- #
# The real value isn't token savings — it's that every compaction wipes the
# structural context the agent built up. --resume emits a single, compact,
# self-contained snapshot (entry points + module graph + call edges + framework)
# sized to re-paste after a compaction, so the agent reloads its map in one shot
# instead of re-deriving it.

def build_resume(files: List[str], root: str, max_files: int) -> dict:
    """Build a compact structural snapshot for post-compaction restore."""
    rules = parse_gitignore(os.path.join(root, ".gitignore")) if os.path.isfile(os.path.join(root, ".gitignore")) else []
    tree = build_map(root, True, max_files)
    graph = build_graph(files, root)          # import edges
    calls = build_call_graph_multi(files, root)  # call edges
    # top callers/callees by edge count — the "what runs what" core
    call_sizes = []
    for mod, funcs in calls.items():
        for caller, callees in funcs.items():
            call_sizes.append((len(callees), f"{mod}.{caller}"))
    call_sizes.sort(reverse=True)
    top_calls = call_sizes[:12]
    # most-depended-on modules (blast radius)
    dependents = {}
    for mod, deps in graph.items():
        for d in deps:
            dependents[d] = dependents.get(d, 0) + 1
    hub_modules = sorted(dependents.items(), key=lambda x: -x[1])[:10]
    return {
        "root": root,
        "files": len(files),
        "entry_points": tree["entry_points"],
        "modules": sorted(graph.keys())[:25],
        "hub_modules": hub_modules,
        "top_calls": top_calls,
    }

def render_resume(files: List[str], root: str, max_files: int) -> str:
    r = build_resume(files, root, max_files)
    buf = io.StringIO()
    buf.write(f"# codeloom --resume (compaction-survival context restore)\n")
    buf.write(f"# repo: {r['root']} — {r['files']} files\n\n")
    if r["entry_points"]:
        buf.write("## Entry points\n")
        for e in r["entry_points"][:15]:
            buf.write(f"  {os.path.relpath(e, r['root'])}\n")
    buf.write("\n## Modules\n")
    for mod in r["modules"]:
        buf.write(f"  {mod}\n")
    buf.write("\n## Most-depended-on modules (change these -> breaks many)\n")
    for mod, n in r["hub_modules"]:
        buf.write(f"  {mod} ({n} importers)\n")
    buf.write("\n## Top call sites (what runs what)\n")
    for n, caller in r["top_calls"]:
        buf.write(f"  {caller} -> {n} callees\n")
    buf.write("\n# Paste this after a context compaction to restore your structural\n"
              "# model of the repo in one shot. Re-run `codeloom --resume` any time.\n")
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
    p.add_argument("--max-files", type=int, default=20000, help="cap traversal (default 20000; raise for 10M+ LOC monorepos)")
    p.add_argument("--parallel", action="store_true", help="parallelize file parsing for heavy ops (--cross/--deadcode/--calls) on large repos")
    p.add_argument("--graph", action="store_true", help="show Python import dependency graph")
    p.add_argument("--focus", metavar="MODULE", help="show deps/dependents of one module (with --graph)")
    p.add_argument("--calls", action="store_true", help="show function-level call graph (multi-language)")
    p.add_argument("--diff", action="store_true", help="show structure of files changed vs HEAD (git)")
    p.add_argument("--install-agents", action="store_true", help="write/update AGENTS.md with a codeloom block")
    p.add_argument("--install-agent", metavar="AGENT", help="print MCP config for an agent (claude|cursor|codex|gemini|opencode)")
    p.add_argument("--detect-agent", action="store_true", help="detect which coding agent's config dir is present")
    p.add_argument("--build-core", action="store_true", help="build the optional C accelerator (codeloom_core.c -> codeloom_core) if not present")
    p.add_argument("--cost", action="store_true", help="append token-cost estimate to output")
    p.add_argument("--session", action="store_true", help="log this invocation to the local session log (JSONL)")
    p.add_argument("--session-report", action="store_true", help="summarize the local session log (calls, tokens, cost)")
    p.add_argument("--impact", metavar="MODULE", help="predict blast radius of changing a module")
    p.add_argument("--check-edit", metavar="SYMBOL", help="preflight: is it safe to edit this symbol? (terminal GO/STOP verdict)")
    p.add_argument("--check-delete", metavar="SYMBOL", help="preflight: is it safe to delete this symbol? (terminal GO/STOP verdict)")
    p.add_argument("--task", metavar="TEXT", help="rank modules relevant to a task description")
    p.add_argument("--plan", metavar="TEXT", help="emit a prioritized reading plan for a task")
    p.add_argument("--pack", metavar="TEXT", help="emit a single-shot context file for a task (reading order + impact + symbols)")
    p.add_argument("--resume", action="store_true", help="emit a compact structural snapshot to restore context after compaction")
    p.add_argument("--loom", metavar="TEXT", help="intent engine: layered context for a task (overview->files->symbols->code->git->memory)")
    p.add_argument("--remember", metavar="NOTE", help="append a note to repository memory (default DECISIONS); use --section ARCHITECTURE|DECISIONS|PATTERNS|CONVENTIONS")
    p.add_argument("--section", metavar="NAME", default="DECISIONS", help="memory section for --remember")
    p.add_argument("--churn", action="store_true", help="git churn: most-edited files (instability signal)")
    p.add_argument("--cross", action="store_true", help="show cross-file call graph (resolved across modules)")
    p.add_argument("--cross-repo", nargs="+", metavar="PATH", help="build a combined knowledge graph across multiple repo roots")
    p.add_argument("--search", metavar="SYMBOL", help="search the symbol index for a function/class/method")
    p.add_argument("--hybrid-search", metavar="QUERY", help="hybrid search: BM25 lexical + structural signals scored together")
    p.add_argument("--seen", action="store_true", help="session memory: report already-read files/symbols to avoid re-reading")
    p.add_argument("--usages", metavar="SYMBOL", help="find where a symbol is used (not just defined)")
    p.add_argument("--grep", metavar="QUERY", help="search file contents for a snippet (ranked + context)")
    p.add_argument("--read", metavar="SYMBOL", help="extract exact source of a function/class/method (token-efficient)")
    p.add_argument("--explain", metavar="SYMBOL", help="plain-English explanation of a symbol (AST + call graph)")
    p.add_argument("--precision", metavar="SYMBOL", help="graph precision report: call edges with confidence + class relationships")
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
    p.add_argument("--auto-grammars", action="store_true", help="scan the repo and install grammars for its languages (beats the manual per-language step)")
    p.add_argument("--yes", action="store_true", help="with --install-grammars, actually run pip install")
    p.add_argument("--index", action="store_true", help="build + save a persistent byte-offset index (scale)")
    p.add_argument("--engine", choices=["py", "c"], default="py", help="scanning engine: py (pure-Python, default) or c (compiled codeloom_core, faster — build with cc -O3 -o codeloom_core codeloom_core.c)")
    p.add_argument("--watch", action="store_true", help="incremental daemon-less refresh: re-index only changed files, keep lookups near-resident")
    p.add_argument("--watch-core", metavar="ROOT", help="native C file watcher (kqueue/inotify): print changed code files live")
    p.add_argument("--serve", metavar="ROOT", help="C-resident index server: answer symbol lookups sub-ms (no Python per query)")
    p.add_argument("--index-status", action="store_true", help="show persistent index status/freshness")
    p.add_argument("--framework", action="store_true", help="detect the web/app framework and surface its structure (routes, models, config, conventions)")
    p.add_argument("--architecture", action="store_true", help="detect the architectural pattern (MVC/layered/DDD/monolith)")
    p.add_argument("--heatmap", action="store_true", help="dependency heatmap: god classes, circular imports, unused modules")
    p.add_argument("--explain-topic", metavar="TOPIC", help="explain a topic/domain end-to-end (files + call flow), e.g. 'explain authentication'")
    p.add_argument("--docs", metavar="KIND", nargs="?", const="readme", help="generate a README or ARCHITECTURE doc (--docs readme|arch)")
    p.add_argument("--refactor", metavar="SYMBOL", help="refactor engine: files, deps, risk, order for a symbol")
    p.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"), help="what a rename touches: definitions, files, dependents, edges")
    p.add_argument("--ask", metavar="TASK", help="one-shot complete task brief: loom context + blast radius + files-to-touch checklist")
    p.add_argument("--bug-predict", action="store_true", help="bug prediction: files likely to break (churn+coupling+complexity)")
    p.add_argument("--timeline", action="store_true", help="repository timeline: replay architecture evolution via git")
    p.add_argument("--dedup", action="store_true", help="session dedupe: skip already-read files, show the new delta")
    p.add_argument("--plugin-sdk", action="store_true", help="show the plugin SDK surface for framework-aware extraction")
    p.add_argument("--lsp", action="store_true", help="show LSP bridge status (optional semantic enrichment)")
    p.add_argument("--langs", action="store_true", help="list supported languages/extensions (broad regex + tree-sitter set)")
    p.add_argument("--lsp-symbol", metavar="SYMBOL", help="resolve a symbol's real definition via an installed LSP server (optional)")
    p.add_argument("--graph-html", action="store_true", help="write a local zoomable HTML graph view (codeloom-graph.html)")
    p.add_argument("--find", metavar="QUERY", help="natural-language flow discovery: 'find where login starts'")
    p.add_argument("--context-diff", nargs=2, metavar=("BASE", "HEAD"), help="branch-to-branch architecture-level diff (e.g. main HEAD)")
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

    # --auto-grammars: beat-the-tradeoff — scan repo, install grammars for its langs
    if args.auto_grammars:
        print(_ensure_grammars_for_root(root))
        return 0

    # --index-status: show persistent index status
    if args.index_status:
        print(render_index_status(root))
        return 0

    # --framework: detect the web/app framework and surface its structure
    if args.framework:
        print(render_framework(root, args.max_files))
        return 0

    if args.architecture:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_architecture(files, root))
        return 0

    if args.heatmap:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(dependency_heatmap(files, root))
        return 0

    if args.explain_topic:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_explain_topic(files, root, args.explain_topic, args.max_files))
        return 0

    if args.docs is not None:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_auto_docs(files, root, args.docs))
        return 0

    if args.refactor:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_refactor(files, root, args.refactor, args.max_files))
        return 0

    if args.rename:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_rename(files, root, args.rename[0], args.rename[1]))
        return 0

    if args.ask is not None:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_ask(files, root, args.ask, args.max_files))
        return 0

    if args.bug_predict:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_bug_predict(files, root))
        return 0

    if args.timeline:
        print(render_repo_timeline(root))
        return 0

    if args.dedup:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_dedup(root, files))
        return 0

    if args.plugin_sdk:
        print(render_plugin_sdk(root))
        return 0

    if args.lsp:
        print(render_lsp(root))
        return 0

    if args.langs:
        print(render_langs())
        return 0

    if args.lsp_symbol:
        # resolve a symbol's real definition via an installed LSP server
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_lsp_symbol(files, root, args.lsp_symbol))
        return 0

    if args.graph_html:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_graph_html(files, root))
        return 0

    if args.find:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(render_find(files, root, args.find, args.max_files))
        return 0

    if args.context_diff:
        print(render_context_diff(root, args.context_diff[0], args.context_diff[1]))
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
        if args.engine == "c" and _find_core():
            files = _c_walk(root)  # C walker — much faster on huge repos
            if not files:
                _walk(root, rules, args.max_files, files)
        else:
            _walk(root, rules, args.max_files, files)
        print(render_index(files, root, args.max_files, parallel=args.parallel, engine=args.engine))
        return 0

    if args.watch:
        # incremental daemon-less refresh: only changed files re-indexed
        print(refresh_index_incremental(root, args.max_files))
        return 0

    if args.watch_core:
        # native C file watcher (kqueue on macOS / inotify on Linux)
        core = _find_core()
        if not core:
            print("C core not built. Run: codeloom --build-core")
            return 1
        import subprocess as _sp
        wroot = args.watch_core if args.watch_core != "." else root
        try:
            _sp.run([core, "--watch", wroot])
        except KeyboardInterrupt:
            pass
        return 0

    if args.serve:
        # C-resident index server: sub-ms lookups, no Python per query
        core = _find_core()
        if not core:
            print("C core not built. Run: codeloom --build-core")
            return 1
        import subprocess as _sp
        sroot = args.serve if args.serve != "." else root
        try:
            _sp.run([core, "--serve", sroot])
        except KeyboardInterrupt:
            pass
        return 0

    # --get-symbol / --search / --hybrid-search: fast-path (no walk where possible)
    if (args.get_symbol or args.search or args.hybrid_search):
        if args.hybrid_search:
            gi = os.path.join(root, ".gitignore")
            rules = parse_gitignore(gi) if os.path.isfile(gi) else []
            hfiles: List[str] = []
            _walk(root, rules, args.max_files, hfiles)
            print(render_hybrid_search(hfiles, root, args.hybrid_search))
            return 0
        lazy_locs = None
        if args.get_symbol and not args.full:
            lazy_locs = load_symbol_lazy(root, args.get_symbol)
            if lazy_locs:
                loc = lazy_locs[0]
                if loc.get("tokens", 0) <= ADAPTIVE_FULL_THRESHOLD:
                    src = _read_source_from_loc(loc, root)
                    print(f"# get_symbol: {args.get_symbol}\n"
                          f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                          f"bytes {loc.get('start_byte',0)}-{loc.get('end_byte',0)}  "
                          f"~{loc.get('tokens',0)} tokens\n\n"
                          f"{src}\n")
                else:
                    sig = loc.get("sig") or args.get_symbol
                    print(f"# get_symbol: {args.get_symbol}\n"
                          f"{loc['module']}:{loc['line']}  [{loc['kind']}]  ~10 tokens (summary)\n\n"
                          f"Signature: {sig}\n"
                          f"Use `--get-symbol {args.get_symbol} --full` for the full source.\n")
                return 0
        pidx = ensure_fresh_index(root, args.max_files)
        if pidx is not None:
            if args.get_symbol:
                locs = pidx.get("symbols", {}).get(args.get_symbol)
                if locs:
                    loc = locs[0]
                    if args.full:
                        # full source: re-read the file from the stored byte range
                        src = _read_source_from_loc(loc, root)
                        print(f"# get_symbol: {args.get_symbol}\n"
                              f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                              f"bytes {loc.get('start_byte',0)}-{loc.get('end_byte',0)}  "
                              f"~{loc.get('tokens',0)} tokens\n\n"
                              f"{src}\n")
                        return 0
                    # adaptive: small symbols return the implementation by default
                    if loc.get("tokens", 0) <= ADAPTIVE_FULL_THRESHOLD:
                        src = _read_source_from_loc(loc, root)
                        print(f"# get_symbol: {args.get_symbol}\n"
                              f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                              f"bytes {loc.get('start_byte',0)}-{loc.get('end_byte',0)}  "
                              f"~{loc.get('tokens',0)} tokens\n\n"
                              f"{src}\n")
                        return 0
                    # render summary directly from the index (no re-parse)
                    sig = loc.get("sig") or args.get_symbol
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

    if args.install_agent:
        agent = args.install_agent.lower()
        valid = {"claude", "cursor", "codex", "gemini", "opencode"}
        if agent not in valid:
            print(f"unknown agent '{agent}'. Valid: {', '.join(sorted(valid))}")
            return 1
        print(install_agent_config(agent, os.path.abspath(__file__)))
        return 0

    if args.detect_agent:
        a = detect_agent()
        print(a if a else "no agent config dir detected")
        return 0

    if args.build_core:
        core_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeloom_core.c")
        if not os.path.isfile(core_src):
            print(f"C core source not found at {core_src}")
            return 1
        if _find_core():
            print("C accelerator already built — nothing to do.")
            return 0
        print("building codeloom_core (cc -O3 codeloom_core.c)…")
        import subprocess as _sp
        out = os.path.join(os.path.dirname(core_src), "codeloom_core")
        r = _sp.run(["cc", "-O3", "-o", out, core_src], capture_output=True, text=True)
        if r.returncode == 0:
            print(f"built {out} — now use --index --engine c")
            return 0
        print(f"build failed (is cc/clang installed?): {r.stderr[:300]}")
        return 1

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
       or args.explain or args.similar or args.deadcode or args.get_symbol or args.precision:
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
            # adaptive: pass summary=True so render_get_symbol flips to full
            # source only for small symbols (<= threshold)
            print(render_get_symbol(files, root, args.get_symbol, summary=(use_summary is True or use_summary == "adaptive"), adaptive=(use_summary == "adaptive")))
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

        if args.precision:
            print(render_precision(files, root, args.precision))
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
    if args.impact or args.task or args.plan or args.pack or args.check_edit or args.check_delete or args.resume or args.loom or args.remember:
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

        if args.check_edit:
            print(preflight_check(files, root, args.check_edit, "edit"))
            return 0

        if args.check_delete:
            print(preflight_check(files, root, args.check_delete, "delete"))
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

        if args.resume:
            print(render_resume(files, root, args.max_files))
            return 0

        if args.loom:
            print(render_loom_context(files, root, args.loom, args.max_files))
            return 0

        if args.remember:
            print(memory_remember(root, args.section, args.remember))
            return 0

    if args.churn:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        print(git_churn(root, files))
        return 0

    if args.seen:
        # session memory: report already-read files/symbols to avoid re-reading
        print(render_seen(root))
        return 0

    # Cross-repo mode: one graph across multiple repo roots
    if args.cross_repo:
        print(render_cross_repo(args.cross_repo, args.max_files))
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
