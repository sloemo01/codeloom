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
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

VERSION = "0.79.7"

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
    extension on first use (opt-in, one grammar at a time, default OFF)."""
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

def _ensure_grammars_for_root(root: str, force: bool = False) -> str:
    """BEAT-THE-TRADEOFF: scan the repo for its actual code extensions and
    auto-install just the tree-sitter grammars those languages need, in one
    shot. No per-language manual trigger. Returns a summary. Only installs
    grammars that have a known package in _EXT_GRAMMAR_PKG.

    OPT-IN (security: this runs pip install with shell=True): it does NOT
    auto-install on --index/--watch by default — the env var defaults to 0.
    Set CODELOOM_AUTO_INSTALL_GRAMMARS=1 to enable auto-install, or pass
    force=True (the explicit `--auto-grammars` flag) to install once."""
    # opt-in via CODELOOM_AUTO_INSTALL_GRAMMARS=1 (or the explicit --auto-grammars
    # flag); default is OFF so index/watch never pip-install without consent
    if not force and os.environ.get("CODELOOM_AUTO_INSTALL_GRAMMARS", "0").lower() in ("0", "false", "off"):
        return ("Auto-install off (set CODELOOM_AUTO_INSTALL_GRAMMARS=1 to enable "
                "automatic grammar installs, or run `codeloom --auto-grammars` once).\n")
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
                 "class_declaration", "class_definition", "struct_item", "impl_item",
                 "func_declaration", "func_literal", "method_declaration", "type_declaration"):
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
                    _collect_calls(sub, edges, caller)
        for child in node.children:
            stack.append(child)
    # Module-top-level statements (e.g. `REGISTRY = Registry()`, top-level
    # `if __name__ == "__main__": main()`) are NOT inside any function and the
    # walk above never visits them — so instantiated classes look uncalled and
    # dead_code flags them (repowise 2026-08-23; mirror of the regex path's
    # synthetic "<module>" caller). Collect calls from every top-level child
    # that is not a definition wrapper (decorated_definition holds the decorator
    # call AND the inner function — skipping it avoids double-attribution).
    _ts_def_types = ("function_definition", "function_declaration", "method_definition",
                     "class_declaration", "class_definition", "struct_item", "impl_item",
                     "func_declaration", "func_literal", "method_declaration",
                     "type_declaration", "decorated_definition")
    for child in root_node.children:
        if child.type in _ts_def_types:
            continue
        _collect_calls(child, edges, "<module>")
    return edges


def _collect_calls(node, edges, caller=None):
    """Collect call targets within a node (iterative, no recursion limit).

    Caller is the enclosing function name; `"<module>"` for module-level
    statements. Attribute calls (`obj.method()`, `self._x()`, `Registry()`
    through `x = Registry()`) resolve to the rightmost identifier so
    qualified/self-calls count as real callers.
    """
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("call_expression", "call", "function_call"):
            # find the function name — for attribute access, take the LAST
            # identifier of the callee expression (method/class name)
            for child in n.children:
                if child.type in ("identifier", "field_identifier", "name"):
                    callee = child.text.decode("utf-8", "replace")
                    edges.add((caller, callee))
                    break
                if child.type in ("attribute", "field_expression", "member_expression"):
                    text = child.text.decode("utf-8", "replace")
                    parts = text.split(".")
                    callee = parts[-1].strip() if parts else text
                    if callee:
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

# --- Zero-dependency subword-hash embedding (fastText n-gram trick) ---------
# Borrowed technique from facebookresearch/fastText (MIT): map subword n-grams
# (2-6 chars) of an identifier into a fixed-size hash bucket, sum -> normalize.
# This gives a *local, always-available* semantic embedding with NO neural net
# and NO pip deps — enough to do fuzzy/"semantic" symbol search that plain
# string match misses (typos, camelCase/snake_case splits, partial names).
#------------------------------------------------------------------------------

def _subword_hashes(word: str, dim: int = 256) -> List[float]:
    """FastText-style subword n-gram hash embedding. Pure-Python, zero-dep.
    Returns a normalized vector over `dim` buckets. Always available."""
    vec = [0.0] * dim
    w = word.lower()
    # word-level n-grams 2..6 + the full word
    grams = [w]
    for n in range(2, 7):
        if len(w) >= n:
            for i in range(len(w) - n + 1):
                grams.append(w[i:i+n])
    for g in grams:
        # fnv-1a hash -> bucket
        h = 2166136261
        for ch in g.encode("utf-8", "replace"):
            h ^= ch
            h = (h * 16777619) & 0xFFFFFFFF
        idx = h % dim
        # sign-symmetry so similar hashes land near each other
        vec[idx] += 1.0 if (h & 1) else -1.0
    # L2 normalize
    norm = sum(v*v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec

def _subword_similarity(a: str, b: str) -> float:
    """Cosine similarity of two subword-hash embeddings (0..1)."""
    va = _subword_hashes(a)
    vb = _subword_hashes(b)
    return sum(x*y for x, y in zip(va, vb))

def render_embed_search(files: List[str], root: str, query: str, limit: int = 15) -> str:
    """--embed-search: semantic symbol search. Uses a local ggml neural
    embedding when available (CODELOOM_GGML_BIN + CODELOOM_GGML_MODEL), else the
    zero-dependency subword-hash embedding. Both catch typos, case splits, and
    cross-language names that exact match misses."""
    index = build_byte_index(files, root)
    names = list(index.keys())
    # try neural first; if it fails, fall back to subword hash
    use_neural = False
    neural_vecs = None
    q_emb = None
    try:
        q_emb_list = _neural_embedding([query])
        if q_emb_list:
            neural_vecs = _neural_embedding(names[:400])  # cap for speed
            if neural_vecs:
                use_neural = True
                q_emb = q_emb_list[0]
    except Exception:
        pass

    scored = []
    for i, (name, locs) in enumerate(index.items()):
        if use_neural and neural_vecs is not None and q_emb is not None and i < len(neural_vecs):
            score = _cosine_sim(q_emb, neural_vecs[i])
        else:
            score = _subword_similarity(query, name)
        mod = locs[0].get("module", "") if locs else ""
        mod_sim = _subword_similarity(query, mod)
        score = max(score, mod_sim * 0.7)
        if score >= 0.35:
            scored.append({"name": name, "module": mod, "score": round(score, 3),
                           "line": locs[0].get("line", 0) if locs else 0})
    scored.sort(key=lambda x: -x["score"])
    buf = io.StringIO()
    kind = "neural (ggml)" if use_neural else "zero-dep subword-hash"
    buf.write(f"# embed search: {query}  (semantic similarity, {kind})\n")
    if not scored:
        buf.write("  No semantically-similar symbols found. Try --search for exact matches.\n")
        return buf.getvalue()
    buf.write(f"{len(scored)} semantically-similar symbol(s) ({kind} embedding):\n\n")
    for r in scored[:limit]:
        buf.write(f"  {r['name']}  [{r['module']}:{r['line']}]  (sim {r['score']})\n")
    if use_neural and len(names) > 400:
        buf.write("\n# neural pass capped to first 400 symbols (speed); use --search for exhaustive matching\n")
    buf.write("\n# Catches typos, camelCase splits, and cross-language names that exact match misses.\n")
    return buf.getvalue()

# --- Optional neural embedding via local ggml binary (progressive) --------
# Honest infra: if a ggml-embedding binary + a gguf model are available on the
# machine (CODELOOM_GGML_BIN / CODELOOM_GGML_MODEL or on PATH), shell out to it
# for REAL neural embeddings. Otherwise fall back to the zero-dep subword hash.
# This wires the neural path without shipping a model (which would break the
# zero-dep single file). The model download is a user choice, not a rewrite.
def _neural_embedding(texts: List[str]) -> Optional[List[List[float]]]:
    """Neural embeddings via a local llama.cpp binary. BATCHED: all texts go
    through ONE process invocation (llama-embedding accepts multiple prompts
    and emits one embedding per line) — N texts cost one model load, not N.
    Falls back to None (caller uses the zero-dep subword path) on any error."""
    import shutil as _sh
    import subprocess as _sp
    import os as _os
    binp = _os.environ.get("CODELOOM_GGML_BIN") or _sh.which("llama-embedding") or _sh.which("main")
    model = _os.environ.get("CODELOOM_GGML_MODEL")
    if not binp or not model or not texts:
        return None
    try:
        # one process, many prompts: each -p adds a prompt; output is one
        # embedding per line in json/array format
        cmd = [binp, "-m", model, "--embd-output-format", "json", "--pooling", "mean"]
        for t in texts:
            cmd += ["-p", t]
        r = _sp.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None
        import re as _re
        vecs = []
        for m in _re.finditer(r"\[[\s\-\d.,eE+]+\]", r.stdout):
            nums = [float(x) for x in m.group(0).replace("[", "").replace("]", "").split(",") if x.strip()]
            if nums:
                vecs.append(nums)
        return vecs if len(vecs) == len(texts) else None
    except Exception:
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
          _visited: Optional[set] = None, _base_real: Optional[str] = None) -> None:
    """Collect file paths, respecting ignores, capped by max_files.
    Guards against symlink loops by tracking visited real paths.
    Merges nested .gitignore files as it descends (gitignore semantics:
    a subdirectory's .gitignore adds rules scoped to that directory).
    Symlinks are followed ONLY when they resolve inside the repo root —
    a symlinked dir/file pointing outside (e.g. ~/.ssh) used to be walked
    and indexed, leaking outside content into the index."""
    if len(files) >= max_files:
        return
    if _visited is None:
        _visited = set()
        _base_real = os.path.realpath(root)
    assert _base_real is not None  # set together with _visited above
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
        if os.path.islink(full):
            # containment: only follow symlinks that stay inside the repo
            try:
                if not os.path.realpath(full).startswith(_base_real + os.sep):
                    continue
            except OSError:
                continue
        if os.path.isdir(full):
            _walk(full, rules, max_files, files, _visited, _base_real)
        elif os.path.isfile(full):
            if len(files) >= max_files:
                return
            files.append(full)
            # binary docs: ensure a searchable text sidecar exists
            ext = os.path.splitext(full)[1].lower()
            if ext in DOC_EXTS:
                side = ensure_doc_sidecar(full)
                if side and len(files) < max_files:
                    files.append(side)

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

# --------------------------------------------------------------------------- #
# Compact / summary-first output layer (--compact, CODELOOM_COMPACT=1, MCP)
# --------------------------------------------------------------------------- #
# An agent pays for every token that lands in its context. The expensive
# renderers (health, map, calls, cross, graph) default to summary-first when
# serving the MCP server (agent-facing), while bare terminal invocations stay
# verbose. --compact (or CODELOOM_COMPACT=1) opts the CLI in; --full forces
# the full output even from MCP.
COMPACT_MAX_LIMIT = 5          # rows shown in summary mode (top-N)
COMPACT_TOKEN_CAP = 400        # summary-first renders self-trim below this
COMPACT_HARD_CAP = 600         # any render beyond this is hard-truncated

def _in_mcp() -> bool:
    """True when codeloom is imported by the MCP server (codeloom-mcp.py /
    codeloom_mcp.py shim / 'codeloom-mcp' console script). Agent-facing
    output must default to compact; the CLI never matches."""
    base = os.path.basename(sys.argv[0] or "").replace(".py", "")
    return base.startswith("codeloom-mcp") or base.startswith("codeloom_mcp")

def _compact_default(flag: Optional[bool]) -> bool:
    """Resolve compact mode: explicit flag wins, else CODELOOM_COMPACT=1,
    else MCP context (agent context is expensive -> compact by default)."""
    if flag is not None:
        return flag
    if os.environ.get("CODELOOM_COMPACT", "").lower() in ("1", "true", "yes", "on"):
        return True
    return _in_mcp()

def _trim(lines: List[str], limit: int) -> List[str]:
    """Cap a rendered body: lines beyond `limit` collapse into a single
    '... N more (use --full)' summary line (the shared --compact helper)."""
    if len(lines) <= limit:
        return lines
    keep = max(1, limit - 1)  # reserve one line for the footer
    return lines[:keep] + ["  ... %d more (use --full)" % (len(lines) - keep)]

def _cap_output(text: str, hard: int = COMPACT_HARD_CAP) -> str:
    """Hard token cap for agent-facing renders: output above `hard` tokens is
    truncated with a footer so a single tool call never floods a context."""
    if estimate_tokens(text) <= hard:
        return text
    budget = hard * 4 - 48  # chars; leave room for the footer line
    lines = text.splitlines()
    out: List[str] = []
    used = 0
    for ln in lines:
        if used + len(ln) + 1 > budget:
            break
        out.append(ln)
        used += len(ln) + 1
    out.append("... (truncated at ~%d tokens — use --full for the complete output)" % hard)
    return "\n".join(out)

def _tree_file_count(node: Node) -> int:
    """Number of files under a tree node (dirs recurse, files count)."""
    n = 0
    for c in node.children:
        n += _tree_file_count(c) if c.is_dir else 1
    return n

def _top_level_counts(tree: Node) -> List[Tuple[str, int]]:
    """File counts per top-level directory (compact map); root-level files
    group under '(root)'. Sorted by count descending."""
    counts: Dict[str, int] = {}
    root_files = 0
    for c in tree.children:
        if c.is_dir:
            counts[c.name + "/"] = _tree_file_count(c)
        else:
            root_files += 1
    if root_files:
        counts["(root)"] = root_files
    return sorted(counts.items(), key=lambda kv: -kv[1])

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
# HTTP route extraction (#framework-routes) — parse actual METHOD path -> handler
# across the common frameworks (FastAPI, Flask, Express, Django, Next.js, Starlette).
# This links URL patterns to the handler function that serves them — what
# codegraph and codebase-memory call "framework-aware routes".
# --------------------------------------------------------------------------- #
_ROUTE_RE = re.compile(
    r"""(?:@)?(?:app|router|bp|api|route)\.(?P<method>get|post|put|delete|patch|options|head)\(
        \s*['\"](?P<path>[^'\"}]+)['\"]""",
    re.IGNORECASE | re.VERBOSE,
)

def extract_routes(root: str, max_files: int = 20000) -> List[dict]:
    """Scan a repo for HTTP routes and return {method, path, handler, file, line}.
    Supports FastAPI/Flask/Starlette decorators (@app.get, @router.post),
    Express (.get/.post chains), Django (urls.py path()), and file-based
    routing (Next.js/Nuxt app dir -> URL). Zero-dep, regex + AST."""
    import ast as _ast
    routes = []
    gi = os.path.join(root, ".gitignore")
    rules = parse_gitignore(gi) if os.path.isfile(gi) else []
    files: List[str] = []
    _walk(root, rules, max_files, files)
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        rel = os.path.relpath(f, root).replace(os.sep, "/")
        # FastAPI / Flask / Starlette decorator routes (Python AST-aware)
        if ext == ".py":
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    tree = _ast.parse(fh.read())
            except Exception:
                continue
            for node in _ast.walk(tree):
                if isinstance(node, _ast.FunctionDef):
                    handler = node.name
                    for deco in node.decorator_list:
                        meth = None
                        path = None
                        # @app.get("/x") or @router.post("/y")
                        if isinstance(deco, _ast.Call) and isinstance(deco.func, _ast.Attribute):
                            meth = deco.func.attr.lower()
                            if deco.args and isinstance(deco.args[0], _ast.Constant):
                                path = deco.args[0].value
                        elif isinstance(deco, _ast.Call) and isinstance(deco.func, _ast.Name) and deco.func.id in ("get", "post", "put", "delete", "patch"):
                            meth = deco.func.id.lower()
                            if deco.args and isinstance(deco.args[0], _ast.Constant):
                                path = deco.args[0].value
                        if meth and path and meth in ("get", "post", "put", "delete", "patch", "options"):
                            routes.append({"method": meth.upper(), "path": path,
                                           "handler": handler, "file": rel,
                                           "line": node.lineno})
            continue
        # Express / Fastify .get('/x', handler) chains
        if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs"):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for m in _ROUTE_RE.finditer(text):
                # find handler = symbol following the path arg
                seg = text[m.end():m.end() + 80]
                handler = "anonymous"
                m2 = re.search(r"[,\s]+([A-Za-z_]\w*)\s*(?:,|\))", seg)
                if m2:
                    handler = m2.group(1)
                routes.append({"file": rel, "method": m.group("method").upper(),
                               "path": m.group("path"), "handler": handler,
                               "line": text[:m.start()].count("\n") + 1})
        # Next.js / Nuxt file-based routes
        if ext in (".tsx", ".jsx", ".ts", ".js") and re.search(r"(app|pages)/", rel):
            if re.search(r"(app|pages)/", rel) and not rel.endswith(("_app.tsx", "_document.tsx")):
                sub = rel.split("/", 1)[1] if "/" in rel else rel
                base = re.sub(r"\.(?:tsx|jsx|ts|js)$", "", sub)
                base = base.replace("page", "").replace("index", "").rstrip("/")
                path = "/" + base if base else "/"
                # dynamic segments
                path = re.sub(r"\[(\w+)\]", r":\1", path)
                routes.append({"method": "GET", "path": path, "handler": "page",
                               "file": rel, "line": 0})
    return routes

def render_routes(root: str, max_files: int = 20000) -> str:
    """--routes: framework-aware URL -> handler mapping (like codegraph/cbm)."""
    routes = extract_routes(root, max_files)
    buf = io.StringIO()
    buf.write(f"# codeloom --routes\n")
    if not routes:
        buf.write("  No routes detected.\n")
        return buf.getvalue()
    buf.write(f"{len(routes)} HTTP route(s):\n\n")
    for r in routes:
        buf.write(f"  {r['method']:<6} {r['path']:<30} -> {r['handler']}  ({r['file']}:{r['line']})\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Channel / pub-sub detection (#channels) — EMITS / LISTENS_ON edges
# --------------------------------------------------------------------------- #
# Detects socket.io emit/on, Node EventEmitter emit/on, generic pub-sub, and
# Kafka/RabbitMQ-style publish/subscribe. Static, regex-based, zero-dep.
_CHANNEL_RE = re.compile(
    r"""(?:\.emit\(|\.publish\(|\.produce\(|\.send\(|socket\.emit\(|io\.emit\()
        ['\"]?(?P<channel>[a-zA-Z0-9_:.\-/]{1,80})""",
    re.VERBOSE,
)
_LISTEN_RE = re.compile(
    r"""(?:\.on\(|\.subscribe\(|\.consume\(|\.addListener\(|socket\.on\()    ['\"]?(?P<channel>[a-zA-Z0-9_:.\-/]{1,80})['\"]""",
    re.VERBOSE,
)

def extract_channels(root: str, max_files: int = 20000) -> dict:
    """Scan a repo for pub-sub / event channels. Returns
    {"emit": {channel: [files]}, "listen": {channel: [files]}} — the EMITS /
    LISTENS_ON edges that link senders to receivers across files."""
    emit = {}
    listen = {}
    gi = os.path.join(root, ".gitignore")
    rules = parse_gitignore(gi) if os.path.isfile(gi) else []
    files: List[str] = []
    _walk(root, rules, max_files, files)
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in (".js", ".jsx", ".ts", ".tsx", ".py", ".mjs", ".go", ".rs", ".rb"):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        rel = os.path.relpath(f, root).replace(os.sep, "/")
        for m in _CHANNEL_RE.finditer(text):
            ch = m.group("channel")
            if not ch:
                continue
            # skip false positives (common keywords)
            if ch in ("error", "data", "message", "close", "open", "end"):
                continue
            emit.setdefault(ch, []).append(rel)
        for m in _LISTEN_RE.finditer(text):
            ch = m.group("channel")
            if not ch or ch in ("error", "data", "message", "close", "open", "end"):
                continue
            listen.setdefault(ch, []).append(rel)
    return {"emit": emit, "listen": listen}

def render_channels(root: str, max_files: int = 20000) -> str:
    """--channels: pub-sub / event channel map (EMITS -> LISTENS_ON)."""
    c = extract_channels(root, max_files)
    buf = io.StringIO()
    buf.write("# codeloom --channels\n")
    if not c["emit"] and not c["listen"]:
        buf.write("  No pub-sub / event channels detected.\n")
        return buf.getvalue()
    if c["emit"]:
        buf.write("## EMITS\n")
        for ch in sorted(c["emit"]):
            buf.write(f"  {ch:<30} <- {', '.join(c['emit'][ch][:5])}\n")
    if c["listen"]:
        buf.write("\n## LISTENS_ON\n")
        for ch in sorted(c["listen"]):
            buf.write(f"  {ch:<30} <- {', '.join(c['listen'][ch][:5])}\n")
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

def render_files(files: List[str], root: str, glob: str, limit: int = 50) -> str:
    """Find files by name/glob pattern — like 'search_files files mode'. Matches
    the basename (case-insensitive substring) or a glob like '*.py' / '*test*'.
    Returns the matching file paths relative to root."""
    q = glob.strip()
    import fnmatch
    buf = io.StringIO()
    buf.write(f"# files: {glob}\n")
    matched = []
    is_glob = any(ch in q for ch in "*?[")
    for f in files:
        rel = os.path.relpath(f, root)
        rel_slash = rel.replace(os.sep, "/")
        if is_glob:
            if fnmatch.fnmatch(base_slash(rel), q) or fnmatch.fnmatch(rel_slash, q):
                matched.append(rel_slash)
        else:
            if q.lower() in rel_slash.lower():
                matched.append(rel_slash)
    if not matched:
        buf.write("  No matching files.\n")
        return buf.getvalue()
    for rel in sorted(matched)[:limit]:
        buf.write(f"  {rel}\n")
    return buf.getvalue()

def base_slash(rel: str) -> str:
    return rel.split("/")[-1]

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
        # '--' guard: base/head are spliced into argv, so a branch name that
        # starts with '-' could otherwise be parsed as a git option. Refuse
        # leading-dash refs outright, and terminate the revspec with '--' so
        # nothing after it can be interpreted as a flag either.
        if base.startswith("-") or head.startswith("-"):
            buf.write("  Invalid revision range (ref names must not start with '-').\n")
            return buf.getvalue()
        r = subprocess.run(["git", "-C", root, "diff", "--name-only",
                            f"{base}...{head}", "--"],
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
def _js_escape(s: str) -> str:
    """Escape a string for safe embedding inside a double-quoted JS string
    literal. Module names derive from FILE NAMES on disk — a hostile repo
    (e.g. a dir named `evil\"+alert(1)+\"`) used to break out of the literal
    and execute arbitrary JS when the generated graph HTML was opened."""
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r")
             .replace("\t", "\\t")
             .replace("</", "<\\/"))  # also defuse </script> breakout


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
    js_nodes = "[" + ",".join(f'"{_js_escape(n)}"' for n in nodes) + "]"
    js_links = "[" + ",".join(f'{{"source":"{_js_escape(a)}","target":"{_js_escape(b)}"}}' for a, b in edges) + "]"
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

_SUFFIX_INDEX_CACHE: dict = {}  # id(module_map) -> (module_map, {suffix: mod})


def _suffix_index(module_map: dict) -> dict:
    """Build {suffix: shallowest_module} ONCE per module_map (plain dicts can't
    take attributes, so a module-level cache keyed by id() with an identity
    check — the strong ref to the map prevents id reuse while cached). The old
    _resolve_import step-4 scanned every module name for every unresolved
    import — O(resolutions × modules), the quadratic wall on HA-core
    (~200k resolutions × 12k modules ≈ 2.5B comparisons)."""
    key = id(module_map)
    hit = _SUFFIX_INDEX_CACHE.get(key)
    if hit is not None and hit[0] is module_map:
        return hit[1]
    idx = {}
    for mod in module_map:
        segs = mod.split(".")
        for i in range(len(segs)):
            skey = ".".join(segs[i:])
            cur = idx.get(skey)
            if cur is None or len(segs) < len(cur.split(".")):
                idx[skey] = mod
    if len(_SUFFIX_INDEX_CACHE) > 64:
        _SUFFIX_INDEX_CACHE.clear()
    _SUFFIX_INDEX_CACHE[key] = (module_map, idx)
    return idx


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
    #    O(1) via the per-map suffix index (built once) — the old full-scan
    #    over every module per import was the quadratic wall on big repos.
    best = _suffix_index(module_map).get(target)
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
# Text/doc/config extensions that --grep and --find should search, even though
# they aren't code languages (so a repo-wide audit catches stale references in
# READMEs, configs, and plain text too).
_TEXT_EXTS = {".md", ".mdx", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml",
              ".ini", ".cfg", ".conf", ".xml", ".html", ".css", ".svg",
              ".csv", ".tsv", ".env", ".sh", ".bash", ".zsh", ".fish"}

# --------------------------------------------------------------------------- #
# Binary-document extraction -> searchable .txt sidecars (zero-dep: zip+xml
# via stdlib; pdftotext optional for PDFs). Sidecars land in _TEXT_EXTS so
# --grep/--map/index pick them up with zero pipeline changes.
# --------------------------------------------------------------------------- #
DOC_EXTS = {
    ".pdf": "pdftotext (optional external)",
    ".docx": "zip+xml (stdlib)", ".docm": "zip+xml (stdlib)",
    ".dotx": "zip+xml (stdlib)",
    ".xlsx": "zip+xml (stdlib)", ".xlsm": "zip+xml (stdlib)",
    ".pptx": "zip+xml (stdlib)",
    ".epub": "zip+xml (stdlib)",
    ".odt": "zip+xml (stdlib)",
    ".rtf": "control-word strip (stdlib)",
}


def _zip_read_text(path: str, member: str) -> Optional[str]:
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            return z.read(member).decode("utf-8", errors="replace")
    except Exception:
        return None


def _xml_texts(xml_text: str, tags: set) -> str:
    """Extract element text for tags (namespace-stripped)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        out = [m.strip() for m in re.findall(r"<[^>]*>([^<]+)</[^>]*>", xml_text)
               if m.strip()]
        return "\n".join(out)
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] in tags and (el.text or "").strip():
            out.append((el.text or "").strip())
    return "\n".join(out)


def extract_doc_text(path: str) -> Optional[str]:
    """Extract plain text from a binary document. None = unsupported or
    missing tool (PDFs need pdftotext on PATH; we never download anything)."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            import shutil
            import subprocess as _sp
            if not shutil.which("pdftotext"):
                return None
            r = _sp.run(["pdftotext", "-layout", path, "-"],
                        capture_output=True, timeout=90)
            return (r.stdout.decode("utf-8", errors="replace")
                    if r.returncode == 0 else None)
        if ext in (".docx", ".docm", ".dotx"):
            xml = _zip_read_text(path, "word/document.xml")
            return _xml_texts(xml, {"t"}) if xml else None
        if ext in (".xlsx", ".xlsm"):
            ss = _zip_read_text(path, "xl/sharedStrings.xml")
            return _xml_texts(ss, {"t"}) if ss else None
        if ext == ".pptx":
            import zipfile
            parts = []
            with zipfile.ZipFile(path) as z:
                slides = sorted(n for n in z.namelist()
                                if re.match(r"ppt/slides/slide\d+\.xml$", n))
                for s in slides:
                    t = _xml_texts(z.read(s).decode("utf-8", errors="replace"),
                                   {"t"})
                    if t:
                        parts.append(t)
            return "\n\n".join(parts) if parts else None
        if ext == ".epub":
            import zipfile
            parts = []
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.lower().endswith((".xhtml", ".html", ".htm")):
                        t = _xml_texts(z.read(n).decode("utf-8", errors="replace"),
                                       {"p", "h1", "h2", "h3", "li", "td", "th"})
                        if t:
                            parts.append(t)
            return "\n\n".join(parts) if parts else None
        if ext == ".odt":
            xml = _zip_read_text(path, "content.xml")
            if xml:
                txt = re.sub(r"<[^>]+>", " ", xml)
                return re.sub(r"\s+", " ", txt).strip() or None
            return None
        if ext == ".rtf":
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
            data = re.sub(r"\\'([0-9a-fA-F]{2})",
                          lambda m: chr(int(m.group(1), 16)), data)
            data = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", data)
            data = data.replace("{", "").replace("}", "")
            return re.sub(r"\s+", " ", data).strip() or None
    except Exception:
        return None
    return None


def ensure_doc_sidecar(path: str) -> Optional[str]:
    """Write <path>.txt next to a binary doc when missing or stale. Returns
    the sidecar path, or None if extraction isn't possible."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in DOC_EXTS:
        return None
    side = path + ".txt"
    try:
        if os.path.isfile(side) and os.path.getmtime(side) >= os.path.getmtime(path):
            return side
        text = extract_doc_text(path)
        if text is None:
            return None
        with open(side, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        return side
    except OSError:
        return None

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

def render_graph_multi(graph: dict, root: str, start: Optional[str] = None,
                       compact: Optional[bool] = None) -> str:
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
    if _compact_default(compact):
        rows = ["  %s -> %s" % (m, d) for m, d in edges[:COMPACT_MAX_LIMIT]]
        out = buf.getvalue() + "\n".join(_trim(rows, COMPACT_MAX_LIMIT)) + "\n"
        return _cap_output(out)
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
        graph = build_graph_multi(files, root, parallel=True)
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

def render_cross_repo(repos: List[str], max_files: int = 20000,
                      compact: Optional[bool] = None) -> str:
    cr = build_cross_repo(repos, max_files)
    buf = io.StringIO()
    buf.write("# cross-repo knowledge graph\n")
    if not cr["repos"]:
        buf.write("  No valid repo roots given.\n")
        return buf.getvalue()
    buf.write(f"  {len(cr['repos'])} repo(s), {sum(len(v['modules']) for v in cr['repos'].values())} modules\n\n")
    for name, info in cr["repos"].items():
        buf.write(f"## {name} ({info['files']} files, {len(info['modules'])} modules)\n")
        if _compact_default(compact):
            for mod in info["modules"][:COMPACT_MAX_LIMIT]:
                buf.write(f"  {mod}\n")
            if len(info["modules"]) > COMPACT_MAX_LIMIT:
                buf.write(f"  ... (+{len(info['modules'])-COMPACT_MAX_LIMIT} more, use --full)\n")
        else:
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
    out = buf.getvalue()
    return _cap_output(out) if _compact_default(compact) else out

def render_export(root: str, out_path: str, max_files: int = 20000) -> str:
    """--export: write a portable, self-contained graph snapshot (symbols +
    call/import edges + routes + channels) to a single JSON file. Commit it to
    the repo so teammates clone a pre-built graph and skip the reindex — the
    'commit-and-share' pattern codebase-memory ships as its .graph snapshot."""
    gi = os.path.join(root, ".gitignore")
    rules = parse_gitignore(gi) if os.path.isfile(gi) else []
    files: List[str] = []
    _walk(root, rules, max_files, files)
    # build the knowledge graph
    kg = build_knowledge_graph(files, root, parallel=True)
    snapshot = {
        "version": 1,
        "root": os.path.abspath(root),
        "files": len(files),
        "calls": kg.get("calls", {}),
        "imports": kg.get("imports", {}),
        "routes": extract_routes(root, max_files),
        "channels": extract_channels(root, max_files),
    }
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        return f"Exported graph snapshot to {out_path} ({len(files)} files, {len(snapshot['calls'])} modules).\n"
    except OSError as e:
        return f"Export failed: {e}\n"

def reachable(graph: dict, start: str, direction: str = "out") -> set:
    """BFS over the graph. direction='out' = what start depends on; 'in' = what depends on start."""
    seen: set = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        nxt = graph.get(cur, []) if direction == "out" else _dependents(graph, cur)
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

def render_calls(calls: dict, root: str, start: Optional[str] = None,
                 compact: Optional[bool] = None) -> str:
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
    rows = []
    for mod, funcs in sorted(calls.items()):
        for caller, callees in sorted(funcs.items()):
            if callees:
                rows.append("  %s.%s() -> %s" % (mod, caller, ", ".join(sorted(callees))))
    if _compact_default(compact):
        out = buf.getvalue() + "\n".join(_trim(rows, COMPACT_MAX_LIMIT)) + "\n"
        return _cap_output(out)
    for r in rows:
        buf.write(r + "\n")
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
            # local variables bound to imported classes: r = Registry()
            # so r.register() can resolve to the class's module.
            local_types: dict = {}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
                    tgt = sub.targets[0]
                    if isinstance(tgt, ast.Name) and isinstance(sub.value, ast.Call):
                        f = sub.value.func
                        if isinstance(f, ast.Name) and f.id in import_maps.get(mod, {}):
                            local_types[tgt.id] = import_maps[mod][f.id]
                    elif isinstance(tgt, ast.Name) and isinstance(sub.value, ast.Name):
                        # variable alias: r2 = r
                        if sub.value.id in local_types:
                            local_types[tgt.id] = local_types[sub.value.id]
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
                    elif isinstance(obj, ast.Name) and obj.id in local_types:
                        base = local_types[obj.id]
                        callees.add(f"{base}.{fn.attr}")
                    elif isinstance(obj, ast.Name) and obj.id in symbols.get(mod, {}):
                        # local class reference: Registry().register()
                        for q in symbols[mod][obj.id]:
                            callees.add(f"{q}.{fn.attr}")
                    elif isinstance(obj, ast.Call) and isinstance(obj.func, ast.Name) \
                            and obj.func.id in import_maps.get(mod, {}):
                        # inline instantiation: Registry().run(...)
                        base = import_maps[mod][obj.func.id]
                        callees.add(f"{base}.{fn.attr}")
                    else:
                        # method on a local class instance / self — best-effort
                        if fn.attr in symbols.get(mod, {}):
                            callees |= symbols[mod][fn.attr]
            # filter to codebase-defined symbols only
            callees = {c for c in callees if c in all_defined_q}
            if callees:
                calls[mod][caller] = callees
    # prune modules with no callers so header counts match rendered rows
    return {m: f for m, f in calls.items() if f}

def render_cross_calls(calls: dict, root: str, start: Optional[str] = None,
                       compact: Optional[bool] = None) -> str:
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
    rows = []
    for mod, funcs in sorted(calls.items()):
        for caller, callees in sorted(funcs.items()):
            if callees:
                rows.append("  %s.%s() -> %s" % (mod, caller, ", ".join(sorted(callees))))
    if _compact_default(compact):
        out = buf.getvalue() + "\n".join(_trim(rows, COMPACT_MAX_LIMIT)) + "\n"
        return _cap_output(out)
    for r in rows:
        buf.write(r + "\n")
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
    # Session-aware rerank: boost symbols that are in the agent's hot set /
    # already-deeply-understood, so search serves the current working state
    hot = set(get_hot_set(root))
    for r in results:
        if r["name"] in hot or r["module"] in hot or any(r["name"] in h for h in hot):
            r["score"] = round(r["score"] * 1.15, 2)
    results.sort(key=lambda x: -x["score"])
    buf = io.StringIO()
    buf.write(f"# hybrid search: {query}\n")
    if not results:
        buf.write("No symbols found.\n")
        return buf.getvalue()
    buf.write(f"{len(results)} result(s), scored (lexical + structure + git + session):\n\n")
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
    Searches code AND text/docs (markdown, config) so a repo-wide audit catches
    stale references in READMEs too."""
    q = query.lower()
    # tokenize query for word-boundary ranking
    q_words = [w for w in re.findall(r"[a-zA-Z0-9_]+", q) if len(w) > 1]
    results = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in LANG_RULES and ext not in IMPORT_LANG_RULES and ext not in _TEXT_EXTS:
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

def grep_symbolic(files: List[str], root: str, query: str, limit: int = 20) -> List[dict]:
    """Symbolic grep: matches in REAL CODE only — comments and string literals
    are stripped before matching (via the same scanner the import graph uses).
    Results ranked by symbol relevance: matches that land inside a known
    symbol's definition outrank loose module-level hits, and the enclosing
    symbol name is attached to every hit. Falls back to plain text matching
    for extensions with no code rules (docs/config)."""
    q = query.lower()
    q_words = [w for w in re.findall(r"[a-zA-Z0-9_]+", q) if len(w) > 1]
    results = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        is_code = ext in LANG_RULES or ext in IMPORT_LANG_RULES or ext in CALL_LANG_RULES
        mod = module_name_of(f, root)
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue

        # per-file: build a stripped twin (same line layout) + symbol map
        if is_code:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    clean_text = _strip_strings_comments(fh.read(), ext)
            except OSError:
                continue
            clean_lines = clean_text.splitlines()
            # symbol spans from the byte index: {start_line: end_line}
            spans = []
            idx = build_byte_index([f], root)
            for _name, locs in idx.items():
                for loc in locs:
                    s = loc.get("line") or 0
                    e = loc.get("end_line") or (s + len(
                        (loc.get("source") or "").splitlines()))
                    if s:
                        spans.append((s, e, _name, loc.get("kind", "")))
            spans.sort()
        else:
            clean_lines = lines  # docs/config: match as-is

        def enclosing(ln: int):
            """Innermost symbol containing this line."""
            best = None
            for s, e, name, kind in spans:
                if s <= ln <= e and (best is None or s >= best[0]):
                    best = (s, name, kind)
            return best

        for i, raw_line in enumerate(lines):
            code_line = clean_lines[i] if i < len(clean_lines) else ""
            hay = code_line.lower() if is_code else raw_line.lower()
            if q not in hay:
                continue
            word_hits = sum(1 for w in q_words if w in hay)
            score = word_hits * 10 + (5 if q in hay else 0)
            enc = enclosing(i + 1) if is_code else None
            if enc:
                score += 15  # inside a real definition beats loose hits
            snippet = "".join(lines[max(0, i - 1):i + 2]).rstrip()
            results.append({
                "module": mod, "path": f, "line": i + 1,
                "score": score, "snippet": snippet,
                "symbol": enc[1] if enc else None,
                "kind": enc[2] if enc else "",
            })
    results.sort(key=lambda r: (-r["score"], r["module"], r["line"]))
    return results[:limit]


def render_grep_symbolic(files: List[str], root: str, query: str,
                         limit: int = 20) -> str:
    results = grep_symbolic(files, root, query, limit)
    buf = io.StringIO()
    n_code = sum(1 for r in results if r.get("symbol"))
    buf.write(f"# symbolic grep: {query}  (code-only; comments/strings "
              f"excluded)\n")
    if not results:
        buf.write("No CODE matches found. Note: comments/strings are excluded "
                  "in this mode — use --grep to search everything.\n")
        return buf.getvalue()
    buf.write(f"{len(results)} match(es) ({n_code} inside definitions):\n\n")
    for r in results:
        sym = f"  in {r['kind']} `{r['symbol']}`" if r.get("symbol") else ""
        buf.write(f"  {r['module']}:{r['line']}{sym}\n")
        if r.get("snippet"):
            buf.write(f"    {r['snippet']}\n")
    buf.write("\n(use --grep for raw text search incl. docs/comments)\n")
    return buf.getvalue()


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
        ts_hit = find(ts_root)
        if ts_hit is not None:
            return ts_hit
        # tree-sitter parsed the file but didn't find the symbol — the file
        # may have syntax errors that hide it (ts drops broken nodes). Fall
        # through to the regex/brace path instead of returning None: --read
        # used to be NARROWER than --search/--get-symbol on broken files.

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
    """Extract a byte-range snippet from a file. Returns {text, tokens, bytes}.
    Byte-accurate: reads the file as bytes and slices the byte range, then
    decodes — the old text-mode slice used CHARACTER indices while the header
    advertises bytes, so multibyte files returned a different span than the
    byte range --get-symbol itself reports."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if start_byte < 0 or end_byte < start_byte or start_byte > len(data):
        return {"text": "", "tokens": 0, "bytes": 0, "invalid_range": True}
    end_byte = min(end_byte, len(data))  # clamp: an over-long END reads to EOF
    raw = data[start_byte:end_byte]
    snippet = raw.decode("utf-8", errors="replace")
    return {"text": snippet, "tokens": estimate_tokens(snippet), "bytes": len(raw)}

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
        graph_built = any(calls.values())
        if not graph_built:
            # honest: no call edges available — say so instead of "Calls (0)"
            summary_text = (
                f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                f"~{estimate_tokens(sig_str + doc_str)} tokens (summary)\n\n"
                f"Signature: {sig_str}\n"
                f"Docstring: {doc_str}\n"
                f"Calls: not built (run --index/--graph for call context)\n"
                f"Called by: not built (run --index/--graph for call context)\n"
                f"\nUse `--get-symbol {symbol} --full` for the full source.\n"
            )
            buf.write(summary_text)
            return buf.getvalue()
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
            if caller == "<module>":
                # synthetic bucket for module-level calls — it marks callees
                # as called but is not itself a defined symbol
                called |= set(callees)
                continue
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
# Code health (repowise parity, speed-first). Deterministic detectors over the
# resident index + call graph — one pass, zero LLM calls, no re-parse: hot data
# comes from the MCP _Index cache. Score is 0-10 per file; every finding names
# its detector and fix. NOT defect-validated like repowise's (that needs a
# labeled corpus) — this is a fast structural screen, honestly labeled.
# --------------------------------------------------------------------------- #

HEALTH_WEIGHTS = {
    "long_function": 1.0,      # >60 lines
    "too_many_params": 0.8,    # >5 params
    "deep_nesting": 0.8,       # indent depth > 4
    "dead_symbol": 0.6,        # defined, never called
    "god_imports": 0.5,        # >20 imports in one module
    "dup_name": 0.4,           # same symbol name defined in many modules
    "hotspot": 0.9,            # high fan-in: many callers depend on this file
    "large_file": 0.7,         # >600 lines in one module
}

def compute_health(files: List[str], root: str, index: dict, calls: dict,
                   texts: Optional[dict] = None) -> dict:
    """One-pass health scan. Returns {file: {"score": float, "findings": [...]}}
    plus aggregate "_summary". All detectors are cheap reads of structures we
    already have (byte index, call graph), so the whole pass is O(symbols)."""
    per_file: dict = {}
    def bucket(f):
        return per_file.setdefault(f, {"score": 10.0, "findings": []})
    def hit(f, detector, detail, weight):
        b = bucket(f)
        b["score"] -= weight
        b["findings"].append({"detector": detector, "detail": detail})

    # --- module-level detectors -------------------------------------------
    for name, locs in index.items():
        for loc in locs:
            f = loc.get("path") or ""
            mod = loc.get("module", "?")
            kind = (loc.get("kind") or "").lower()
            line = loc.get("line", 0)
            # params + length come from the symbol's own source (first line
            # carries the signature; source is present on fresh indexes)
            src = (loc.get("source") or "") or (loc.get("sig") or "")
            first = src.split("\n")[0] if src else ""
            if "(" in first and ")" in first and kind != "class":
                inner = first[first.index("(") + 1:first.rindex(")")]
                cleaned = inner.replace("self", "").replace("cls", "")
                nparams = len([p for p in cleaned.split(",") if p.strip()])
                if nparams > 5 and f:
                    hit(f, "too_many_params", "%s takes %d params (%s:%d)" % (name, nparams, mod, line),
                        HEALTH_WEIGHTS["too_many_params"])
            # long function: count source lines when we have them
            if src and kind == "function" and f:
                length = len(src.rstrip().splitlines())
                if length > 60:
                    hit(f, "long_function", "%s spans %d lines (%s:%d)" % (name, length, mod, line),
                        HEALTH_WEIGHTS["long_function"])
        # duplicate symbol names across modules (design smell / confusion risk)
        defining_mods = {l.get("module") for l in locs}
        if len(defining_mods) >= 4:
            for l in locs:
                f = l.get("path") or ""
                if f:
                    hit(f, "dup_name", "'%s' defined in %d modules" % (name, len(defining_mods)),
                        HEALTH_WEIGHTS["dup_name"])

    # imports per module from the call-graph structure (graph = import edges)
    try:
        graph = build_call_graph_multi(files, root)
        for m, funcs in graph.items():
            if len(funcs) == 0:
                continue
    except Exception:
        graph = {}

    # dead symbols from the call graph: defined but never called AND never
    # calling anything (entry points like main() are exempt via name check).
    called = set()
    callers = set()
    for m, funcs in calls.items():
        for fn, outs in funcs.items():
            called.update(outs)
            if outs:
                callers.add(fn)
    entry_like = ("main", "test_", "setUp", "tearDown")
    for name, locs in index.items():
        if name in called or any(name.startswith(p) for p in entry_like):
            continue
        for loc in locs:
            f = loc.get("path") or ""
            kind = (loc.get("kind") or "").lower()
            mod = loc.get("module", "?")
            if f and kind in ("function", "method"):
                # function with outgoing call edges is live wiring, not dead
                if name in callers:
                    continue
                hit(f, "dead_symbol", "'%s' never called (%s:%d)" % (
                    name, mod, loc.get("line", 0)),
                    HEALTH_WEIGHTS["dead_symbol"])

    # --- file-level risk signals (heuristic defect correlates, honestly
    # labeled: these mirror what the defect-prediction literature uses —
    # size and fan-in/centrality — but are NOT validated against a labeled
    # corpus the way repowise's scores are).
    fan_in: dict = {}
    for m, funcs in calls.items():
        for fn, outs in funcs.items():
            for o in outs:
                fan_in[o] = fan_in.get(o, 0) + 1
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                nlines = sum(1 for _ in fh)
        except OSError:
            continue
        if nlines > 600:
            hit(f, "large_file", "%d lines in one module" % nlines,
                HEALTH_WEIGHTS["large_file"])
        # hotspot: module whose symbols carry high fan-in (many dependents)
        mod_syms = [name for name, locs in index.items() for l in locs
                    if l.get("path") == f]
        deg = sum(1 for s in mod_syms if fan_in.get(s, 0) >= 5)
        if deg >= 3:
            hit(f, "hotspot", "%d heavily-depended-on symbols (change with care)" % deg,
                HEALTH_WEIGHTS["hotspot"])

    for f in per_file:
        per_file[f]["score"] = round(max(0.0, min(10.0, per_file[f]["score"])), 1)
    worst = sorted(per_file.items(), key=lambda kv: kv[1]["score"])[:10]
    total_findings = sum(len(v["findings"]) for v in per_file.values())
    avg = round(sum(v["score"] for v in per_file.values()) / len(per_file), 2) if per_file else 10.0
    return {
        "files": per_file,
        "_summary": {
            "avg_score": avg,
            "files_scanned": len(files),
            "files_with_findings": len(per_file),
            "total_findings": total_findings,
            "worst": [{"file": os.path.relpath(f, root), "score": v["score"],
                       "top": (v["findings"][0]["detail"] if v["findings"] else "")}
                      for f, v in worst],
        },
    }

def render_health(files: List[str], root: str, index: Optional[dict] = None,
                  calls: Optional[dict] = None,
                  compact: Optional[bool] = None) -> str:
    """--health: per-file 0-10 score + findings, ranked worst-first.
    Compact mode (MCP default / --compact) is summary-first: totals line +
    top-5 worst modules with a one-line why, instead of the full findings
    dump that costs agents ~4k tokens on real repos."""
    t0 = time.time()
    if index is None:
        index = build_byte_index(files, root)
    if calls is None:
        calls = build_call_graph_multi(files, root)
    result = compute_health(files, root, index, calls)
    dt = time.time() - t0
    s = result["_summary"]
    buf = io.StringIO()
    buf.write("# code health — avg %.1f/10 across %d files (%d findings, %.2fs, zero LLM)\n"
              % (s["avg_score"], s["files_scanned"], s["total_findings"], dt))
    if not s["worst"]:
        buf.write("No structural findings. Clean.\n")
        return buf.getvalue()
    if _compact_default(compact):
        # summary-first: top-5 worst modules, name + score + one-line why.
        # The full dump stays available via --full (CLI) or --full (MCP).
        buf.write("\n## Worst files (top 5 of %d with findings; use --full for all)\n"
                  % s["files_with_findings"])
        rows = []
        for w in s["worst"][:COMPACT_MAX_LIMIT]:
            rows.append("  %s — %.1f/10 · %s" % (w["file"], w["score"], w["top"]))
        if s["files_with_findings"] > COMPACT_MAX_LIMIT:
            rows.append("  ... %d more files with findings (use --full)"
                        % (s["files_with_findings"] - COMPACT_MAX_LIMIT))
        buf.write("\n".join(rows) + "\n")
        return _cap_output(buf.getvalue())
    buf.write("\n## Worst files\n")
    for w in s["worst"]:
        buf.write("  %s — %.1f/10 · %s\n" % (w["file"], w["score"], w["top"]))
    buf.write("\n## All findings\n")
    for f, v in sorted(result["files"].items(), key=lambda kv: kv[1]["score"]):
        rel = os.path.relpath(f, root)
        for fd in v["findings"]:
            buf.write("  [%s] %s — %s\n" % (fd["detector"], rel, fd["detail"]))
    out = buf.getvalue()
    lines = out.splitlines()
    if len(lines) > 200:
        out = "\n".join(lines[:200]) + "\n(truncated to 200 lines)\n"
    return out

# --------------------------------------------------------------------------- #
# Change risk (clean-room implementation on codeloom primitives + stdlib git).
# Scores a revspec's diff for review priority from signals the literature ties
# to defects: diff size, spread across files, churned health-findings, fan-in
# of touched symbols, and the file's own recent history. Output is a 0-100
# score + band + per-file drivers. Zero LLM, sub-second, no network.
# --------------------------------------------------------------------------- #

def _git_out(root: str, argv: List[str]) -> Optional[str]:
    try:
        import subprocess as _sp
        r = _sp.run(["git"] + argv, cwd=root, capture_output=True,
                    text=True, timeout=30)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None

RISK_WEIGHTS = {
    "added_lines": 0.06,      # per added line (diminishing below)
    "deleted_lines": 0.08,    # deletions correlate with fixes/refactors
    "file_spread": 2.2,       # per additional file touched beyond the first
    "health_hit": 3.0,        # per active health finding in a touched file
    "hot_symbol": 4.0,        # touching a symbol with fan-in >= 5
    "recent_fix_file": 5.0,   # file had fix-typed commits in last 90 days
}

def compute_change_risk(root: str, index: dict, calls: dict,
                        revspec: str = "HEAD~1..HEAD") -> dict:
    """Risk assessment for a commit/range. Returns {score, band, drivers[],
    files[]} — deterministic given the same repo state and range."""
    import re as _re
    # --- diff shape -------------------------------------------------------
    numstat = _git_out(root, ["diff", "--numstat", revspec])
    if numstat is None:
        # Single-commit repo: HEAD~1..HEAD has no parent. Diff against the
        # empty tree instead of lying "not a git repo".
        if revspec == "HEAD~1..HEAD":
            n = _git_out(root, ["rev-list", "--count", "HEAD"])
            if n is not None and n.strip() == "1":
                numstat = _git_out(
                    root, ["diff-tree", "--numstat", "-r", "HEAD"])
                if numstat is not None:
                    revspec = "(initial commit)"
    if numstat is None:
        return {"error": "not a git repo or unknown revspec '%s'" % revspec}
    files_touched = []
    added = deleted = 0
    for ln in numstat.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 3:
            try:
                a, d = int(parts[0]), int(parts[1])
            except ValueError:
                continue  # binary rows show '-'
            added += a
            deleted += d
            files_touched.append(parts[2])
    if not files_touched:
        return {"score": 0.0, "band": "none",
                "summary": "no textual changes in '%s'" % revspec,
                "drivers": [], "files": []}

    score = 0.0
    drivers = []
    def add(pts, why):
        nonlocal score
        if pts <= 0:
            return
        score += pts
        drivers.append({"points": round(pts, 1), "why": why})

    add(min(added * RISK_WEIGHTS["added_lines"], 18.0),
        "%d added lines" % added)
    add(min(deleted * RISK_WEIGHTS["deleted_lines"], 15.0),
        "%d deleted lines" % deleted)
    add(max(0, len(files_touched) - 1) * RISK_WEIGHTS["file_spread"],
        "touches %d files" % len(files_touched))

    # --- health findings inside touched files ------------------------------
    health = compute_health(files_touched, root, index, calls)
    hf = sum(len(v["findings"]) for k, v in health["files"].items())
    add(min(hf * RISK_WEIGHTS["health_hit"], 20.0),
        "%d open health findings in touched files" % hf)

    # --- hot symbols: did the diff touch high-fan-in definitions? ----------
    fan_in = {}
    for _m, funcs in calls.items():
        for _fn, outs in funcs.items():
            for o in outs:
                fan_in[o] = fan_in.get(o, 0) + 1
    patch = _git_out(root, ["diff", "-U0", revspec]) or ""
    hot_hits = []
    for name, deg in sorted(fan_in.items(), key=lambda kv: -kv[1]):
        if deg < 5:
            break
        if _re.search(r"\b%s\b" % _re.escape(name), patch):
            hot_hits.append((name, deg))
            if len(hot_hits) >= 5:
                break
    for name, deg in hot_hits:
        add(RISK_WEIGHTS["hot_symbol"], "touches '%s' (%d callers depend on it)" % (name, deg))

    # --- recent fix-history of touched files --------------------------------
    fix_count = 0
    for f in files_touched[:20]:
        log = _git_out(root, ["log", "--oneline", "--since=90 days ago", "--", f])
        if log and _re.search(r"\b(fix|bug|regression|hotfix|patch)\b",
                              log.lower()):
            fix_count += 1
    if fix_count:
        add(fix_count * RISK_WEIGHTS["recent_fix_file"],
            "%d touched file(s) had recent fix commits" % fix_count)

    score = round(min(100.0, score), 1)
    band = ("low" if score < 25 else "medium" if score < 50
            else "high" if score < 75 else "critical")
    return {
        "score": score, "band": band, "drivers": drivers,
        "revspec": revspec,
        "files": [{"path": f, "health_findings":
                   len(health["files"].get(f, {}).get("findings", []))}
                  for f in files_touched],
    }

def render_change_risk(files: List[str], root: str, revspec: str = "HEAD~1..HEAD") -> str:
    """--risk: pre-merge change-risk report for a commit or range."""
    t0 = time.time()
    index = build_byte_index(files, root)
    calls = build_call_graph_multi(files, root)
    r = compute_change_risk(root, index, calls, revspec)
    dt = time.time() - t0
    if "error" in r:
        return "# change risk\n\n%s\n" % r["error"]
    buf = io.StringIO()
    buf.write("# change risk — %s\n" % r.get("revspec", revspec))
    buf.write("score %.0f/100 [%s] (%.2fs, zero LLM)\n\n" % (r["score"], r["band"], dt))
    buf.write("## Drivers\n")
    if not r["drivers"]:
        buf.write("  none — minimal diff\n")
    for d in r["drivers"]:
        buf.write("  +%.0f  %s\n" % (d["points"], d["why"]))
    buf.write("\n## Touched files\n")
    for f in r["files"]:
        n = f["health_findings"]
        buf.write("  %s%s\n" % (f["path"], "  (%d health finding%s)" % (n, "s" if n != 1 else "") if n else ""))
    buf.write("\nBand meaning: low<25 medium<50 high<75 critical>=75 "
              "(heuristic percentile-style scale; NOT defect-validated against "
              "a labeled corpus).\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Structural pattern search (ast-grep-style, pure-Python over real ASTs).
# Pattern syntax: $NAME captures one identifier/expression, $$$BODY captures a
# rest-arg. Example: "try: $BODY except Exception: pass" or "$F($$$A)".
# Neither codebase-memory nor codegraph ship this; ast-grep needs its binary.
# We match on Python via the stdlib ast module — zero deps, always available.
# --------------------------------------------------------------------------- #

def _pattern_to_ast(pattern: str):
    """Compile a pattern like '$F($$$ARGS)' into an AST template with
    metavariables marked. Returns (tree, meta_names) or None if unparseable."""
    import ast as _ast
    import re as _re
    # $ / $$$ aren't valid Python identifier chars — swap to safe placeholders
    # (__codo_meta_NAME) before parsing, so '$F($$$ARGS)' becomes parseable.
    def _sub(m):
        return "__codo_meta_" + (m.group(1) or m.group(2))
    safe = _re.sub(r"\$\$\$([A-Za-z_][A-Za-z0-9_]*)|\$([A-Za-z_][A-Za-z0-9_]*)", _sub, pattern)
    tree = None
    # expressions first ($F($$$ARGS)); statements second (try/except blocks)
    for mode in ("eval", "exec"):
        try:
            parsed = _ast.parse(safe.strip(), mode=mode)
            tree = parsed.body[0] if mode == "exec" else parsed.body
            break
        except SyntaxError:
            continue
    if tree is None:
        return None
    return (tree, [])


def _ast_matches(template, node, binds: dict) -> bool:
    """Structural match of template against node; metavariables bind."""
    import ast as _ast
    META = "__codo_meta_"
    # metavariable: matches anything, records binding
    if isinstance(template, _ast.Name) and template.id.startswith(META):
        raw = template.id[len(META):]
        key = raw[3:] if raw.startswith("rest_") else raw
        if raw.startswith("rest_"):
            binds.setdefault(key, []).append(node)
            return True
        prev = binds.get(key)
        if prev is not None and not isinstance(prev, _ast.AST):
            return False  # multi-bind conflict
        binds[key] = node
        return True
    if type(template) is not type(node):
        return False
    for field in template._fields:
        tv = getattr(template, field)
        nv = getattr(node, field)
        if isinstance(tv, _ast.AST):
            if not isinstance(nv, _ast.AST):
                return False
            if not _ast_matches(tv, nv, binds):
                return False
        elif isinstance(tv, list):
            if not isinstance(nv, list):
                return False
            ti = ni = 0
            while ti < len(tv):
                t_item = tv[ti]
                if isinstance(t_item, _ast.Name) and t_item.id.startswith("__codo_meta_rest_"):
                    rest_key = t_item.id[len("__codo_meta_rest_"):]
                    binds[rest_key] = nv[ni:]
                    ti += 1
                    ni = len(nv)  # $$$ consumes the remainder
                    break
                if ni >= len(nv):
                    return False
                if isinstance(t_item, _ast.AST):
                    if not isinstance(nv[ni], _ast.AST):
                        return False
                    if not _ast_matches(t_item, nv[ni], binds):
                        return False
                elif tv[ti] != nv[ni]:
                    return False
                ti += 1
                ni += 1
            else:
                if ni != len(nv):
                    return False
        elif tv != nv:
            return False
    return True


def render_pattern_search(files: List[str], root: str, pattern: str,
                          limit: int = 30) -> str:
    """--pattern: structural search. Finds every code site whose AST matches
    the pattern shape, with captured metavariables shown per hit."""
    import ast as _ast
    compiled = _pattern_to_ast(pattern)
    if compiled is None:
        return "# pattern search\n\nunparseable pattern: %r\n" % pattern
    template, _metas = compiled

    hits = []
    t0 = time.time()
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext != ".py":
            continue  # v1: Python ASTs (stdlib); other langs via regex fallback
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            tree = _ast.parse(src)
        except (SyntaxError, OSError):
            continue
        for node in _ast.walk(tree):
            binds: dict = {}
            if _ast_matches(template, node, binds):
                line = getattr(node, "lineno", 0)
                snippet = src.splitlines()[line - 1].strip()[:120] if line else ""
                caps = {}
                for k, v in binds.items():
                    if v is None:
                        continue
                    try:
                        caps[k] = (_ast.unparse(v[0])[:60]
                                   if isinstance(v, list) and v
                                   else _ast.unparse(v)[:60])
                    except Exception:
                        caps[k] = "..."
                hits.append({"file": os.path.relpath(f, root), "line": line,
                             "snippet": snippet, "captures": caps})
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break
    dt = time.time() - t0

    buf = io.StringIO()
    buf.write("# pattern search — %r (%d match(es), %.2fs, zero LLM)\n\n"
              % (pattern, len(hits), dt))
    if not hits:
        buf.write("No structural matches.\n")
        return buf.getvalue()
    for h in hits:
        buf.write("%s:%d  %s\n" % (h["file"], h["line"], h["snippet"]))
        for k, v in h["captures"].items():
            buf.write("    $%s = %s\n" % (k, v))
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# Incremental / indexed mode (hash-based cache, no daemon)
# --------------------------------------------------------------------------- #

CACHE_VERSION = 2

def _file_hash(path: str) -> str:
    """Return a content hash for a file. Full-file SHA-256: the old
    mtime+size+first-8KB sample produced IDENTICAL hashes for same-length
    edits past byte 8192 with preserved mtime (rsync -a / cp -p / touch -r),
    so --incremental and the resident index silently missed real edits."""
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
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
    Returns the list of locs, or None if the symbol isn't present/loaded.
    SECURITY: the dbm store is repo-supplied and was previously trusted
    verbatim — a crafted store with path '../secret.py' made --get-symbol
    read files OUTSIDE the repo. Locs whose resolved path escapes the root
    are dropped here."""
    try:
        import dbm
        path = _index_lazy_path(root)
        with dbm.open(path, "r") as db:
            raw = db.get(symbol.encode("utf-8"))
        if raw is None:
            return None
        locs = json.loads(raw.decode("utf-8"))
        if not isinstance(locs, list):
            return None
        root_real = os.path.realpath(root)
        kept = []
        for loc in locs:
            if not isinstance(loc, dict):
                continue
            p = loc.get("path")
            if p is None:
                kept.append(loc)
                continue
            resolved = os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p))
            if resolved.startswith(root_real + os.sep):
                kept.append(loc)
        return kept or None
    except Exception:
        return None

def _read_source_from_loc(loc: dict, root: str) -> str:
    """Re-read a symbol's full source from disk using the stored byte range.
    Used by --full and the adaptive small-symbol path, since the persisted
    index no longer stores full source strings (they make it multi-GB).
    SECURITY: resolves through realpath and refuses paths outside the root —
    a crafted index entry pointing through an in-repo symlink used to read
    files outside the repo."""
    path = loc.get("path")
    sb, eb = loc.get("start_byte", 0), loc.get("end_byte", 0)
    if not path or not os.path.isabs(path):
        path = os.path.join(root, path) if path else None
    if path:
        try:
            root_real = os.path.realpath(root)
            if not os.path.realpath(path).startswith(root_real + os.sep):
                return loc.get("source", "") or ""
        except OSError:
            return loc.get("source", "") or ""
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

# The accelerator's own source/binary files. They live next to codeloom.py,
# so a repo-root --engine c/rust walk would otherwise index the engine
# itself — junk symbols from codeloom_core.c (its libc helpers, etc.) would
# appear as if they were the target repo's code. Never index these.
_ENGINE_SELF_FILES = frozenset({
    "codeloom_core.c", "codeloom_core", "codeloom_core.exe",
    "codeloom_core_rs.rs", "codeloom_core_rs", "codeloom_core_rs.exe",
    "codeloom_rs",
})

def _find_core_engine(engine: str = "c") -> Optional[str]:
    """Locate the compiled accelerator binary (C or Rust) next to codeloom.py.
    engine='c' -> codeloom_core; engine='rust' -> codeloom_core_rs.
    Auto-builds a missing OR STALE core from its committed source (no download)
    when the matching compiler is present: cc for the C core, rustc for the
    Rust core — the binary is stale whenever the committed .c/.rs source has a
    newer mtime (a shipped binary can lag its source after source-only fixes,
    and a silent stale binary was exactly the 2026-08-27 HA-core finding: the
    committed core emitted ZERO import edges and the CLI never complained).
    Both engines stay symmetric: optimization hints that fail loudly (with an
    honest per-compiler message) only when the source AND the compiler are
    both absent."""
    name = "codeloom_core" if engine == "c" else "codeloom_core_rs"
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, name), os.path.join(here, name + ".exe")]
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            if not _binary_matches_platform(c):
                break  # foreign-OS binary (e.g. Mach-O on Linux) — rebuild below
            if not _core_is_stale(here, name, c):
                return c
            # stale binary — rebuild below; do NOT silently serve it
    import shutil
    on_path = shutil.which(name)
    if on_path and os.path.isfile(on_path) and os.access(on_path, os.X_OK):
        if _binary_matches_platform(on_path) and not _core_is_stale(os.path.dirname(on_path), name, on_path):
            return on_path
        # a stale PATH binary: rebuild next to codeloom.py below
    # not built or stale — auto-compile from committed source (no download)
    import subprocess as _sp
    if engine == "c":
        src = os.path.join(here, "codeloom_core.c")
        if os.path.isfile(src) and shutil.which("cc"):
            out = os.path.join(here, name)
            try:
                r = _sp.run(["cc", "-O3", "-o", out, src], capture_output=True, text=True, timeout=180)
                if r.returncode == 0 and os.path.isfile(out):
                    return out
            except Exception:
                pass
    elif engine == "rust":
        src = os.path.join(here, "codeloom_core_rs.rs")
        if os.path.isfile(src) and shutil.which("rustc"):
            out = os.path.join(here, name)
            try:
                r = _sp.run(["rustc", "-O", "-o", out, src], capture_output=True, text=True, timeout=180)
                if r.returncode == 0 and os.path.isfile(out):
                    return out
            except Exception:
                pass
    return None

def _binary_matches_platform(path: str) -> bool:
    """True if an existing binary can run on THIS OS (magic-byte check).
    Committed accelerator binaries are built on the maintainer's macOS
    (Mach-O) — a Linux/Windows checkout must NOT serve them (running a
    Mach-O on Linux yields empty output and '--engine c produced no
    symbols', the 2026-08-27 CI failure on all non-macOS runners). A
    foreign-OS binary is treated as missing: the finders auto-rebuild from
    committed source instead. Shebang scripts are portable and pass."""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return False
    if not magic:
        return False
    # #! scripts run anywhere — let them through (empty output is caught by
    # the zero-symbols guard, not here)
    if magic[:2] == b"#!":
        return True
    if sys.platform == "darwin":
        return magic[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
                             b"\xca\xfe\xba\xbe")
    if sys.platform.startswith("linux"):
        return magic[:4] == b"\x7fELF"
    if sys.platform in ("win32", "cygwin"):
        return magic[:2] == b"MZ"
    return True  # unknown platform: never block


def _find_rs_watcher() -> Optional[str]:
    """Locate the standalone Rust CLI (codeloom_rs) that has the `watch`
    subcommand. The single-file scan core (codeloom_core_rs) does NOT have a
    watch mode — passing it 'watch ROOT' silently scans an empty stdin and
    exits, which --watch used to mistake for a working watcher."""
    name = "codeloom_rs"
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, name), os.path.join(here, name + ".exe")]
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            if _binary_matches_platform(c):
                return c
    import shutil
    return shutil.which(name)

def _core_is_stale(here: str, name: str, binary: str) -> bool:
    """True if the accelerator binary is older than its committed source.
    mtime comparison (like the --verify-edit / docs sidecar pattern):
    source-only fixes land in the .c/.rs file but the shipped binary can lag
    behind — a stale binary must be rebuilt, never silently served."""
    src = os.path.join(here, name + (".c" if name == "codeloom_core" else ".rs"))
    if not os.path.isfile(src):
        return False
    try:
        return os.path.getmtime(src) > os.path.getmtime(binary) + 1
    except OSError:
        return False


def _find_core() -> Optional[str]:
    """Locate the compiled codeloom_core binary next to codeloom.py or on PATH.
    If it's not built (or is STALE vs codeloom_core.c), auto-build it from the
    committed source (no download — compiles locally with cc). Returns None
    only if cc is unavailable or the source is missing."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, _CORE_NAME), os.path.join(here, _CORE_NAME + ".exe")]
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            if not _binary_matches_platform(c):
                break  # foreign-OS binary — rebuild below
            if not _core_is_stale(here, _CORE_NAME, c):
                return c
            break  # stale local binary — rebuild it below
    import shutil
    on_path = shutil.which(_CORE_NAME)
    if on_path and os.path.isfile(on_path) and os.access(on_path, os.X_OK):
        if _binary_matches_platform(on_path) and not _core_is_stale(os.path.dirname(on_path), _CORE_NAME, on_path):
            return on_path
    # not built or stale — auto-build from committed source (integrated, no download)
    core_src = os.path.join(here, "codeloom_core.c")
    if os.path.isfile(core_src) and shutil.which("cc"):
        import subprocess as _sp
        out = os.path.join(here, _CORE_NAME)
        try:
            r = _sp.run(["cc", "-O3", "-o", out, core_src], capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and os.path.isfile(out):
                return out
        except Exception:
            pass
    return None

def _c_walk(root: str, engine: str = "c") -> List[str]:
    """List code files via the accelerator's fast walker (--list ROOT). Falls back
    to Python _walk on any error. Much faster than Python os.walk + gitignore
    matching on huge repos. engine='c' -> C core; 'rust' -> Rust core."""
    core = _find_core_engine(engine)
    if not core:
        return []
    import subprocess
    try:
        r = subprocess.run([core, "--list", root], capture_output=True, text=True, timeout=300)
    except Exception:
        return []
    out = [l for l in r.stdout.splitlines() if l.strip()]
    # Skip artifact/build dirs the Python walker excludes (gitignore parity
    # plus target/: the Rust engine's build dir pollutes the file list with
    # binary .rlib/.d files whose bytes kill a text-mode shard decode — the
    # 2026-08-23 dogfood failure: --engine c on codeloom itself returned
    # 'no symbols' from 81 binary .rlib files).
    _C_SKIP_DIRS = (".git", "node_modules", ".venv", "venv", "__pycache__",
                    "dist", "build", "target", ".cargo", ".tox", ".mypy_cache",
                    ".pytest_cache", ".ruff_cache", ".eggs", ".cache")
    out_norm = []
    for l in out:
        rel = l.lstrip("./").replace("\\", "/")  # normalize sep for skip match
        if os.path.basename(rel.rstrip("/")) in _ENGINE_SELF_FILES:
            continue
        if os.path.splitext(rel)[1].lower() not in CALL_LANG_RULES:
            continue
        if any(("/" + d + "/") in ("/" + rel + "/") for d in _C_SKIP_DIRS):
            continue
        out_norm.append(os.path.join(root, rel) if not os.path.isabs(l) else l)
    return out_norm

def _c_scan(files: List[str], engine: str = "c") -> List[dict]:
    """Run the accelerator core over files. Returns per-file dicts
    {file, symbols:[{name,kind,line,start_byte,end_byte,sig?}], imports:[...],
    calls:[...]}. Empty on error. A core emitting only {name,kind} (older
    binaries) is handled by _c_symbol_index's precise-extraction fallback.
    Shards the file list across parallel core processes (each core is
    single-threaded; on a 64k-file kernel repo this turns ~80s of scanning
    into ~15-20s across cores). Each shard uses stdin mode (no argv limits).
    engine='c' -> C core; 'rust' -> Rust core (codeloom_core_rs)."""
    core = _find_core_engine(engine)
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

def _aggregate_precise_by_path(precise_idx: dict) -> dict:
    """Re-key {symbol: [locs]} into {path: [(symbol, loc)]} in O(symbols).
    The old per-file filter (`for name, locs in precise_idx.items()` inside
    the per-file loop) was O(files x symbols) — ~2.5B path comparisons on
    HA-core (18k files x 137k symbol-locs), the same quadratic pattern the
    suffix index killed elsewhere. Pure restructuring; output identical."""
    by_path: dict = {}
    for name, locs in precise_idx.items():
        for loc in locs:
            by_path.setdefault(loc.get("path"), []).append((name, loc))
    return by_path


def _c_symbol_index(files: List[str], root: str, scan: Optional[List[dict]] = None) -> dict:
    """Build a symbol index (name -> locs) using the C core's fast scan.
    Faster than Python parsing; used by --engine c. Snippet is the def line.
    `scan` is an optional pre-computed _c_scan() result (avoids re-scanning).

    Records carry the full persistent-index schema (module, path, kind, line,
    start_byte, end_byte, tokens, source) so --get-symbol/--read/--cross work
    identically for C-engine and Python-engine indexes. A core that emits
    per-symbol spans (line/start_byte/end_byte — codeloom_core.c) is used
    directly; a core that emits only {name, kind} (e.g. the Rust core) falls
    back to the precise Python extractors per file, keeping --engine c
    byte-accurate on every supported core.
    """
    idx: dict = {}
    if scan is None:
        scan = _c_scan(files)
    # precompute file->module ONCE per file (avoid 3.2M os.path.relpath calls)
    mod_map = {}
    for fr in scan:
        p = fr.get("file", "")
        if p:
            mod_map[p] = module_name_of(p, root)
    # files whose core records lack per-symbol spans -> precise re-extract.
    # .py ALWAYS goes precise: the core's dedent-based Python spans truncate
    # complex bodies (decorators, nested defs, multi-line signatures) — the
    # 2026-08-23 flask benchmark showed wsgi_app at 22 tok vs the Python
    # engine's 471 tok. The core still defines the fast symbol SET; the
    # precise pass supplies correct offsets/source for it.
    need_precise = {}
    for fr in scan:
        path = fr.get("file", "")
        if not path:
            continue
        syms = fr.get("symbols", []) or []
        ext = os.path.splitext(path)[1].lower()
        if ext == ".py":
            need_precise[path] = ext
        elif syms and not any("line" in s or "start_byte" in s for s in syms):
            if ext in CALL_LANG_RULES:
                need_precise[path] = ext
    precise_idx: dict = {}
    precise_by_path: dict = {}
    if need_precise:
        # parallel precise extraction (the serial re-AST of every .py is why
        # --engine c ran SLOWER than the py engine on HA-core: C scan + full
        # serial Python parse = strictly more work). Multiprocessing on >=100
        # files; serial otherwise (spawn overhead dominates on tiny repos).
        if len(need_precise) >= 100:
            import multiprocessing as mp
            work = [(path, root) for path in need_precise]
            with mp.Pool() as pool:
                per_files = pool.map(_index_file_worker, work)
            for per_file in per_files:
                for name, locs in per_file.items():
                    precise_idx.setdefault(name, []).extend(locs)
        else:
            for path, ext in need_precise.items():
                mod = mod_map.get(path) or module_name_of(path, root)
                if ext == ".py":
                    _index_python_bytes(path, mod, precise_idx)
                else:
                    _index_other_bytes(path, mod, ext, precise_idx)
        # keep only the names the core actually saw (its scanner defines the
        # symbol set; the precise pass only supplies offsets/source for them)
        core_names = set()
        for fr in scan:
            for s in fr.get("symbols", []):
                core_names.add(s.get("name", ""))
        for name in list(precise_idx.keys()):
            if name not in core_names:
                del precise_idx[name]
        # index precise locations BY PATH once (O(symbols)) — the per-file
        # O(symbols) scan would otherwise be O(files x symbols): ~2.5B
        # path comparisons on HA-core (18k files x 137k symbol-locs).
        precise_by_path = _aggregate_precise_by_path(precise_idx)
    for fr in scan:
        path = fr.get("file", "")
        if not path:
            continue
        mod = mod_map.get(path) or module_name_of(path, root)
        # precise extraction exists for this file -> reuse it verbatim
        if path in need_precise:
            for name, loc in precise_by_path.get(path, ()):
                idx.setdefault(name, []).append(loc)
            continue
        for s in fr.get("symbols", []):
            name = s.get("name", "")
            if not name:
                continue
            line = s.get("line")
            sb = s.get("start_byte")
            eb = s.get("end_byte")
            if line is not None and sb is not None and eb is not None:
                src = s.get("source", "")
                if not src and eb > sb:
                    try:
                        with open(path, "r", encoding="utf-8",
                                  errors="replace") as fh:
                            fh.seek(sb)
                            src = fh.read(eb - sb)
                    except (OSError, ValueError):
                        src = ""
                if src and eb - sb > 0:
                    idx.setdefault(name, []).append({
                        "module": mod, "path": path, "kind": "function",
                        "line": line, "start_byte": sb, "end_byte": eb,
                        "tokens": estimate_tokens(src), "source": src,
                    })
                    continue
                # span present but source unreadable -> still record offsets
                idx.setdefault(name, []).append({
                    "module": mod, "path": path, "kind": "function",
                    "line": line, "start_byte": sb, "end_byte": eb,
                    "tokens": 3, "source": "",
                })
                continue
            # core gave no span at all -> precise re-extract for this file
            ext = os.path.splitext(path)[1].lower()
            if ext == ".py" or ext in CALL_LANG_RULES:
                per_file: dict = {}
                if ext == ".py":
                    _index_python_bytes(path, mod, per_file)
                else:
                    _index_other_bytes(path, mod, ext, per_file)
                locs = per_file.get(name)
                if locs:
                    idx.setdefault(name, []).extend(locs)
                    continue
            # last resort: name/line only (keeps --cross/--search working)
            idx.setdefault(name, []).append({
                "module": mod, "path": path, "kind": "function", "line": 1,
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
            # calls may be structured {"caller":..,"targets":[..]} (C core) or
            # flat strings (Rust core). Handle both — flat strings contribute no
            # resolved call edge (no caller known) but never crash the graph.
            if isinstance(c, str):
                continue
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

def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _git_commit(root: str) -> Optional[str]:
    """HEAD commit for the freshness envelope, or None outside a repo."""
    if not os.path.isdir(os.path.join(root, ".git")):
        return None
    try:
        import subprocess as _sp
        r = _sp.run(["git", "rev-parse", "HEAD"], cwd=root,
                    capture_output=True, text=True, timeout=5)
        out = (r.stdout or "").strip()
        return out or None
    except Exception:
        return None

def meta_envelope(root: str) -> dict:
    """Freshness envelope attached to every MCP response (repowise parity):
    how old is the index, which commit it was built at, and does live state
    diverge (stale_warning). Agents read this before trusting any result."""
    pidx = load_persistent_index(root)
    if not pidx:
        return {"indexed": False, "index_age_days": None,
                "indexed_commit": None, "stale_warning": True}
    age_days = None
    ts = pidx.get("built_at")
    if ts:
        try:
            import datetime
            built = datetime.datetime.fromisoformat(ts)
            if built.tzinfo is None:
                built = built.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            age_days = round((now - built).total_seconds() / 86400, 2)
        except ValueError:
            pass
    commit = pidx.get("commit")
    if commit is None and os.path.isdir(os.path.join(root, ".git")):
        commit = _git_commit(root)
    try:
        stale = not index_is_fresh(root, pidx, sample=50)
    except Exception:
        stale = True
    return {"indexed": True, "index_age_days": age_days,
            "indexed_commit": commit, "stale_warning": stale}

# --------------------------------------------- task-shaped tools (repowise parity)
def render_context_card(files: List[str], root: str, targets: List[str]) -> str:
    """Batch triage card: pass N symbol names in ONE call, get per-target
    definition, same-module signatures, callers count, governing ADR titles.
    Collapses the search->read->impact chain into a single round-trip."""
    index = build_byte_index(files, root)
    cg = build_call_graph_multi(files, root)
    cg_built = any(cg.values())
    # callers count per symbol name across all modules
    callers = {}
    for _mod, funcs in cg.items():
        for _fn, outs in funcs.items():
            for o in outs:
                callers[o] = callers.get(o, 0) + 1
    adr_dir = os.path.join(root, ".codeloom-memory", "adr")
    adrs = []
    if os.path.isdir(adr_dir):
        for fn in sorted(os.listdir(adr_dir)):
            if not fn.endswith(".md"):
                continue
            try:
                with open(os.path.join(adr_dir, fn), encoding="utf-8", errors="replace") as f:
                    body = f.read()
                title = next((l for l in body.splitlines() if l.startswith("# ")), "")
                adrs.append((title or fn, body))
            except OSError:
                continue

    buf = io.StringIO()
    buf.write("# context card (%d target%s)\n" % (len(targets), "s" if len(targets) != 1 else ""))
    shown = 0
    for t in targets:
        if shown >= 40:
            buf.write("## ... (truncated at 40 targets)\n")
            break
        locs = index.get(t)
        buf.write("\n## %s\n" % t)
        if not locs:
            buf.write("  not found\n")
            continue
        loc = locs[0]
        mod = loc.get("module", "?")
        kind = loc.get("kind", "?")
        line = loc.get("line", 0)
        buf.write("  defined: %s:%s [%s]\n" % (mod, line, kind))
        sigs = [l.get("sig", "") for l in locs[:3] if l.get("sig")]
        for s in sigs:
            buf.write("  sig: %s\n" % s)
        n = callers.get(t, 0)
        if cg_built:
            buf.write("  callers: %d\n" % n)
        else:
            buf.write("  callers: not built (run --index/--graph for call context)\n")
        hits = [title for title, body in adrs if t in body]
        for h in hits[:3]:
            buf.write("  adr: %s\n" % h)
        shown += 1
    out = buf.getvalue()
    lines = out.splitlines()
    if len(lines) > 120:
        out = "\n".join(lines[:120]) + "\n(truncated to 120 lines)\n"
    return out

_FLOW_ANSWER_TERMS = ("lifecycle", "flow", "pipeline", "request path",
                      "how does", "call path", "data path", "process flow",
                      "end to end", "how is the", "how the request")

def _answer_is_flow_question(question: str) -> bool:
    """True when the question asks about a lifecycle/flow/process rather than
    naming a concrete symbol. Such questions must never be answered with a
    single token-overlap symbol guess."""
    ql = question.lower()
    return any(t in ql for t in _FLOW_ANSWER_TERMS)


def _answer_flow_fallback(files: List[str], root: str, question: str,
                          top: dict) -> str:
    """Honest no-confident-match answer for lifecycle/flow questions: never
    present the token-overlap guess as a real answer. Gives the task-style
    module ranking (--task parity) + next-step guidance instead."""
    buf = io.StringIO()
    buf.write("confidence: no confident match\n\n")
    buf.write("# answer: %s\n" % question)
    buf.write("\nNo confident match — this looks like a lifecycle/flow question.\n")
    buf.write("The best symbol hit (%s: %s:%s, score %.2f) is a token-overlap\n"
              % (top.get("name", "?"), top.get("module", "?"),
                 top.get("line", 0), float(top.get("score", 0) or 0)))
    buf.write("guess, not a confirmed answer.\n")
    buf.write("\nUse --task '<query>' for module ranking + --impact <candidate>\n")
    buf.write("for blast radius, or --pack for a task bundle.\n")
    try:
        task = render_task(files, root, question, top=3)
        rows = [ln for ln in task.splitlines()
                if ln.strip() and not ln.startswith("#")]
        if rows:
            buf.write("\n## task-ranked modules\n")
            for ln in rows:
                buf.write("%s\n" % ln)
    except Exception:
        pass
    return buf.getvalue()


def render_answer(files: List[str], root: str, question: str) -> str:
    """One-shot cited answer (repowise get_answer parity): exact match ->
    single hit with honest confidence + summary-first source + callers/callees.
    Confidence thresholds calibrated on the zero-dep scoring scale
    (~0.7-1.9 observed; exact class match ~1.8, weak partial ~0.7).

    Two semantic gates, in order:
      1. FLOW GATE — lifecycle/flow questions ("how does the request
         lifecycle work?") that only produce a token-overlap heuristic hit
         are NOT answered with a single symbol. They get the task-style
         module ranking + guidance instead (never a wrong confident symbol).
      2. CONFIDENCE GATE —  a heuristic/partial match is never labeled
         "high"; only a full name-token match (>= the 1.8 floor) can be.
    """
    results = hybrid_search(files, root, question, limit=3)
    if not results:
        return "confidence: low\n\nNo matching symbols."
    top = results[0]
    score = float(top.get("score", 0) or 0)
    # Heuristic gate is standard on the hybrid_search scoring scale
    # (lex = 2.0 per exact name-token match, * kind_b * (0.7 + size_b),
    # size factor >= 0.9): a single full match token floors at
    # 2.0 * 1.0 * 0.9 = 1.8 — any score below that cannot be a full match.
    # Additionally, every query token must appear in the top hit's name
    # (exact or substring); a token that contributes only via module/
    # substring of a DIFFERENT symbol means the top hit is a partial
    # heuristic match (e.g. "Request class" matching "RequestHandler":
    # the wrong match scored 3.40 while "class" never touches the name).
    top_name = str(top.get("name", "") or "")
    top_name_toks = _bm25(top_name)
    top_name_lc = top_name.lower()
    q_tokens = _bm25(question)
    full_match_floor = 1.8
    name_covers = all(
        t in top_name_toks or t in top_name_lc for t in q_tokens)
    heuristic_match = (score < full_match_floor) or not name_covers
    # Gate 1: flow/lifecycle questions with only a heuristic hit -> honest
    # no-confident-match + task-style module ranking, never a symbol guess.
    # Only when the top hit is a WEAK partial (sub-threshold or a token that
    # never touches the name) — a genuine multi-token full match keeps the
    # confident answer path even if the question also contains flow words.
    if heuristic_match and _answer_is_flow_question(question):
        exact_tokens = [t for t in q_tokens
                        if t in top_name_toks or t in top_name_lc]
        if len(exact_tokens) <= 1:
            return _answer_flow_fallback(files, root, question, top)
    # Gate 2: a heuristic/partial match is never "high" — only a full
    # name-token match (>= the 1.8 floor) qualifies as confident.
    if heuristic_match:
        conf = "low"
    else:
        conf = "high" if score >= 1.5 else ("medium" if score >= 0.9 else "low")
    buf = io.StringIO()
    buf.write("confidence: %s\n\n" % conf)
    buf.write("# answer: %s\n" % question)
    buf.write("\n## best match\n")
    buf.write("source: %s:%s [%s] (score %.2f)\n" % (
        top.get("module", "?"), top.get("line", 0), top.get("kind", "?"), score))
    snip = (top.get("snippet") or "").strip()
    if snip:
        buf.write("%s\n" % snip)
    # alternatives considered
    for alt in results[1:2]:
        buf.write("also: %s:%s [%s] (score %.2f)\n" % (
            alt.get("module", "?"), alt.get("line", 0), alt.get("kind", "?"),
            float(alt.get("score", 0) or 0)))
    # callers/callees from the call graph
    try:
        cg = build_call_graph_multi(files, root)
        mod = top.get("module")
        name = top.get("name")
        callees = set()
        callers = set()
        for m, funcs in cg.items():
            for fn, outs in funcs.items():
                if m == mod and fn == name:
                    callees |= set(outs)
                if name in outs:
                    callers.add("%s.%s" % (m, fn) if m != mod else fn)
        if callees:
            buf.write("callees: %s\n" % ", ".join(sorted(callees)[:6]))
        if callers:
            buf.write("callers: %s\n" % ", ".join(sorted(callers)[:6]))
    except Exception:
        pass
    if heuristic_match:
        buf.write("\n(heuristic match — verify against the source before trusting)\n")
    return buf.getvalue()

def render_why(files: List[str], root: str, query: str) -> str:
    """Decision lookup with evidence stamps (repowise get_why parity): every
    matching memory/ADR line is stamped [exact]/[fuzzy]/[unverified] so the
    agent knows how much to trust it. Falls back to nothing found."""
    q = query.strip()
    ql = q.lower()
    memdir = os.path.join(root, ".codeloom-memory")
    files_to_scan = []
    if os.path.isdir(memdir):
        for fn in sorted(os.listdir(memdir)):
            p = os.path.join(memdir, fn)
            if os.path.isfile(p) and fn.endswith(".md"):
                files_to_scan.append(p)
        adrd = os.path.join(memdir, "adr")
        if os.path.isdir(adrd):
            for fn in sorted(os.listdir(adrd)):
                p = os.path.join(adrd, fn)
                if os.path.isfile(p) and fn.endswith(".md"):
                    files_to_scan.append(p)
    buf = io.StringIO()
    buf.write("# why: %s\n" % q)
    found = False
    terms = [w for w in ql.split() if len(w) >= 4]
    for path in files_to_scan:
        rel = os.path.relpath(path, root)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            ll = line.lower()
            hit_kw = bool(terms) and all(w in ll for w in terms)
            hit_sub = ql in ll
            if not hit_kw and not hit_sub:
                continue
            found = True
            if hit_sub:
                stamp = "[exact]"
            elif _subword_similarity(q, line) >= 0.55:
                stamp = "[fuzzy]"
            else:
                stamp = "[unverified]"
            buf.write("  %s %s: %s\n" % (stamp, rel, line[:160]))
    if not found:
        buf.write("No recorded decisions/memory match.\n")
    return buf.getvalue()

def save_persistent_index(root: str, index: dict, files: List[str], kg: Optional[dict] = None,
                          skip_json: bool = False) -> None:
    """Save the persistent index with per-file (mtime, size) for incremental
    refresh. Size is tracked because Windows mtime has ~2s resolution, so a
    quick append may not change mtime — but it always changes size.
    `kg` is the optional knowledge-graph edges (call + import).
    JSON-only persistence (SECURITY): marshal is never written — .bin files
    are attacker-controlled and unsafe to load. `skip_json=True` (used by
    --engine c) still writes the JSON copy (JSON is now the ONLY format);
    it simply skips the duplicate binary blob entirely."""
    data = {
        "version": INDEX_VERSION,
        "root": root,
        "built_at": _now_iso(),
        "commit": _git_commit(root),
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
    # NOTE: no marshal .bin copy is written anymore (SECURITY: repo-supplied
    # marshal data is attacker-controlled; JSON is the only persisted format).

def _index_contain(root: str, data: Optional[dict]) -> Optional[dict]:
    """Containment: drop any persisted index entry whose path escapes the
    repo root. A repo-supplied index could otherwise smuggle absolute paths
    (or ../ escapes) that later reads open outside the repo."""
    if not data:
        return data
    root_abs = os.path.abspath(root)
    root_real = os.path.realpath(root)
    files = data.get("files")
    if isinstance(files, dict):
        kept = {}
        for f, meta in files.items():
            if os.path.isabs(f):
                # realpath: an in-repo symlink pointing outside used to pass
                # the abspath prefix check and then read outside the repo
                if not os.path.realpath(f).startswith(root_real + os.sep):
                    continue
            elif os.path.normpath(f).startswith(".."):
                continue
            kept[f] = meta
        data["files"] = kept
    symbols = data.get("symbols")
    if isinstance(symbols, dict):
        for name in list(symbols.keys()):
            locs = symbols[name]
            if not isinstance(locs, list):
                continue
            keep = []
            for loc in locs:
                if not isinstance(loc, dict):
                    continue
                p = loc.get("path")
                if p is None:
                    keep.append(loc)
                    continue
                if os.path.isabs(p):
                    if os.path.realpath(p).startswith(root_real + os.sep):
                        keep.append(loc)
                elif not os.path.normpath(p).startswith(".."):
                    keep.append(loc)
            symbols[name] = keep
    kg = data.get("kg")
    if isinstance(kg, dict):
        for key in ("calls", "imports"):
            edges = kg.get(key)
            if isinstance(edges, dict):
                for k in list(edges.keys()):
                    v = edges[k]
                    if isinstance(v, list):
                        edges[k] = [x for x in v
                                    if isinstance(x, str)
                                    and (not os.path.isabs(x)
                                         or os.path.abspath(x).startswith(root_abs + os.sep))]
    return data

def load_persistent_index(root: str) -> Optional[dict]:
    """Load the persistent index if present and valid. JSON only (SECURITY):
    if a legacy .bin marshal file exists it is ignored with a warning — the
    loader never unmarshals repo-supplied data. Index entries whose paths
    escape the repo root are dropped (containment)."""
    bin_path = _index_bin_path(root)
    if os.path.isfile(bin_path):
        print(f"codeloom: ignoring {bin_path} (marshal index is not trusted; "
              f"delete it and re-run `codeloom --index` to rebuild as JSON).",
              file=sys.stderr)
    try:
        with open(_index_path(root), "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == INDEX_VERSION:
            return _index_contain(root, data)
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
    # NOTE: no marshal .bin copy is written (SECURITY: JSON-only persistence;
    # the loader never unmarshals repo-supplied data).
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
    faster on 100k-file repos). engine='rust' uses the multi-threaded Rust core
    (codeloom_core_rs). Pure-Python ('py') is the default. If an engine core is
    requested but unavailable, this FAILS LOUDLY (no silent empty index)."""
    if engine in ("c", "rust"):
        # defensive: never index the engine's own sources/bins even if they
        # reached the file list through some other walker path
        files = [f for f in files
                 if os.path.basename(f.rstrip("/")) not in _ENGINE_SELF_FILES]
        if not _find_core_engine(engine):
            raise SystemExit(
                f"[error] --engine {engine} requested but no core binary is built "
                f"and it could not be auto-built.\n"
                f"  {_core_unavailable_help(engine)}\n"
                f"  Fix: install the compiler (or run --build-core) OR use --engine py.\n"
                f"  (Refusing to write an empty index.)")
        scan = _c_scan(files, engine=engine)  # scan each file ONCE, reuse for symbols + kg
        index = _c_symbol_index(files, root, scan=scan)
        all_defined = set(index.keys())
        kg = _c_kg(files, root, all_defined, scan=scan)
        if not index:
            raise SystemExit(
                f"[error] --engine {engine} produced no symbols "
                f"({len(files)} files scanned). Refusing to save an empty index.\n"
                f"  Fix: run --engine py (pure Python) or repair the {engine} core.")
    else:
        index = build_persistent_index(files, root, parallel=parallel)
        kg = build_knowledge_graph(files, root, parallel=parallel)
    save_persistent_index(root, index, files, kg=kg, skip_json=False)
    n_syms = sum(len(v) for v in index.values())
    n_edges = sum(len(v) for v in kg["calls"].values()) + sum(len(v) for v in kg["imports"].values())
    buf = io.StringIO()
    buf.write(f"# codeloom --index — built persistent index + knowledge graph ({engine} engine)\n")
    buf.write(f"  {len(files)} files, {n_syms} symbols, {n_edges} call/import edges\n")
    buf.write(f"  saved to {_index_path(root)}\n")
    buf.write(f"  subsequent --get-symbol/--search/--cross/--deadcode load it in milliseconds\n")
    return buf.getvalue()

def render_query(root: str, query: str) -> str:
    """Fast structural query against the persisted knowledge graph — no re-walk.
    Loads the saved index (symbols + call/import edges) and answers:
      callers X | callees X | dependents X | hubs | routes | symbol X
    This is the 'one graph query replaces many greps' primitive: sub-ms once the
    index exists, mirroring codebase-memory's fast structural queries."""
    import time
    t0 = time.time()
    pidx = load_persistent_index(root)
    if pidx is None:
        return ("No persistent index. Run `codeloom --index` first, then "
                "queries answer in milliseconds.\n")
    q = query.strip()
    ql = q.lower()
    buf = io.StringIO()
    buf.write(f"# codeloom --query \"{query}\"  ({time.time() - t0:.0f}ms load)\n")
    kg = pidx.get("kg", {})
    calls = kg.get("calls", {}) or {}
    imports = kg.get("imports", {}) or {}
    symbols = pidx.get("symbols", {}) or {}

    def _resolve_sym(s):
        if s in calls or s in symbols:
            return s
        # suffix-match module names
        for name in list(calls) + list(symbols):
            if name.endswith(s) or s in name.split(".")[-1]:
                return name
        return s

    # query types
    if ql.startswith("callers "):
        sym = _resolve_sym(q[8:].strip())
        # kg calls = {module: {caller_func: set(callee_names)}} — a "caller" of
        # sym is any function (in any module) whose target set contains sym.
        # The old code tested `sym in cs` against the module's function-KEYS,
        # which only ever matched the module defining sym — a pre-existing bug
        # (both engines) exposed on HA-core: 20 usages, 1 bogus "caller".
        # Legacy flat shape {module: [callees]} is tolerated too (old indexes).
        hits = []
        for m, funcs in calls.items():
            if isinstance(funcs, dict):
                for caller, targets in funcs.items():
                    if sym in targets:
                        hits.append(f"{m}.{caller}" if caller != "<module>" else m)
            elif sym in funcs:
                hits.append(m)
        buf.write(f"## Callers of {sym}\n")
        for m in sorted(hits):
            buf.write(f"  {m}\n")
        if not hits:
            buf.write("  (none)\n")
        buf.write(f"\n  {len(hits)} caller(s) — 1 query vs N file scans.\n")
        return buf.getvalue()
    if ql.startswith("callees"):
        sym = _resolve_sym(q[7:].strip())
        callees = set()
        if sym in calls:
            funcs = calls[sym]
            if isinstance(funcs, dict):
                # module name: every callee of every function in the module
                for targets in funcs.values():
                    callees |= set(targets)
            else:
                # legacy flat shape: {module: [callee names]}
                callees |= set(funcs)
        else:
            # function name: the callees of that function wherever defined
            for funcs in calls.values():
                if isinstance(funcs, dict) and sym in funcs:
                    callees |= set(funcs[sym])
        buf.write(f"## Callees of {sym}\n")
        for c in sorted(callees):
            buf.write(f"  {c}\n")
        buf.write(f"\n  {len(callees)} callee(s).\n")
        return buf.getvalue()
    if ql.startswith("dependents") or ql.startswith("depends on"):
        rest = q.split(" ", 1)
        if len(rest) < 2 or not rest[1].strip():
            buf.write("Usage: --query 'dependents <module>'\n")
            buf.write("  (module name required — bare 'dependents' has no target)\n")
            return buf.getvalue()
        sym = _resolve_sym(rest[1].strip())
        hits = [m for m, deps in imports.items() if sym in deps]
        buf.write(f"## Dependents of {sym}\n")
        for m in sorted(hits):
            buf.write(f"  {m}\n")
        if not hits:
            buf.write("  (none)\n")
        buf.write(f"\n  {len(hits)} dependent(s).\n")
        return buf.getvalue()
    if ql.startswith("hubs"):
        # most-depended-on modules (change these -> breaks many)
        from collections import Counter
        dep_count = Counter()
        for deps in imports.values():
            for d in deps:
                dep_count[d] += 1
        buf.write("## Hub modules (most-depended-on — change these -> breaks many)\n")
        for m, n in dep_count.most_common(10):
            buf.write(f"  {m} ({n} importers)\n")
        return buf.getvalue()
    if ql.startswith("routes"):
        return render_routes(root)
    if ql.startswith("symbol "):
        sym = q[7:].strip()
        locs = symbols.get(sym)
        buf.write(f"## Symbol {sym}\n")
        if locs:
            for l in locs[:10]:
                buf.write(f"  {l.get('module', '?')}:{l.get('line', '?')}\n")
        else:
            buf.write("  not found in index\n")
        return buf.getvalue()
    # default: fuzzy — show matching callers/callees for any symbol in the query
    words = [w for w in re.findall(r"[A-Za-z_][\w.]*", q) if w.lower() not in
             {"callers", "callees", "dependents", "depends", "on", "hubs",
              "routes", "symbol", "of", "the", "who", "calls", "what", "uses"}]
    if words:
        sym = _resolve_sym(words[-1])
        if sym:
            return render_query(root, f"callers {sym}")
    return ("Query not recognized. Try: 'callers X', 'callees X', "
            "'dependents X', 'hubs', 'routes', 'symbol X'.\n")

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

# --------------------------------------------------------------------------- #
# Structural-first --task ranking (entry points + call-graph centrality beat
# keyword hits; keywords are a capped tiebreaker, never the primary signal).
# Deterministic: pure AST/regex over the module graph, stable sort, no LLM.
# --------------------------------------------------------------------------- #

_ENTRY_BASENAMES = {
    "app", "main", "server", "api", "index", "cli", "router",
    "wsgi", "asgi", "views", "handler", "controller", "service",
}

def _package_exported_names(files: List[str], root: str) -> set:
    """Collect names re-exported by package __init__ files
    (`from .app import Flask as Flask`, `from . import json as json`).

    A module that DEFINES one of these names is the package's entry point —
    e.g. class Flask lives in flask/app.py, so src.flask.app is the module
    agents should start from for app-lifecycle tasks."""
    exported: set = set()
    for f in files:
        if os.path.basename(f) != "__init__.py":
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext not in (".py", ".pyi", ".pyw"):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        clean = _strip_strings_comments(text, ext)
        for m in re.finditer(r"^\s*from\s+[\w.]+\s+import\s+(.+)$", clean, re.M):
            for name in m.group(1).split(","):
                name = name.strip()
                if name.startswith("("):
                    name = name.lstrip("(").strip()
                alias = name.split(" as ")[-1].strip() if " as " in name else name
                if re.match(r"^[A-Za-z_]\w*$", alias):
                    exported.add(alias)
        for m in re.finditer(r"^\s*from\s+\.\s+import\s+(.+)$", clean, re.M):
            for name in m.group(1).split(","):
                alias = name.strip().split(" as ")[-1].strip()
                if re.match(r"^[A-Za-z_]\w*$", alias):
                    exported.add(alias)
    return exported

def _entry_point_score(path: str, root: str, exported: set) -> int:
    """Structural entry-point signal for a module (0..8, deterministic):
    +4 defines a class the package re-exports (the entry class),
    +3 defines a class named after the module itself,
    +3 route/URL decorators (HTTP entry points),
    +2 create_app/factory or main entry function,
    +2 __main__ guard,
    +2 conventional entry basename (app/main/cli/...)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".py", ".pyi", ".pyw"):
        return 0
    mod = module_name_of(path, root)
    base = mod.rsplit(".", 1)[-1] if "." in mod else mod
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return 0
    clean = _strip_strings_comments(text, ext)
    s = 0
    for m in re.finditer(r"^class\s+(\w+)", clean, re.M):
        if m.group(1) in exported:
            s += 4  # package entry class (class Flask in flask/app.py)
            break
    if re.search(r"^class\s+" + re.escape(base) + r"\b", clean, re.M):
        s += 3  # class named after the module (its own entry point)
    if re.search(r"@[\w.]*\.(?:route|get|post|put|delete|patch|use)\s*\(", clean):
        s += 3  # route decorators — URL entry points
    if re.search(r"^def\s+(?:create_app|init_app|main|run|serve)\s*\(", clean, re.M):
        s += 2  # factory / entry function
    if re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", clean):
        s += 2  # runnable entry script
    if base in _ENTRY_BASENAMES:
        s += 2
    return min(s, 8)

def task_structural_rank(files: List[str], root: str, task: str, top: int = 10) -> List[dict]:
    """Structural-first ranking for --task.

    Score = (entry_point + centrality) * 10 + capped keyword delta.
      - entry_point: does this module define the entry class / routes / a
        factory or main? (the code agents must touch for app-level tasks)
      - centrality: direct import in-degree + cross-module call edges
        (hubs the task's flow actually passes through)
      - keyword hits: capped, tie-break only — docstring word-count matches
        (e.g. json.provider's 'response' docstring) can NEVER outrank
        structural relevance.
    Deterministic: same repo + same query -> same order, every run."""
    task_tokens = _tokenize(task)
    if not task_tokens:
        return []
    exported = _package_exported_names(files, root)
    # import graph (multi-language): direct in-degree = who imports this module
    graph = build_graph_multi(files, root)
    indeg: dict = {}
    for mod, deps in graph.items():
        for d in deps:
            indeg[d] = indeg.get(d, 0) + 1
    # call graph: cross-module call edges (callers of the module's functions + 
    # modules the module's functions call into)
    calls = build_cross_call_graph(files, root)
    call_out: dict = {}
    for mod, funcs in calls.items():
        for caller, callees in funcs.items():
            for callee in callees:
                cm = callee.rsplit(".", 1)[0]
                if cm and cm != mod:
                    call_out.setdefault(mod, set()).add(cm)
    call_in: dict = {}
    for mod, targets in call_out.items():
        for t in targets:
            call_in[t] = call_in.get(t, 0) + 1

    scored = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in LANG_RULES and ext not in IMPORT_LANG_RULES:
            continue
        mod = module_name_of(f, root)
        toks = _module_tokens(f)
        overlap = len(task_tokens & toks)
        if overlap == 0:
            continue
        entry = _entry_point_score(f, root, exported)
        hub = min(len(call_out.get(mod, set())) + call_in.get(mod, 0), 8)
        centrality = min(indeg.get(mod, 0), 10) + hub
        centrality = min(centrality, 12)
        # keyword hits: capped tiny delta — structural signals dominate
        mod_tokens = _tokenize(mod)
        name_bonus = min(len(task_tokens & mod_tokens), 3)
        score = (centrality + entry) * 10 + min(overlap, 2) * 2 + name_bonus
        scored.append({"module": mod, "path": f, "score": score,
                       "overlap": overlap, "centrality": centrality,
                       "entry": entry})
    scored.sort(key=lambda s: (-s["score"], s["module"]))
    return scored[:top]

def render_task(files: List[str], root: str, task: str, top: int = 10) -> str:
    results = task_structural_rank(files, root, task, top)
    buf = io.StringIO()
    buf.write(f"# task: {task}\n")
    if not results:
        buf.write("No modules matched the task. Try different keywords.\n")
        return buf.getvalue()
    buf.write(f"Top {len(results)} relevant modules (structural relevance — entry points + call-graph centrality, keyword tie-break):\n\n")
    for i, r in enumerate(results, 1):
        buf.write(f"{i}. {r['module']}  (score {r['score']}, {r['overlap']} keyword hits, "
                  f"entry {r['entry']} + centrality {r['centrality']})\n")
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

    # 2. THE RELEVANT CODE — byte-precise, embedded, capped.
    # Symbol selection follows the task, not file order: functions whose
    # bodies hit the task's anchor tokens first, then call-graph centrality
    # (hub score = inbound callers + outbound callees). That makes the
    # module's entry/dispatch chain — the functions reachable from route
    # registration -> request dispatch — what gets embedded, instead of
    # docstring-matched helpers like `_make_timedelta`. Deterministic:
    # hits desc, hub desc, name length asc, then line order.
    buf.write("## 2. THE RELEVANT CODE (byte-precise, embedded)\n")
    calls = build_call_graph_multi(files, root)
    hit_tokens = _anchor_symbols(task) or _tokenize(task)
    # reverse index: callee -> set of callers (intra- + inter-module), used
    # for the hub score. Sets only contribute their size — deterministic.
    callers_of: dict = {}
    all_defined: set = set()
    for cm, funcs in calls.items():
        all_defined |= set(funcs)
        for caller, callees in funcs.items():
            for callee in callees:
                callers_of.setdefault(callee, set()).add(caller)
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
        # local defined names: the global graph (tree-sitter) drops methods
        # whose calls are all attribute calls (`self.x()`), so re-add this
        # module's own def-regex names for the call-site scan below.
        local_defined = set(all_defined)
        for dm in re.finditer(def_re, text, re.MULTILINE):
            dn = next((g for g in dm.groups() if g), None)
            if dn:
                local_defined.add(dn)
        # call sites per defined name in this module (attribute calls like
        # `self.dispatch_request(...)` included; def lines excluded) — so the
        # dispatch chain hubs up even though the global graph ignores methods
        call_sites: dict = {}
        strip = _strip_strings_comments(text, ext)
        for line, clean_line in zip(text.splitlines(), strip.splitlines()):
            if re.match(def_re, clean_line):
                continue  # definition line, not a call site
            for cm in re.finditer(r"(?:\w+\.)?(\w+)\s*\(", clean_line):
                c = cm.group(1)
                if c in local_defined:
                    call_sites[c] = call_sites.get(c, 0) + 1
        # score each symbol: (hit, hub, name_len, line) -> embed top 12
        mod_calls = calls.get(mod, {})
        symbols = []
        for m in re.finditer(def_re, text, re.MULTILINE):
            name = next((g for g in m.groups() if g), None)
            if not name:
                continue
            line = text[:m.start()].count("\n") + 1
            # extract the symbol body (up to ~40 lines)
            lines = text[m.start():].splitlines()
            body = "\n".join(lines[:40])
            hit = 1 if (hit_tokens & _tokenize(body)) else 0
            hub = (len(callers_of.get(name, ()))
                   + min(call_sites.get(name, 0), 20)
                   + len(mod_calls.get(name, ())))
            symbols.append((hit, hub, len(name), line, name, body, lines))
        # task-relevant first: keyword hits, then hubs (dispatch chain),
        # then shorter names, then file order. Deterministic across runs.
        symbols.sort(key=lambda s: (-s[0], -s[1], s[2], s[3]))
        for hit, hub, _name_len, line, name, body, lines in symbols[:12]:
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
# --cognitive-load: cognitive-load-theory-aware task decomposition.
# Splits a topic into working-memory-sized steps, labels element interactivity,
# and suggests the minimal high-signal context (hot set + decisions + relevant
# memory). This is the "managing the agent's cognitive load" primitive — it
# optimizes for low extraneous load (no noise), sequenced intrinsic load
# (easy -> hard), and high germane load (mental model = decisions + hot set).
# --------------------------------------------------------------------------- #
def render_cognitive_load(files: List[str], root: str, topic: str) -> str:
    results = edit_relevance(files, root, topic, top=6)
    buf = io.StringIO()
    buf.write(f"# codeloom --cognitive-load \"{topic}\"\n")
    buf.write("# Decomposed for working-memory-sized steps, low extraneous load.\n\n")

    # Intrinsic load: the essential complexity, sequenced easiest->hardest
    buf.write("## Intrinsic load (the inherent complexity, sequenced)\n")
    if results:
        # order: fewer deps first (easiest to hardest by anchor distance)
        ranked = sorted(results, key=lambda r: r.get("anchor_dist", 99))
        for i, r in enumerate(ranked, 1):
            buf.write(f"  {i}. {r['module']}  (anchor distance {r.get('anchor_dist','?')}, "
                      f"{r.get('overlap',0)} keyword hits)\n")
    else:
        buf.write("  No modules matched. Refine the topic.\n")
    buf.write("\n")

    # Extraneous load: what to AVOID (the noise) — full-file dumps, re-reads
    hot = set(get_hot_set(root))
    buf.write("## Extraneous load (skip this noise — it's already handled)\n")
    if hot:
        buf.write("  Already-deeply-understood (hot set) — do NOT re-read:\n")
        for h in list(sorted(hot))[:8]:
            buf.write(f"    - {h}\n")
    else:
        buf.write("  Nothing marked as understood yet. Use `--mark-seen` as you read.\n")
    buf.write("  Prefer summary-first retrieval; full source is high extraneous load.\n\n")

    # Germane load: build the mental model — decisions + open + lessons
    buf.write("## Germane load (build the mental model — decisions, lessons, open items)\n")
    mem = memory_query(root, topic)
    body = mem.strip()
    if "No long-term memory" not in body:
        for line in body.splitlines()[1:]:
            if line.strip():
                buf.write(f"  {line.strip()}\n")
    else:
        buf.write("  (no recorded memory on this topic yet — record decisions as you go)\n")
    opens = [e for e in journal_read(root) if e.get("type") in ("hypothesis", "open")]
    if opens:
        buf.write("  Open items/hypotheses:\n")
        for o in opens[:5]:
            buf.write(f"    - {o.get('title')} [{o.get('status','open')}]\n")
    buf.write("\n")

    buf.write("## Recommended minimal context (one call)\n")
    buf.write("  codeloom --pack \"" + topic + "\" .   # full code-embedded brief\n")
    buf.write("  codeloom --working-state .            # if post-compaction, restore first\n")
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
        # growth bound: rotate oldest entries if the file exceeds its cap
        memory_rotate(root, section + ".md")
        return f"remembered: {section} <- {note}"
    except OSError as e:
        return f"memory write failed: {e}"

# --------------------------------------------------------------------------- #
# Rich long-term memory: lessons/traps, supersession, query-memory.
# --lesson "we tried X, failed because Y" kills re-exploring dead ends.
# --supersede OLD NEW marks "decision OLD is replaced by NEW".
# --query-memory "auth" pulls decisions + lessons + conventions + ADRs.
# Stored in .codeloom-memory/LESSONS.md / SUPERSEDED.md (git-friendly).
# --------------------------------------------------------------------------- #
def memory_lesson(root: str, lesson: str) -> str:
    """Record a lesson/trap: something tried and why it failed, so a wiped
    agent never re-explores the same dead end."""
    d = _memory_dir(root)
    p = os.path.join(d, "LESSONS.md")
    try:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"- {lesson}\n")
        memory_rotate(root, "LESSONS.md")
        return f"recorded lesson: {lesson}"
    except OSError as e:
        return f"lesson write failed: {e}"

def memory_supersede(root: str, old: str, new: str) -> str:
    """Record that decision/hypothesis `old` is superseded by `new`."""
    d = _memory_dir(root)
    p = os.path.join(d, "SUPERSEDED.md")
    try:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"- {old} → superseded by {new}\n")
        memory_rotate(root, "SUPERSEDED.md")
        return f"superseded: {old} -> {new}"
    except OSError as e:
        return f"supersede write failed: {e}"

def memory_query(root: str, query: str) -> str:
    """Search long-term memory (DECISIONS/PATTERNS/CONVENTIONS/LESSONS/SUPERSEDED/
    ADRs + typed memory.jsonl entries) for items relevant to `query`. Full-text
    term match over the plain-text memory AND the typed JSONL layer (type +
    title + body keywords), typed hits ranked by priority desc — the 'what do
    we already know about X' primitive."""
    import re as _re
    q = _re.escape(query.lower())
    d = _memory_dir(root)
    buf = io.StringIO()
    buf.write(f"# codeloom --query-memory \"{query}\"\n")
    hits = 0
    files = [n for n in os.listdir(d) if n.endswith(".md")]
    adr_d = os.path.join(d, "adr")
    if os.path.isdir(adr_d):
        files += ["adr/" + n for n in os.listdir(adr_d) if n.endswith(".md")]
    for rel in sorted(files):
        p = os.path.join(d, rel)
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            if _re.search(q, line.lower()):
                buf.write(f"  [{rel}] {line.strip()}\n")
                hits += 1
    # typed JSONL layer: match type/title/body keywords, ranked by priority desc
    typed = [e for e in _memory_entries(root)
             if _re.search(q, ((e.get("type") or "") + " "
                               + (e.get("title") or "") + " "
                               + (e.get("body") or "")).lower())]
    typed.sort(key=_memory_sort_key)
    for e in typed:
        buf.write("  [memory.jsonl] %s\n" % _memory_line(e))
        hits += 1
    if not hits:
        buf.write("  No long-term memory matches. Record some with --remember/--lesson/--decide.\n")
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --adr: Architectural Decision Records (structured, cross-session)
# --------------------------------------------------------------------------- #
# Matches codebase-memory's manage_adr: save human architectural constraints
# (context + decision + status) so they survive alongside the raw codebase
# graph. Stored as numbered ADR files in .codeloom-memory/adr/.
ADR_DIR = "adr"

def _adr_dir(root: str) -> str:
    d = os.path.join(_memory_dir(root), ADR_DIR)
    if not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return d

def render_adr(root: str, title: str, context: str, decision: str,
               status: str = "Accepted") -> str:
    """Write a structured Architectural Decision Record. Returns the ADR text.
    Survives compaction because it's a file on disk, and it captures the human
    'why' that a raw code graph can't."""
    d = _adr_dir(root)
    # next ADR number
    n = 1
    for fn in os.listdir(d):
        if fn.startswith("ADR-") and fn.endswith(".md"):
            try:
                n = max(n, int(fn.split("-")[1].split("_")[0]) + 1)
            except (ValueError, IndexError):
                pass
    safe = re.sub(r"[^A-Za-z0-9]+", "-", title.strip().lower()).strip("-")[:40] or "decision"
    path = os.path.join(d, f"ADR-{n:03d}_{safe}.md")
    body = (f"# ADR-{n:03d}: {title}\n\n"
            f"**Status:** {status}\n\n"
            f"## Context\n{context.strip()}\n\n"
            f"## Decision\n{decision.strip()}\n")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return f"ADR-{n:03d} written: {path}\n\n{body}"
    except OSError as e:
        return f"ADR write failed: {e}\n"

def render_adr_list(root: str) -> str:
    """--adr-list: list all saved Architectural Decision Records."""
    d = _adr_dir(root)
    buf = io.StringIO()
    buf.write("# codeloom --adr-list\n")
    adrs = sorted(f for f in os.listdir(d) if f.startswith("ADR-") and f.endswith(".md"))
    if not adrs:
        buf.write("  No ADRs yet. Run `codeloom --adr \"<title>\" --context \"...\" --decision \"...\"`.\n")
        return buf.getvalue()
    for fn in adrs:
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                first = fh.readline().strip()
            buf.write(f"  {fn}  ({first})\n")
        except OSError:
            continue
    return buf.getvalue()

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
        graph = build_graph_multi(files, root, parallel=True)
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

def git_changed_files(root: str) -> Optional[List[str]]:
    """Return paths of files changed vs HEAD (tracked + untracked), root-relative.
    Returns None when the root is not a git repo (or git is unavailable) so
    callers can say so honestly instead of reporting a clean tree."""
    changed: List[str] = []
    try:
        import subprocess
        # tracked changes (staged + unstaged) vs HEAD
        r = subprocess.run(
            ["git", "-C", root, "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            # not a git repo / no HEAD — distinguish from a clean tree
            return None
        changed += [l for l in r.stdout.splitlines() if l.strip()]
        # untracked files
        r2 = subprocess.run(
            ["git", "-C", root, "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10,
        )
        if r2.returncode == 0:
            changed += [l for l in r2.stdout.splitlines() if l.strip()]
    except Exception:
        return None
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
    if changed is None:
        return "# codeloom --diff\nNot a git repository (or git unavailable) — nothing to diff against.\n"
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
    ".c":    (r"^\s*(?:static\s+)?(?!(?:if|while|for|switch|return|sizeof|else|do|case)\b)[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".h":    (r"^\s*(?:static\s+)?(?!(?:if|while|for|switch|return|sizeof|else|do|case)\b)[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".cpp":  (r"^\s*(?:static\s+)?(?!(?:if|while|for|switch|return|sizeof|else|do|case|catch|new|delete)\b)[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
    ".hpp":  (r"^\s*(?:static\s+)?(?!(?:if|while|for|switch|return|sizeof|else|do|case|catch|new|delete)\b)[\w\*]+\s+(\w+)\s*\(", r"\b(\w+)\s*\("),
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
        if ext == ".py":
            # classes are definitions too: `class Registry:` is instantiable
            # (Registry()) — without it, all_defined lacks the name, the
            # call filter drops the instantiation, and dead_code flags the
            # class while `REGISTRY = Registry()` sits next to it
            # (repowise 2026-08-23 finding).
            for cm in re.finditer(r"^\s*class\s+(\w+)", text, re.MULTILINE):
                defined[mod].add(cm.group(1))

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
            else:
                # module-level calls (e.g. `REGISTRY = Registry()`, `x = Foo()`)
                # — these keep classes/lambdas alive. Without this bucket,
                # instantiated classes are flagged dead (repowise 2026-08-23:
                # Registry flagged dead while REGISTRY = Registry() sat 3 lines
                # below). Track under a synthetic "<module>" caller so
                # dead_code's called-set sees them.
                for cm in re.finditer(r"\b(\w+)\s*\(", clean_line):
                    callee = cm.group(1)
                    if callee in all_defined:
                        calls[mod].setdefault("<module>", set()).add(callee)
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

# The PR-review CI workflow that --install-agents writes (set-and-forget).
# Modeled on the repo's own .github/workflows/pr-bot.yml (the retired
# codeloom-map.yml workflow has been removed): posts a deterministic
# codeloom review summary + inline findings on every PR, and hands off to
# Codex for the LLM pass.
CI_WORKFLOW = """name: codeloom-pr-bot

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # full history so merge-base with the base branch exists

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Resolve merge base
        id: base
        run: |
          MB=$(git merge-base "origin/${{ github.base_ref }}" HEAD 2>/dev/null || echo HEAD~1)
          echo "revspec=${MB}..HEAD" >> "$GITHUB_OUTPUT"

      - name: Assemble codeloom review (deterministic)
        run: |
          python codeloom.py --context-diff "${{ github.event.pull_request.base.sha }}" HEAD \
            > /tmp/codeloom-review.md 2>&1 || true
          python codeloom.py --risk "${{ steps.base.outputs.revspec }}" \
            >> /tmp/codeloom-review.md 2>&1 || true

      - name: Post / update sticky summary comment
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const MARKER = '<!-- codeloom-pr-bot -->';
            let body = fs.readFileSync('/tmp/codeloom-review.md', 'utf8');
            const cap = 60000;
            if (body.length > cap) body = body.slice(0, cap) + '\\n… (truncated)';
            const { data: comments } = await github.rest.issues.listComments({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              per_page: 100,
            });
            const mine = comments.find(c => c.body && c.body.includes(MARKER));
            if (mine) {
              await github.rest.issues.updateComment({
                owner: context.repo.owner, repo: context.repo.repo,
                comment_id: mine.id, body,
              });
              console.log(`updated existing summary ${mine.id}`);
            } else {
              await github.rest.issues.createComment({
                owner: context.repo.owner, repo: context.repo.repo,
                issue_number: context.issue.number, body,
              });
              console.log('created new summary comment');
            }

      - name: Hand off to Codex for LLM review pass
        uses: actions/github-script@v7
        with:
          script: |
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: `🪄 codeloom deterministic pass complete.\\n\\n`
                + `@codex review — please run your LLM review now. The deterministic `
                + `codeloom context (architecture diff + risk band) is posted above; `
                + `focus on semantics, logic, and design.`,
            });
            console.log('handed off to codex');
"""

def install_agents(root: str) -> str:
    """Write or update AGENTS.md with a codeloom instruction block, and write
    the codeloom PR-review CI workflow (set-and-forget onboarding)."""
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
    # write the codeloom PR-review CI workflow (set-and-forget)
    ci_path = os.path.join(root, ".github", "workflows", "codeloom-pr-bot.yml")
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
    # JSON-style inline "codeloom": { "command": python3, "args": [...] }
    inline = f'"codeloom": {{\n  "command": "python3",\n  "args": ["{server_py}"]\n}}'
    # mcpServers-wrapped JSON block
    mcp_block = f'{{\n  "mcpServers": {{\n    "codeloom": {{\n      "command": "python3",\n      "args": ["{server_py}"]\n    }}\n  }}\n}}'
    # bare {"codeloom": {...}} JSON block (opencode style)
    bare_block = f'{{\n  "codeloom": {{\n    "command": "python3",\n    "args": ["{server_py}"]\n  }}\n}}'
    # TOML [mcp_servers.codeloom] block (hermes, openhands, devin)
    toml_block = f'[mcp_servers.codeloom]\ncommand = "python3"\nargs = ["{server_py}"]'
    if a in ("claude", "claude-code", "claudecode"):
        # Claude Code: ~/.claude.json / .mcp.json — mcpServers-wrapped
        return mcp_block
    if a in ("cursor",):
        return mcp_block
    if a == "codex":
        return mcp_block
    if a in ("gemini", "gemini-cli"):
        return mcp_block
    if a == "opencode":
        return bare_block
    if a == "cline":
        # Cline: .cline/mcp_settings.json — mcpServers object
        return mcp_block
    if a in ("openhands", "openhands-cli"):
        # OpenHands: ~/.config/openhands/config.toml — [mcp_servers.codeloom]
        return toml_block
    if a == "devin":
        # Devin: ~/.devin/config.toml — [mcp_servers.codeloom]
        return toml_block
    if a in ("hermes", "hermes-agent"):
        # Hermes Agent: mcp_servers.json / config — [mcp_servers.codeloom]
        return toml_block
    if a == "aider":
        # Aider: .mcp.json — mcpServers object
        return mcp_block
    if a in ("roo", "roo-code"):
        # Roo Code: ~/.roo/mcp.json — mcpServers object
        return mcp_block
    if a == "windsurf":
        # Windsurf: ~/.codeium/windsurf/mcp.json — mcpServers object
        return mcp_block
    if a in ("amazon-q", "amazonq", "amazon", "q-developer"):
        # Amazon Q Developer: ~/.aws/amazonq/mcp.json — mcpServers object
        return mcp_block
    if a in ("jetbrains", "intellij", "goland", "pycharm", "webstorm"):
        # JetBrains IDEs: ~/.config/JetBrains/<product>/mcp.json — mcpServers
        return mcp_block
    if a in ("junie",):
        # Junie (JetBrains' coding agent): mcpServers JSON
        return mcp_block
    if a in ("kimi", "kimi-cli"):
        # Kimi CLI: ~/.kimi/ — mcpServers object
        return mcp_block
    if a in ("qwen", "qwen-code"):
        # Qwen Code: ~/.qwen/ — mcpServers object
        return mcp_block
    return inline

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
        ("cline", os.path.join(home, ".cline")),
        ("openhands", os.path.join(home, ".config", "openhands")),
        ("devin", os.path.join(home, ".devin")),
        ("hermes", os.path.join(home, ".hermes")),
        ("aider", os.path.join(home, ".aider")),
        ("roo", os.path.join(home, ".roo")),
        ("windsurf", os.path.join(home, ".codeium")),
        ("amazon-q", os.path.join(home, ".aws", "amazonq")),
        ("jetbrains", os.path.join(home, ".config", "JetBrains")),
        ("junie", os.path.join(home, ".junie")),
        ("kimi", os.path.join(home, ".kimi")),
        ("qwen", os.path.join(home, ".qwen")),
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

def log_session(root: str, command: str, text: str, elapsed: float = 0.0) -> None:
    """Append one invocation to the local session log (JSONL).
    est_tokens_out = bytes/4 of emitted text (honest estimate);
    est_tokens_in = the grep+read baseline input the command replaced (4x
    the output estimate — reading whole files instead of the compressed map).
    Both are LOCAL estimates; nothing leaves the machine.
    `elapsed` is the REAL wall time of the invocation (measured by the
    --session wrapper) — the old hardcoded 0.0 made the savings ledger
    fiction."""
    import json as _json
    import time as _time
    est_out = max(1, len(text) // 4)
    entry = {
        "ts": _time.time(),
        "root": os.path.abspath(root),
        "cmd": command,
        "tokens": estimate_tokens(text),
        "bytes": len(text),
        "est_tokens_out": est_out,
        "est_tokens_in": est_out * 4,
        "seconds": round(elapsed, 3),
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
    buf.write(f"\n{render_memory_line(root)}\n")
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

# --------------------------------------------------------------------------- #
# --verify-edit: post-edit graph-integrity oracle
# Reads the git working-tree diff, re-parses changed files, and compares
# against the pre-edit import graph (HEAD). Verdicts:
#   GO    — clean, no provably-dangling pre-edit statically-resolvable edge,
#           no new import cycle.
#   STOP  — a pre-edit statically-resolvable import edge now dangles (the
#           module it resolved to is gone or the import no longer resolves),
#           or a NEW import cycle appeared. --severity strict elevates STOP
#           to exit code 1.
#   CHECK — dynamic/lazy/vendored import suspects (importlib.import_module,
#           __import__, sys.modules, import inside function, vendored dirs,
#           star imports). Mentioned, never fatal.
# Driver style mirrors --risk's named-driver lines.
# --------------------------------------------------------------------------- #
VERIFY_EDIT_DRIVERS = {
    "dangling_edge": "import edge resolved to a module that no longer resolves after the edit",
    "new_cycle": "the edit introduces a new import cycle",
    "dynamic_import": "dynamic import (importlib/__import__/lazy) — static resolution cannot prove it",
    "vendored_import": "vendored/third-party import — outside the repo graph",
    "star_import": "star import (from x import *) — edge set is not statically closed",
    "syntax_error": "changed file does not parse — cannot verify statically",
}
IMPORT_SUSPECT_RE = re.compile(
    r"importlib\s*\.\s*import_module|__import__\s*\(|sys\s*\.\s*modules|"
    r"pkgutil\s*\.|find_spec\s*\(|import_module\s*\(")

def _git_quiet(root: str, argv: List[str]) -> Optional[str]:
    """Run a git command in root, return stdout or None on any failure."""
    try:
        import subprocess as _sp
        r = _sp.run(["git"] + argv, cwd=root, capture_output=True,
                    text=True, timeout=30)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None

def _git_changed_files(root: str) -> Tuple[Optional[List[str]], str]:
    """Working-tree changed files (tracked, incl. staged) relative to root.

    Returns (None, error) when git itself fails (not a repo, unreadable) so
    callers can distinguish 'clean tree' from 'cannot tell' — the old
    silent-[] shape made a non-git dir look like a clean GO."""
    out = _git_quiet(root, ["status", "--porcelain"])
    if out is None:
        return None, "not a git repository (git status failed)"
    changed = []
    for ln in out.splitlines():
        if len(ln) < 4:
            continue
        status, path = ln[:2], ln[3:]
        if status.strip() in ("D", "R"):
            continue  # deletions/renames have no new content to re-parse
        # quoted paths (spaces) -> strip git's C-style quoting
        if path.startswith('"') and path.endswith('"'):
            try:
                path = path[1:-1].encode().decode("unicode_escape")
            except Exception:
                continue
        changed.append(path)
    return changed, ""


class _HeadReader:
    """Batch reader for file contents at HEAD.

    One `git cat-file --batch` session serves every blob, instead of one
    `git show` subprocess per file (which costs ~13ms of process spawn each —
    ~90s on a 7k-file repo). Reads are lazy and buffered per file: a caller
    that only needs a few files pays for exactly those."""

    def __init__(self, root: str):
        import subprocess as _sp
        self._sp = _sp
        self._ls: Optional[str] = None
        self._ls_failed: bool = False
        self._buf: dict = {}
        self._proc: Optional[_sp.Popen] = None
        self._root = root

    @property
    def ls(self) -> Optional[str]:
        """`git ls-tree -r --name-only HEAD` output (None on failure)."""
        if self._ls is None and not self._ls_failed:
            self._ls = _git_quiet(self._root, ["ls-tree", "-r", "--name-only", "HEAD"])
            self._ls_failed = self._ls is None
        return self._ls

    def _get_proc(self):
        if self._proc is None:
            self._proc = self._sp.Popen(
                ["git", "cat-file", "--batch"],
                cwd=self._root, stdin=self._sp.PIPE,
                stdout=self._sp.PIPE, stderr=self._sp.DEVNULL)
        return self._proc

    def _fetch(self, rel: str) -> Optional[str]:
        """One buffered blob read (None when HEAD:rel does not exist)."""
        if rel in self._buf:
            return self._buf[rel]
        try:
            p = self._get_proc()
            assert p.stdin is not None and p.stdout is not None
            p.stdin.write(("HEAD:%s\n" % rel).encode())
            p.stdin.flush()
            hdr = p.stdout.readline().split()
            if len(hdr) != 3:
                self._buf[rel] = None
                return None
            size = int(hdr[2])
            data = p.stdout.read(size)
            p.stdout.read(1)  # trailing newline
            out = data.decode("utf-8", errors="replace")
        except Exception:
            out = None
        self._buf[rel] = out
        return out

    def text(self, rel: str) -> Optional[str]:
        """File content at HEAD (None if untracked/new)."""
        return self._fetch(rel)

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()


def verify_edit(root: str, severity: str = "warn") -> str:
    """Post-edit graph-integrity oracle. Returns the verdict report; the
    caller maps STOP+strict to exit code 1."""
    buf = io.StringIO()
    buf.write(f"# codeloom --verify-edit ({severity})\n")
    changed, gerr = _git_changed_files(root)
    if changed is None:
        # git itself failed (not a repo, no HEAD, unreadable): an empty
        # working tree is NOT a verified-clean tree — say so instead of
        # silently reporting GO.
        buf.write("VERDICT: ERROR\n")
        buf.write(f"  cannot read the git tree: {gerr}\n")
        buf.write("  run from inside a git repository (or pass its root).\n")
        return buf.getvalue()
    if not changed:
        buf.write("VERDICT: GO\n")
        buf.write("  no working-tree changes (clean).\n")
        return buf.getvalue()
    head = _HeadReader(root)
    try:
        return _verify_edit_report(root, changed, head)
    finally:
        head.close()

def _verify_edit_report(root: str, changed: List[str],
                        head: "_HeadReader") -> str:
    """Verdict computation over a known-dirty tree (HEAD via `head`)."""
    buf = io.StringIO()
    # files available for import resolution
    files: List[str] = []
    gi = os.path.join(root, ".gitignore")
    rules = parse_gitignore(gi) if os.path.isfile(gi) else []
    _walk(root, rules, 50000, files)
    module_map = {}
    for f in files:
        if f.endswith(".py"):
            module_map[module_name_of(f, root)] = f
    # HEAD file set: pre-edit imports must resolve against the PRE-EDIT tree
    # (tracked files at HEAD), not the working tree, so removed modules still
    # resolve when computing what the edit dangles. ls-tree lists files at
    # HEAD including ones since deleted in the working tree.
    head_modules = {}
    ls = head.ls
    if ls:
        for rel in ls.splitlines():
            if rel.endswith(".py"):
                head_modules[module_name_of(os.path.join(root, rel), root)] = rel
    changed_py = [rel for rel in changed if rel.endswith(".py")]
    # pre-edit graph (HEAD) — edges that MUST still resolve
    pre_edges: dict = {}   # mod -> set(edges at HEAD)
    for rel in changed_py:
        abs_path = os.path.join(root, rel)
        if not os.path.isfile(abs_path):
            continue
        mod = module_name_of(abs_path, root)
        old_text = head.text(rel)
        if old_text is None:
            continue  # new file: nothing pre-existing to dangle
        e, _s = _import_edges(old_text, mod, root, head_modules)
        pre_edges[mod] = e
    # post-edit graph — re-parse changed files against the CURRENT tree
    module_map_now = dict(module_map)
    for rel in changed_py:
        abs_path = os.path.join(root, rel)
        if os.path.isfile(abs_path):
            module_map_now[module_name_of(abs_path, root)] = abs_path
    post_edges: dict = {}
    post_suspects: dict = {}
    for rel in changed_py:
        abs_path = os.path.join(root, rel)
        if not os.path.isfile(abs_path):
            continue
        mod = module_name_of(abs_path, root)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        e, s = _import_edges(text, mod, root, module_map_now)
        post_edges[mod] = e
        if s:
            post_suspects[mod] = s
    # full post-edit graph over ALL modules (for cycle detection)
    full_graph = {}
    for f in files:
        if f.endswith(".py"):
            m = module_name_of(f, root)
            if m in post_edges:
                full_graph[m] = post_edges[m]
            else:
                info = parse_module(f, root, module_map_now)
                deps = set()
                for imp in info["imports"]:
                    r = _resolve_import(imp, m, root, module_map_now)
                    if r and r != m:
                        deps.add(r)
                full_graph[m] = deps
    # verdict drivers: (tier, name, why)
    drivers = []
    # STOP 1 — provably-dangling pre-edit statically-resolvable edges:
    #   (a) target module no longer exists, or
    #   (b) the changed file no longer imports it.
    for mod, edges in sorted(pre_edges.items()):
        for dep in sorted(edges):
            if dep not in module_map_now:
                drivers.append(("STOP", "dangling-import",
                                f"{mod} -> {dep}: target module removed"))
            elif dep not in post_edges.get(mod, set()):
                drivers.append(("STOP", "dangling-import",
                                f"{mod} -> {dep}: import removed from changed file"))
    # STOP — NEW import cycles introduced by the diff vs the FULL HEAD graph.
    # pre_graph must be computed over EVERY .py module at HEAD (not just the
    # changed files): a pre-existing cycle in unchanged modules (e.g. a <-> b
    # committed long ago) must never trigger STOP. Only cycle edges the
    # working-tree diff actually introduces (in cyc_post but not cyc_head)
    # may fire the new-cycle driver.
    pre_graph = {}
    if ls:
        for rel in ls.splitlines():
            if not rel.endswith(".py"):
                continue
            mod = module_name_of(os.path.join(root, rel), root)
            head_text = head.text(rel)
            if head_text is None:
                continue
            e, _s = _import_edges(head_text, mod, root, head_modules)
            pre_graph[mod] = e

    def _cycle_edges(g: dict) -> set:
        cyc = set()
        for mod, deps in sorted(g.items()):
            for dep in sorted(deps):
                if dep in g and mod in reachable(g, dep, "out"):
                    cyc.add((mod, dep))
        return cyc
    cyc_head = _cycle_edges(pre_graph)
    cyc_post = _cycle_edges(full_graph)
    for mod, dep in sorted(cyc_post - cyc_head):
        drivers.append(("STOP", "new-cycle", f"{mod} -> {dep}"))
    # CHECK — dynamic/lazy/vendored import suspects (never fatal)
    for mod, suspects in sorted(post_suspects.items()):
        for s in sorted(suspects):
            name = s if s in ("dynamic_import", "vendored_import",
                              "star_import", "syntax_error") else "dynamic_import"
            drivers.append(("CHECK", name, f"{mod}: {s}"))
    for tier, name, why in drivers:
        buf.write(f"  {tier}  [{name}]  {why}\n")
    stops = [d for d in drivers if d[0] == "STOP"]
    checks = [d for d in drivers if d[0] == "CHECK"]
    if stops:
        buf.write(f"\nVERDICT: STOP — {len(stops)} provably-dangling pre-edit edge(s)/new cycle(s) detected.\n")
        buf.write("  Fix the imports before committing; re-run --verify-edit to confirm.\n")
    elif checks:
        buf.write(f"\nVERDICT: CHECK — {len(checks)} suspect(s) (dynamic/lazy/vendored); review, not blocking.\n")
    else:
        buf.write("\nVERDICT: GO — clean, no dangling edges, no new cycles.\n")
    return buf.getvalue()

def _import_edges(text: str, mod: str, root: str,
                  module_map: dict) -> Tuple[set, set]:
    """Resolve import edges (local modules) + suspect flags for a file body."""
    edges: set = set()
    suspects: set = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        suspects.add("syntax_error")
        return edges, suspects
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                r = _resolve_import(a.name, mod, root, module_map)
                if r and r != mod:
                    edges.add(r)
                elif not r:
                    suspects.add("vendored_import")
        elif isinstance(node, ast.ImportFrom):
            base = (node.module or "").strip()
            if node.level:
                pkg = ".".join(mod.split(".")[:-1]) if "." in mod else ""
                parts = pkg.split(".") if pkg else []
                lvl = node.level
                while lvl > 1 and parts:
                    parts.pop(); lvl -= 1
                base = ".".join(parts + ([base] if base else []))
            if base:
                r = _resolve_import(base, mod, root, module_map)
                if r and r != mod:
                    edges.add(r)
                elif any(n.name == "*" for n in node.names):
                    suspects.add("star_import")
                else:
                    suspects.add("vendored_import")
            elif any(n.name == "*" for n in node.names):
                suspects.add("star_import")
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id in ("import_module", "__import__"):
                suspects.add("dynamic_import")
            elif isinstance(fn, ast.Attribute) and fn.attr == "import_module":
                suspects.add("dynamic_import")
    if IMPORT_SUSPECT_RE.search(text):
        suspects.add("dynamic_import")
    return edges, suspects


# --------------------------------------------------------------------------- #
# --blindspot: hot set vs impact-derived read set
# STOP-tier: you're editing a file that was NEVER read (not in the hot set).
# CHECK-tier: blast-radius dependents of the edited file were never read.
# --no-blindspot opts out (prints a skip verdict).
# --------------------------------------------------------------------------- #
def render_blindspot(root: str, severity: str = "warn") -> str:
    """Compare --mark-seen hotset against --impact-derived read set."""
    buf = io.StringIO()
    buf.write(f"# codeloom --blindspot ({root})\n")
    hot = set(get_hot_set(root))
    if not hot:
        buf.write("VERDICT: SKIP — nothing marked as read yet.\n")
        buf.write("  No hot set exists in this repo (--mark-seen writes it), so there\n")
        buf.write("  is nothing to compare the impact-derived read set against.\n")
        buf.write("  Fix: run `codeloom --mark-seen <files...>` after reading them.\n")
        return buf.getvalue()
    gi = os.path.join(root, ".gitignore")
    rules = parse_gitignore(gi) if os.path.isfile(gi) else []
    files: List[str] = []
    _walk(root, rules, 50000, files)
    graph = build_graph(files, root)
    # normalize hot-set entries: bare names, relative paths, module dots
    def norm(x: str) -> str:
        x = x.strip()
        if x.endswith(".py"):
            return module_name_of(os.path.join(root, x), root) \
                if not os.path.isabs(x) else module_name_of(x, root)
        if os.path.sep in x or "/" in x:
            p = os.path.join(root, x) if not os.path.isabs(x) else x
            return module_name_of(p, root) if os.path.isfile(p) else x
        return x
    hot_mods = {norm(h) for h in hot}
    # map module -> relpath for friendly output
    mod_to_rel = {}
    for f in files:
        if f.endswith(".py"):
            mod_to_rel[module_name_of(f, root)] = os.path.relpath(f, root)
    drivers = []  # (tier, why)
    for mod in sorted(graph):
        if mod in hot_mods:
            continue
        rel = mod_to_rel.get(mod, mod)
        # check whether ANY hot entry covers this file (prefix/suffix match)
        covered = any(h == mod or mod.startswith(h + ".") or h.startswith(mod + ".")
                      or h in mod or mod in h for h in hot_mods)
        if covered:
            continue
        # STOP-tier: editing a file never read
        drivers.append(("STOP", f"never-read file: {rel}"))
        # CHECK-tier: blast-radius dependents of never-read files
        for dep in _dependents(graph, mod):
            drel = mod_to_rel.get(dep, dep)
            dep_covered = any(h == dep or dep.startswith(h + ".") or h.startswith(dep + ".")
                              or h in dep or dep in h for h in hot_mods)
            if not dep_covered:
                drivers.append(("CHECK", f"unread dependent of {rel}: {drel}"))
    if not drivers:
        buf.write("VERDICT: GO — every editable file (and its dependents) has been read.\n")
        return buf.getvalue()
    seen = set()
    for tier, why in drivers:
        if why in seen:
            continue
        seen.add(why)
        buf.write(f"{tier}  {why}\n")
    stops = sum(1 for t, _ in drivers if t == "STOP")
    checks = sum(1 for t, _ in drivers if t == "CHECK")
    buf.write(f"\nVERDICT: {'STOP' if stops else 'CHECK'} — {stops} never-read file(s), "
              f"{checks} unread blast-radius dependent(s).\n"
              "  Read them first, or run --mark-seen <files> after reading, or "
              "--no-blindspot to override.\n")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# --savings-report: token/time savings vs a grep+read baseline
# Methodology: baseline = per invoked command, grep+read of the same bytes
# costs ~4x the tokens codeloom emits (codeloom is a compressed map; the
# agent would otherwise read whole files). est_tokens_out = bytes/4 of what
# the command emitted (honest estimate); est_tokens_in = the grep+read
# baseline for the same repo state = 4x est_tokens_out (reading whole files).
# All local; nothing leaves the machine. When --pack feeds --task, the pack
# row IS the task context (one command), so no double counting: each row is
# counted once, and the task row's input is the same pack content.
# --------------------------------------------------------------------------- #
def render_savings_report(root: str, since_days: Optional[int] = None,
                          repo_filter: Optional[str] = None) -> str:
    import json as _json
    import datetime as _dt
    path = _session_path(root)
    buf = io.StringIO()
    buf.write("# codeloom --savings-report\n")
    buf.write("# Methodology: baseline = grep+read chain per command; est_tokens_out = bytes/4\n")
    buf.write("# (honest estimate). est_tokens_in = grep+read input the command replaced (4x\n")
    buf.write("# output estimate). No double counting: --pack output that feeds --task is\n")
    buf.write("# counted once per row. All local — nothing leaves this machine.\n")
    if not os.path.isfile(path):
        buf.write("  No session log yet. Run `codeloom --session` to start logging.\n")
        return buf.getvalue()
    now = _dt.datetime.now(_dt.timezone.utc)
    rows = []
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
                ts = e.get("ts", 0)
                if since_days:
                    try:
                        row_dt = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
                        if (now - row_dt).days > since_days:
                            continue
                    except (ValueError, OSError, OverflowError):
                        continue
                if repo_filter:
                    rp = e.get("root", "")
                    if rp and os.path.abspath(rp) != os.path.abspath(repo_filter):
                        continue
                rows.append(e)
    except OSError:
        buf.write("  Could not read session log.\n")
        return buf.getvalue()
    if not rows:
        buf.write("  No rows match the filter.\n")
        return buf.getvalue()
    # aggregate per repo or per day
    groups = {}
    for e in rows:
        key = e.get("root", "?") if repo_filter is None else \
            _dt.datetime.fromtimestamp(e.get("ts", 0), _dt.timezone.utc).strftime("%Y-%m-%d")
        groups.setdefault(key, []).append(e)
    total_out = 0
    total_in = 0
    total_seconds = 0.0
    for key in sorted(groups):
        g = groups[key]
        tout = sum(e.get("est_tokens_out", e.get("bytes", 0) // 4) for e in g)
        tin = sum(e.get("est_tokens_in", tout * 4) for e in g)
        secs = sum(e.get("seconds", 0.0) for e in g)
        total_out += tout
        total_in += tin
        total_seconds += secs
        buf.write(f"## {key}\n")
        buf.write(f"  {len(g)} call(s), ~{tout} tokens emitted, ~{tin} tokens baseline "
                  f"(grep+read), ~{secs:.1f}s saved\n")
    buf.write(f"\nTOTAL: {len(rows)} call(s), ~{total_out} tokens emitted, "
              f"~{total_in} tokens baseline, ~{total_seconds:.1f}s saved\n")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Memory growth bounds: caps + lossless archive + --memory-prune
# Each of DECISIONS.md / LESSONS.md / SUPERSEDED.md is capped at
# MEMORY_FILE_CAP bytes. When a write would exceed the cap, the OLDEST
# entries are rotated deterministically (entry = leading '- ' lines, oldest
# first) into .codeloom-memory/archive/<FILE>-YYYY-MM-DD.md. Lossless move:
# content is copied, never deleted, never summarized. The session JSONL
# rotates weekly into archive/session-YYYY-MM-DD.jsonl. --memory-prune
# reports what it WOULD remove and deletes ONLY with --delete; there is no
# auto-delete path.
# --------------------------------------------------------------------------- #
MEMORY_CAPPED_FILES = ("DECISIONS.md", "LESSONS.md", "SUPERSEDED.md")
MEMORY_FILE_CAP = 200 * 1024  # 200 KB per memory file
_SESSION_WEEK_SECS = 7 * 24 * 3600

def _memory_archive_dir(root: str) -> str:
    d = os.path.join(_memory_dir(root), "archive")
    if not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return d

def _memory_cap(root: str, name: str) -> int:
    """Cap in bytes; env override (CODELOOM_MEMORY_CAP_BYTES) is honored so
    tests can force rotation without writing 200KB."""
    try:
        env = int(os.environ.get("CODELOOM_MEMORY_CAP_BYTES", "") or 0)
        if env > 0:
            return env
    except ValueError:
        pass
    return MEMORY_FILE_CAP

def memory_rotate(root: str, name: str) -> bool:
    """Rotate the OLDEST entries out of <name> into archive/<name>-YYYY-MM-DD.md.
    Returns True if a rotation happened (lossless: entries moved, not deleted)."""
    d = _memory_dir(root)
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        return False
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    cap = _memory_cap(root, name)
    if len(text.encode("utf-8")) <= cap:
        return False
    # split into entries: lines starting with '- ' (or '*'/digits) are entry
    # headers; an entry is a header + its following non-header lines.
    lines = text.splitlines(keepends=True)
    entries = []  # (start_idx, end_idx)
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(("- ", "* ", "## ")) or ln.strip()[:1].isdigit() \
           or ln.startswith(("### ", "# ")):
            if start is not None:
                entries.append((start, i))
            start = i
    if start is not None:
        entries.append((start, len(lines)))
    if not entries:
        # no structured entries: rotate by halves (oldest first)
        entries = [(0, len(lines) // 2), (len(lines) // 2, len(lines))]
    # move oldest entries until under cap
    removed = []
    remaining = lines
    while len("".join(remaining).encode("utf-8")) > cap and entries:
        s, e = entries.pop(0)
        removed.append((s, e))
        remaining = lines[e:]
        if not remaining:
            break
    if not removed:
        return False
    import datetime as _dt
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    arch = os.path.join(_memory_archive_dir(root), f"{name}-{stamp}.md")
    removed_text = "".join(lines[s] for s, e in removed)
    header = f"# archived from {name} (rotated {stamp})\n"
    try:
        with open(arch, "a", encoding="utf-8") as fh:
            fh.write(header if not os.path.isfile(arch) else "")
            fh.write(removed_text)
        with open(p, "w", encoding="utf-8") as fh:
            fh.writelines(remaining)
        return True
    except OSError:
        return False

def session_rotate_weekly(root: str) -> Optional[str]:
    """Rotate .codeloom-session.jsonl weekly into archive/session-YYYY-MM-DD.jsonl.
    Returns the archive path, or None when no rotation happened."""
    path = _session_path(root)
    if not os.path.isfile(path):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    if now - mtime < _SESSION_WEEK_SECS:
        return None
    stamp = _dt.datetime.fromtimestamp(mtime, _dt.timezone.utc).strftime("%Y-%m-%d")
    arch = os.path.join(_memory_archive_dir(root), f"session-{stamp}.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        with open(arch, "a", encoding="utf-8") as fh:
            fh.write(content)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("")
        return arch
    except OSError:
        return None

def render_memory_prune(root: str, older_than_days: int = 90,
                        do_delete: bool = False) -> str:
    """--memory-prune: report (dry-run) or delete (--delete) old archive
    entries. NEVER auto-deletes."""
    import datetime as _dt
    arch = os.path.join(_memory_dir(root), "archive")
    buf = io.StringIO()
    buf.write(f"# codeloom --memory-prune {'(delete)' if do_delete else '(dry-run)'} "
              f"({root})\n")
    if not os.path.isdir(arch):
        buf.write("  no archive dir — nothing to prune.\n")
        return buf.getvalue()
    now = _dt.datetime.now(_dt.timezone.utc)
    candidates = []
    for fn in sorted(os.listdir(arch)):
        p = os.path.join(arch, fn)
        try:
            mtime = os.path.getmtime(p)
            age_days = (now - _dt.datetime.fromtimestamp(mtime, _dt.timezone.utc)).days
        except (OSError, ValueError, OverflowError):
            continue
        if age_days > older_than_days:
            candidates.append((fn, age_days))
    if not candidates:
        buf.write("  nothing older than %d days.\n" % older_than_days)
        return buf.getvalue()
    for fn, age in candidates:
        buf.write(f"  would remove: {fn} ({age}d)\n")
    if do_delete:
        removed = 0
        for fn, _ in candidates:
            try:
                os.remove(os.path.join(arch, fn))
                removed += 1
            except OSError:
                pass
        buf.write(f"\ndeleted {removed} archived file(s).\n")
    else:
        buf.write(f"\n{len(candidates)} file(s) would be removed; re-run with "
                  f"--delete to actually delete. Nothing was deleted.\n")
    return buf.getvalue()

def render_memory_line(root: str) -> str:
    """One-line memory stats for --session-report / --savings-report."""
    d = _memory_dir(root)
    total = 0
    kb = 0
    archived = 0
    for dirpath, _, fnames in os.walk(d):
        for fn in fnames:
            if fn.startswith("."):
                continue
            p = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            total += 1
            kb += sz
            if os.path.basename(dirpath) == "archive":
                archived += 1
    return f"memory: {total} files, {kb // 1024} KB ({archived} archived)"


# --------------------------------------------------------------------------- #
# Memory OS: typed, graph-linked repository memory (memory.jsonl)
# --------------------------------------------------------------------------- #
# The differentiator layer: every memory write ALSO appends a typed JSON entry
# to .codeloom-memory/memory.jsonl (append-only JSONL, one JSON object per
# line), while the legacy markdown files keep working unchanged. memory.jsonl
# obeys the same 200 KB cap via lossless rotation into
# archive/memory-<date>.jsonl (pure JSONL lines, no header — every line stays
# parseable). --memory <symbol> is the graph-linked retrieval superpower:
# entries pinned to a symbol plus entries pinned to its call/import-graph
# neighbors. Deterministic everywhere: zero deps, no daemon, no telemetry.
# --------------------------------------------------------------------------- #
MEMORY_JSONL = "memory.jsonl"
MEMORY_TYPES = ("decision", "bug", "question", "architecture", "api",
                "constraint", "lesson", "todo", "warning", "goal",
                "hypothesis")
MEMORY_CREATED_SOURCES = ("decide", "lesson", "adr", "goal", "hypothesis",
                          "checkpoint", "extract", "memory")
MEMORY_TYPE_WEIGHT = {"bug": 20, "architecture": 15, "constraint": 15,
                      "warning": 15, "decision": 10, "todo": 5, "goal": 5,
                      "hypothesis": 5, "lesson": 5, "question": 5, "api": 5}
MEMORY_KEYWORDS = ("always", "never", "must", "critical", "important",
                   "security", "do not", "dont")
MEMORY_CONFIDENCE = {"decision": 0.9, "bug": 0.8, "lesson": 0.8,
                     "architecture": 0.8, "constraint": 0.8, "api": 0.7,
                     "goal": 0.7, "warning": 0.6, "todo": 0.5,
                     "hypothesis": 0.5, "question": 0.4}
MEMORY_IMPORTANCE_CAP = 100
MEMORY_IMPORTANCE_HELP = (
    "importance = 10 base +30 if title/body contains "
    "always|never|must|critical|important|security|do not|dont + type_weight "
    "(bug 20, architecture 15, constraint 15, warning 15, decision 10, todo 5, "
    "others 5) + graph_centrality (0 affected symbols -> 0, 1-2 -> 5, 3+ -> 10) "
    "+ recency (+10 within 7 days, +5 within 30); capped at 100. "
    "--priority N overrides the computed value.")


def _memory_jsonl_path(root: str) -> str:
    return os.path.join(_memory_dir(root), MEMORY_JSONL)


def _memory_entries(root: str, include_archive: bool = False) -> List[dict]:
    """Read typed memory entries from memory.jsonl (+ archive/memory-*.jsonl
    when include_archive). Corrupt lines are skipped, never fatal."""
    import json as _json
    out: List[dict] = []

    def _read(p: str) -> None:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if isinstance(e, dict):
                        out.append(e)
        except OSError:
            pass

    _read(_memory_jsonl_path(root))
    if include_archive:
        arch = _memory_archive_dir(root)
        if os.path.isdir(arch):
            for fn in sorted(os.listdir(arch)):
                if fn.startswith("memory") and fn.endswith(".jsonl"):
                    _read(os.path.join(arch, fn))
    return out


def memory_importance(title: str, body: str = "", type_: str = "",
                      symbols: Optional[List[str]] = None) -> int:
    """Deterministic importance score (documented in MEMORY_IMPORTANCE_HELP):
    10 base + 30 keyword bonus + type weight + graph centrality (affected
    symbols) + recency (a fresh write is within 7 days -> +10). Cap 100."""
    text = ("%s %s" % (title or "", body or "")).lower()
    score = 10
    if any(k in text for k in MEMORY_KEYWORDS):
        score += 30
    score += MEMORY_TYPE_WEIGHT.get((type_ or "").strip().lower(), 5)
    n = len([s for s in (symbols or []) if s and s.strip()])
    if n >= 3:
        score += 10
    elif n >= 1:
        score += 5
    score += 10  # recency: a just-written entry is within 7 days
    return min(MEMORY_IMPORTANCE_CAP, score)


def _memory_tier(importance: int) -> str:
    if importance >= 70:
        return "hot"
    if importance >= 40:
        return "active"
    return "archive"


def _memory_epoch(ts: str) -> Optional[float]:
    """Parse an entry timestamp to epoch seconds; None when unparseable.
    Accepts aware ISO-8601 (Z or offset) AND naive datetimes (treated as
    UTC — the naive/aware subtraction crash and the silent recency=0 for
    naive timestamps were both bugs)."""
    import datetime as _dt
    if not isinstance(ts, str) or not ts:
        return None
    try:
        t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.timestamp()


def _memory_recency(entry: dict) -> int:
    """Read-time recency bonus: +10 within 7 days, +5 within 30, else 0.
    Used as the deterministic tiebreaker for retrieval ranking. Naive
    timestamps parse as UTC (no crash, no silent zero)."""
    ep = _memory_epoch(entry.get("timestamp", ""))
    if ep is None:
        return 0
    import time as _time
    days = (_time.time() - ep) / 86400.0
    if days <= 7:
        return 10
    if days <= 30:
        return 5
    return 0


def _memory_sort_key(entry: dict) -> Tuple:
    """Deterministic retrieval ranking: importance desc, then recency desc,
    then timestamp desc (newest first), then id for a stable total order.
    Timestamps are compared as epoch floats so mixed naive/aware stamps
    never raise (naive treated as UTC, unparseable = oldest). Schema-corrupt
    entries (string importance, non-string id) sort as lowest importance
    instead of crashing retrieval — corrupt-line tolerance covers JSON-valid
    but wrong-typed lines, not just unparseable JSON."""
    ep = _memory_epoch(entry.get("timestamp", ""))
    imp = entry.get("importance", 0) or 0
    try:
        imp = int(imp)
    except (ValueError, TypeError):
        imp = 0
    ident = entry.get("id")
    if not isinstance(ident, str):
        ident = str(ident) if ident is not None else ""
    return (-imp,
            -_memory_recency(entry),
            -(ep if ep is not None else -1.0),
            ident)


def _memory_scan_id(root: str, type_: str) -> Tuple[int, int]:
    """Stream live + archived JSONL for one type; return (count, max_numeric_id).

    Cheap write-path scan — no entry materialization (the old path parsed
    every line into a dict on EVERY write). count guards id uniqueness when
    entries carry no numeric suffix (legacy sha256-style ids); max_numeric_id
    keeps the counter monotonic across archive loss: counting entries instead
    under-counts after --memory-prune removes archive files, which would
    recycle ids and collide with live entries."""
    import json as _json
    import re as _re
    count = 0
    max_n = 0

    def _scan(p: str) -> None:
        nonlocal count, max_n
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"type"' not in line:
                        continue
                    try:
                        e = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if not isinstance(e, dict) or e.get("type") != type_:
                        continue
                    count += 1
                    m = _re.search(r"(\d+)\s*$", str(e.get("id", "")))
                    if m:
                        max_n = max(max_n, int(m.group(1)))
        except OSError:
            pass

    _scan(_memory_jsonl_path(root))
    arch = _memory_archive_dir(root)
    if os.path.isdir(arch):
        for fn in sorted(os.listdir(arch)):
            if fn.startswith("memory") and fn.endswith(".jsonl"):
                _scan(os.path.join(arch, fn))
    return count, max_n


def _memory_next_id(root: str, type_: str) -> str:
    """Deterministic '<slug>-NNN' id: NNN = max existing numeric id + 1 across
    live + archived entries (rotation is lossless, so counters stay monotonic
    even after --memory-remove/prune deletes archive files — counting entries
    instead would recycle ids and collide with live entries)."""
    count, max_n = _memory_scan_id(root, type_)
    return "%s-%03d" % (type_, max(count, max_n) + 1)


def memory_append(root: str, type_: str, title: str, body: str = "",
                  reason: str = "", symbols: Optional[List[str]] = None,
                  priority: Optional[int] = None,
                  created: str = "memory") -> dict:
    """Append one typed entry to memory.jsonl (append-only, capped by the
    existing memory_rotate path into archive/memory-<date>.jsonl). Returns the
    entry dict. Never raises on disk errors: the entry dict carries an
    'error' key instead, so callers can still report the attempt."""
    import datetime as _dt
    import json as _json
    type_ = (type_ or "goal").strip().lower()
    if type_ not in MEMORY_TYPES:
        type_ = "goal"
    if created not in MEMORY_CREATED_SOURCES:
        created = "memory"
    syms = [s.strip() for s in (symbols or []) if s and s.strip()]
    title = (title or "").strip()
    entry: dict = {
        "type": type_,
        "id": _memory_next_id(root, type_),
        "title": title,
        "body": (body or "").strip(),
        "reason": (reason or "").strip(),
        "affected_symbols": syms,
        "importance": 0,
        "confidence": MEMORY_CONFIDENCE.get(type_, 0.5),
        "tier": "active",
        "timestamp": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "created": created,
    }
    imp = memory_importance(title, entry["body"], type_, syms)
    if priority is not None:
        try:
            imp = max(0, min(MEMORY_IMPORTANCE_CAP, int(priority)))
        except (ValueError, TypeError):
            pass
    entry["importance"] = imp
    entry["tier"] = _memory_tier(imp)
    line = _json.dumps(entry, ensure_ascii=False) + "\n"
    cap = _memory_cap(root, MEMORY_JSONL)
    try:
        with open(_memory_jsonl_path(root), "a", encoding="utf-8") as fh:
            fh.write(line)
        # cap check AFTER the append (post-append size): a single entry
        # larger than the cap must not rotate itself out on the next call
        # (that would shuttle it between live and archive — lossless but
        # invisible to default --memory/--memory-stats). If the whole file
        # exceeds the cap, rotate OLDEST lines out; a lone oversized entry
        # stays live (it IS the newest) and the file is allowed to exceed
        # the cap until a smaller entry triggers rotation.
        if _memory_jsonl_size(root) > cap:
            memory_rotate_jsonl(root)
    except OSError as e:
        entry["error"] = str(e)
    return entry


def _memory_jsonl_size(root: str) -> int:
    """Byte size of memory.jsonl (0 when missing)."""
    p = _memory_jsonl_path(root)
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def memory_rotate_jsonl(root: str) -> bool:
    """Rotate the OLDEST lines out of memory.jsonl into
    archive/memory-YYYY-MM-DD.jsonl when the cap is exceeded (lossless move —
    lines are copied, never deleted, never summarized). Archive files are
    pure JSONL (no header) so every line stays machine-parseable."""
    p = _memory_jsonl_path(root)
    if not os.path.isfile(p):
        return False
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    cap = _memory_cap(root, MEMORY_JSONL)
    size = sum(len(l.encode("utf-8")) for l in lines)
    if size <= cap:
        return False
    removed = []
    # Leave at least one line in place: when a single entry alone exceeds
    # the cap, rotating it out would hide the just-written entry from
    # default (live-only) retrieval entirely — the file is allowed to ride
    # above the cap until a smaller entry triggers rotation.
    while len(lines) > 1 and sum(len(l.encode("utf-8")) for l in lines) > cap:
        removed.append(lines.pop(0))
    if not removed:
        return False
    import datetime as _dt
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    arch = os.path.join(_memory_archive_dir(root), f"memory-{stamp}.jsonl")
    try:
        with open(arch, "a", encoding="utf-8") as fh:
            fh.writelines(removed)
        with open(p, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        return True
    except OSError:
        return False


def _memory_line(entry: dict) -> str:
    """One retrieval line: 'T [priority] type: title — body[:200]'."""
    t = entry.get("title")
    b = entry.get("body")
    if not isinstance(t, str):
        t = str(t) if t is not None else ""
    if not isinstance(b, str):
        b = str(b) if b is not None else ""
    t = t.strip()
    b = b.strip()
    out = "%s [%s] %s: %s" % (entry.get("tier", "?"),
                              entry.get("importance", 0),
                              entry.get("type", "?"),
                              t or "(untitled)")
    if b:
        out += " — " + b[:200]
    return out


def _memory_symbols(files: List[str], root: str) -> Tuple[Set[str], Dict[str, Set[str]]]:
    """{module names} and {symbol -> modules} for the codebase (Python ast).
    Deterministic; empty sets for non-Python-only repos."""
    modules: Set[str] = set()
    symbols: Dict[str, Set[str]] = {}
    for f in files:
        if not f.endswith(".py"):
            continue
        mod = module_name_of(f, root)
        modules.add(mod)
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                symbols.setdefault(node.name, set()).add(mod)
    return modules, symbols


def _memory_resolve(sym: str, modules: Set[str],
                    symbols: Dict[str, Set[str]]) -> Optional[str]:
    """Resolve a symbol to its defining module: exact module, exact symbol,
    then case-insensitive matches. None when not in the codebase."""
    if sym in modules:
        return sym
    if sym in symbols:
        return min(symbols[sym])
    low = sym.lower()
    for m in sorted(modules):
        if m.lower() == low:
            return m
    for name, mods in symbols.items():
        if name.lower() == low:
            return min(mods)
    return None


def _memory_symbol_list(entry: dict) -> List[str]:
    """Non-empty string symbols from an entry's affected_symbols. Corrupt
    shapes (string instead of list, non-string members, non-iterable) are
    normalized instead of crashing retrieval/stats."""
    raw = entry.get("affected_symbols")
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, (list, tuple, set)):
        return []
    return [s.strip() for s in raw if isinstance(s, str) and s.strip()]


def memory_symbol_resolve(files: List[str], root: str, sym: str) -> bool:
    """True when `sym` names a module/symbol in the repo OR is pinned in
    memory.jsonl — used by --remember's smart dispatch (retrieval vs the
    legacy append-to-section write)."""
    modules, symbols = _memory_symbols(files, root)
    if _memory_resolve(sym.strip(), modules, symbols) is not None:
        return True
    low = sym.strip().lower()
    return any(low in [s.lower() for s in _memory_symbol_list(e)]
               for e in _memory_entries(root))


def render_memory_graph(files: List[str], root: str, symbol: str,
                        target_root: Optional[str] = None,
                        include_archive: bool = False,
                        max_files: int = 5000) -> str:
    """--memory <symbol>: graph-linked memory retrieval. Direct entries
    (affected_symbols contain the symbol), ranked importance desc then
    recency, then a 'reachable via graph' section: entries whose
    affected_symbols match graph NEIGHBORS of the symbol — modules that
    import it or that it imports (build_graph) plus modules whose functions
    call it (build_call_graph). Deterministic ordering throughout."""
    buf = io.StringIO()
    sym = symbol.strip()
    buf.write(f"# memory: {sym}\n")
    entries = _memory_entries(root, include_archive=include_archive)
    if not entries:
        buf.write("  no typed memory yet — add some with "
                  "--decide/--lesson/--adr/--goal/--hypothesis/--memory-add.\n")
        return buf.getvalue()
    tgt = os.path.abspath(target_root or root)
    # --target-root: graph expansion runs against ANOTHER repo, so walk THAT
    # root for its own files — reusing the memory root's file list would
    # module-name them relative to tgt, producing garbage '..' dotted names
    # and an empty graph (files must live under the root they are named
    # against).
    if target_root:
        tfiles: List[str] = []
        rules = []
        gi = os.path.join(tgt, ".gitignore")
        if os.path.isfile(gi):
            rules = parse_gitignore(gi)
        _walk(tgt, rules, max_files, tfiles)
    else:
        tfiles = files
    modules, symbols = _memory_symbols(tfiles, tgt)
    low = sym.lower()
    direct = [e for e in entries
              if any(s.lower() == low for s in _memory_symbol_list(e))]
    direct.sort(key=_memory_sort_key)
    buf.write("\n## entries linked to %s\n" % sym)
    if direct:
        for e in direct:
            buf.write("  " + _memory_line(e) + "\n")
    else:
        buf.write("  no entries linked directly to %s.\n" % sym)

    buf.write("\n## reachable via graph\n")
    mod = _memory_resolve(sym, modules, symbols)
    if mod is None:
        buf.write(f"  {sym} is not a known module/symbol in the codebase "
                  f"({tgt}) — no graph to expand.\n")
        return buf.getvalue()
    try:
        graph = build_graph(tfiles, tgt)
    except Exception:
        graph = {}
    neighbors: Set[str] = set()
    for m, deps in graph.items():
        if mod in deps:
            neighbors.add(m)
    for dep in graph.get(mod, set()):
        neighbors.add(dep)
    try:
        calls = build_call_graph(tfiles, tgt)
        for m, fns in calls.items():
            for _caller, callees in fns.items():
                if sym in callees and m != mod:
                    neighbors.add(m)
    except Exception:
        pass
    if neighbors:
        buf.write("  graph neighbors of %s (import/call graph): %s\n" % (
            sym, ", ".join(sorted(neighbors))))
    else:
        buf.write(f"  no graph neighbors of {sym} found.\n")
    neigh_symbols: Set[str] = set(neighbors)
    for name, mods in symbols.items():
        if mods & neighbors:
            neigh_symbols.add(name)
    reach = [e for e in entries
             if any(s.lower() in {x.lower() for x in neigh_symbols}
                    for s in _memory_symbol_list(e))]
    direct_ids = {id(e) for e in direct}
    reach = [e for e in reach if id(e) not in direct_ids]
    reach.sort(key=_memory_sort_key)
    if reach:
        for e in reach:
            buf.write("  " + _memory_line(e) + "\n")
    else:
        buf.write("  no entries reachable via graph neighbors of %s.\n" % sym)
    return buf.getvalue()


def render_memory_stats(root: str) -> str:
    """--memory-stats: one-screen counts by type, by tier, total bytes,
    archive size, and top-5 symbols by linked-memory count."""
    entries = _memory_entries(root)
    buf = io.StringIO()
    buf.write(f"# codeloom --memory-stats ({root})\n")
    if not entries:
        buf.write("  no typed memory yet — add some with "
                  "--decide/--memory-add/--lesson/--adr/--goal/--hypothesis.\n")
        return buf.getvalue()
    by_type: Dict[str, int] = {}
    by_tier: Dict[str, int] = {}
    for e in entries:
        by_type[e.get("type", "?")] = by_type.get(e.get("type", "?"), 0) + 1
        by_tier[e.get("tier", "?")] = by_tier.get(e.get("tier", "?"), 0) + 1
    buf.write(f"total entries: {len(entries)}\n\n")
    buf.write("by type:\n")
    for t in sorted(by_type):
        buf.write(f"  {t}: {by_type[t]}\n")
    buf.write("\nby tier:\n")
    for t in ("hot", "active", "archive"):
        if t in by_tier:
            buf.write(f"  {t}: {by_tier[t]}\n")
    total_bytes = 0
    archive_bytes = 0
    for dirpath, _, fnames in os.walk(_memory_dir(root)):
        for fn in fnames:
            if fn.startswith("."):
                continue
            p = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            total_bytes += sz
            if os.path.basename(dirpath) == "archive":
                archive_bytes += sz
    buf.write(f"\ntotal bytes: {total_bytes}\narchive bytes: {archive_bytes}\n")
    counts: Dict[str, int] = {}
    for e in entries:
        for s in _memory_symbol_list(e):
            counts[s] = counts.get(s, 0) + 1
    buf.write("\ntop linked symbols:\n")
    if counts:
        for s, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]:
            buf.write(f"  {s}: {n}\n")
    else:
        buf.write("  (no affected_symbols linked yet — pass --symbols on write)\n")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# --eval plumbing: shell out to benchmarks/eval_runner.py (argv-style)
# --------------------------------------------------------------------------- #
def run_eval(kind: str, root: str, as_json: bool = False) -> int:
    """Run benchmarks/eval_runner.py <kind> [--json] [--root PATH] as a
    subprocess (no shell). The runner is owned by benchmarks/ (other agents);
    codeloom only plumbs argv and relays stdout. CODELOOM_EVAL_RUNNER can
    override the runner path (used by tests to stub the runner)."""
    bench = os.environ.get("CODELOOM_EVAL_RUNNER") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "benchmarks", "eval_runner.py")
    if not os.path.isfile(bench):
        print(f"# eval: benchmarks/eval_runner.py not found at {bench}\n"
              f"  (owned by the benchmarks agent — not built yet; nothing to run).")
        return 1
    import subprocess as _sp
    cmd = [sys.executable, bench, kind]
    if as_json:
        cmd.append("--json")
    cmd += ["--root", root]
    try:
        r = _sp.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        print(f"# eval: failed to run {bench}: {e}")
        return 1
    sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode


# --------------------------------------------------------------------------- #
# --install-hook / --uninstall-hook: pre-commit hook installer
# The hook body lives in scripts/pre-commit-hook.sh (owned by another
# agent); codeloom only writes the .git/hooks/pre-commit wrapper that
# references it. Idempotent: re-running updates the wrapper.
# --------------------------------------------------------------------------- #
HOOK_MARKER = "# codeloom-managed pre-commit hook"

def _hook_script_path(root: str) -> str:
    return os.path.join(root, "scripts", "pre-commit-hook.sh")

def install_hook(root: str) -> str:
    """Write .git/hooks/pre-commit referencing scripts/pre-commit-hook.sh."""
    git_dir = os.path.join(root, ".git")
    if not os.path.isdir(git_dir):
        return "install-hook: not a git repo (.git missing) — hook not installed."
    hooks = os.path.join(git_dir, "hooks")
    try:
        os.makedirs(hooks, exist_ok=True)
    except OSError as e:
        return f"install-hook: cannot create {hooks}: {e}"
    script = _hook_script_path(root)
    if not os.path.isfile(script):
        return (f"install-hook: scripts/pre-commit-hook.sh not found at {script}.\n"
                f"  The hook body lives in the repo scripts dir (owned by the\n"
                f"  scripts agent) — codeloom only installs the wrapper.")
    body = (
        "#!/bin/sh\n"
        f"# {HOOK_MARKER} (installed by codeloom --install-hook)\n"
        "# Runs the warn-only pre-commit check. The hook body lives in\n"
        "# scripts/pre-commit-hook.sh; this file only references it, so the\n"
        "# check can be updated without reinstalling. Always exits 0 unless\n"
        "# the script itself fails — codeloom hooks never block a commit\n"
        "# (they warn).\n"
        f"HOOK_SCRIPT='{script.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
        "if [ -x \"$HOOK_SCRIPT\" ]; then\n"
        "  \"$HOOK_SCRIPT\"\n"
        "  rc=$?\n"
        "  if [ $rc -ne 0 ]; then\n"
        "    echo \"pre-commit-hook.sh exited $rc; codeloom hook is warn-only and\"\n"
        "    echo \"will NOT block the commit. Fix warnings before pushing.\"\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "echo \"codeloom pre-commit hook: scripts/pre-commit-hook.sh not found; skipping.\"\n"
        "exit 0\n"
    )
    hook_path = os.path.join(hooks, "pre-commit")
    try:
        with open(hook_path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(hook_path, 0o755)
    except OSError as e:
        return f"install-hook: write failed: {e}"
    return f"installed {hook_path} (idempotent; re-run updates it)."

def uninstall_hook(root: str) -> str:
    """Remove the codeloom-managed pre-commit hook (only ours)."""
    hook = os.path.join(root, ".git", "hooks", "pre-commit")
    if not os.path.isfile(hook):
        return "uninstall-hook: no pre-commit hook to remove."
    try:
        with open(hook, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(256)
    except OSError:
        return f"uninstall-hook: cannot read {hook}."
    if HOOK_MARKER not in head:
        return ("uninstall-hook: pre-commit exists but is not codeloom-managed — "
                "leaving it untouched.")
    try:
        os.remove(hook)
    except OSError as e:
        return f"uninstall-hook: remove failed: {e}"
    return f"removed codeloom-managed pre-commit hook ({hook})."


def render_text(m: dict, compact: Optional[bool] = None) -> str:
    ep = m["entry_points"]
    buf = io.StringIO()
    buf.write(f"# codeloom — {m['root']}\n")
    buf.write(f"{m['file_count']} files\n")
    if ep:
        buf.write("\n## Entry points\n")
        for e in ep:
            buf.write(f"  {os.path.relpath(e, m['root'])}\n")
    if _compact_default(compact):
        # summary-first map: file counts by top-level dir + entry points only
        # (the full tree stays one --full away).
        buf.write("\n## Structure (files by top-level dir; use --full for the tree)\n")
        for name, n in _top_level_counts(m["tree"]):
            buf.write(f"  {name}  {n} file(s)\n")
        buf.write(f"\n# {m['file_count']} files total — run with --full for the complete tree\n")
        return _cap_output(buf.getvalue())
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

def render_resume(files: List[str], root: str, max_files: int, full: bool = False) -> str:
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
    # the decisions half of the flagship promise: the working-state ledger
    # (goal, decisions, actions, open items, hot set) + persistent memory.
    # --resume --full additionally folds in the full memory files so the
    # restored context carries structure AND decisions either way.
    ledger = render_memory_ledger(root, full=full)
    if ledger:
        buf.write("\n## Decisions / working state\n")
        buf.write(ledger)
    buf.write("\n# Paste this after a context compaction to restore your structural\n"
              "# model of the repo — and the decisions you made — in one shot.\n"
              "# Re-run `codeloom --resume` any time.\n")
    return buf.getvalue()

def render_memory_ledger(root: str, full: bool = False) -> str:
    """Render the decision/working-state ledger for --resume: journal events
    (goal, decisions, actions, open items, hot set), persistent memory files
    (.codeloom-memory/*), and the latest checkpoint. `full=True` additionally
    includes the richer memory files (ARCHITECTURE, PATTERNS, CONVENTIONS,
    SUPERSEDED) so --resume --full carries the whole memory layer. Pure read —
    never creates directories or writes files."""
    buf = io.StringIO()
    events = journal_read(root)
    if events or get_hot_set(root):
        summary = build_layered_summary(root, include_structural=full)
        buf.write(summary)
        if not summary.endswith("\n"):
            buf.write("\n")
    mem = os.path.join(root, ".codeloom-memory")
    if os.path.isdir(mem):
        names = ["DECISIONS", "LESSONS"]
        if full:
            names += ["SUPERSEDED", "ARCHITECTURE", "PATTERNS", "CONVENTIONS"]
        for name in names:
            p = os.path.join(mem, name + ".md")
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read().strip()
                    if content:
                        buf.write(f"### {name}\n{content}\n\n")
                except OSError:
                    pass
    cp = _checkpoint_path(root)
    if os.path.isfile(cp):
        try:
            with open(cp, "r", encoding="utf-8", errors="replace") as fh:
                cptxt = fh.read().strip()
            if cptxt:
                buf.write("### Last checkpoint\n" + cptxt[:2000] + "\n")
        except OSError:
            pass
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# --checkpoint: snapshot in-progress work (uncommitted diff + status note) so
# it survives a context compaction. Writes .codeloom-checkpoint.md next to the
# repo. This is the "never forgets" layer on top of --resume/--remember/--seen.
# --------------------------------------------------------------------------- #
CHECKPOINT_FILE = ".codeloom-checkpoint.md"

def _checkpoint_path(root: str) -> str:
    return os.path.join(root, CHECKPOINT_FILE)

def render_checkpoint(root: str, note: Optional[str] = None) -> str:
    """Write a checkpoint of in-progress work: the git diff (uncommitted
    changes) + a status note. Returns the checkpoint text. Survives compaction
    because it's a file on disk."""
    import subprocess
    buf = io.StringIO()
    buf.write("# codeloom --checkpoint (in-progress work snapshot)\n")
    buf.write(f"# repo: {os.path.abspath(root)}\n")
    buf.write(f"# saved: {__import__('time').strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    # git diff (uncommitted changes) — the actual in-progress work
    if os.path.isdir(os.path.join(root, ".git")):
        try:
            r = subprocess.run(["git", "-C", root, "diff", "--stat"],
                               capture_output=True, text=True, timeout=20)
            stat = r.stdout.strip()
            if stat:
                buf.write("## Uncommitted changes (git diff --stat)\n")
                buf.write(stat + "\n\n")
            r2 = subprocess.run(["git", "-C", root, "diff"],
                                capture_output=True, text=True, timeout=30)
            diff = r2.stdout.strip()
            if diff:
                buf.write("## Diff (uncommitted)\n")
                # cap the diff so the checkpoint stays compact
                buf.write(diff[:4000] + ("\n... (truncated)\n" if len(diff) > 4000 else "\n"))
                buf.write("\n")
        except Exception:
            pass
    else:
        buf.write("## Not a git repo — no diff captured.\n\n")
    # status note (what the agent was doing / decided)
    if note:
        buf.write("## Status note\n")
        buf.write(note.strip() + "\n\n")
    # what's been explored (from --seen) so the agent knows what it already read
    seen = render_seen(root)
    if "No session" not in seen and "nothing" not in seen.lower():
        buf.write("## Already explored\n")
        buf.write(seen)
    # journal a checkpoint event so the working-state Status reflects it
    journal_append(root, "checkpoint", "checkpoint saved", body=note or "")
    # typed mirror: checkpoint is a documented Memory OS source
    # (created: checkpoint) — the session journal AND memory.jsonl both
    # record it, matching --goal/--decide/--lesson dual-write parity.
    me = memory_append(root, "goal", note or "checkpoint saved",
                       created="checkpoint")
    if me.get("error"):
        buf.write(f"\nmemory: jsonl mirror FAILED ({me['error']}); the "
                  f"checkpoint file was still written\n")
    # write it to disk
    try:
        with open(_checkpoint_path(root), "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
    except OSError as e:
        return f"Checkpoint write failed: {e}\n"
    return buf.getvalue()

def render_checkpoint_restore(root: str) -> str:
    """--checkpoint-restore: read the last checkpoint back so the agent can
    resume in-progress work after a compaction."""
    path = _checkpoint_path(root)
    if not os.path.isfile(path):
        return "No checkpoint found. Run `codeloom --checkpoint \"<status note>\"` to save one.\n"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        return f"Checkpoint read failed: {e}\n"

# --------------------------------------------------------------------------- #
# Working-memory journal: a declarative event log (decision/action/hypothesis/
# checkpoint/seen) + a layered summary builder. This is the "the agent does not
# forget what it did" layer — build_layered_summary turns the JSONL journal into
# a compact working-state packet that --resume --full and codeloom_get_working_
# state return after a compaction. Stdlib-only, human-readable, per-repo.
# --------------------------------------------------------------------------- #
WM_DIR = ".codeloom"

def _wm_dir(root: str) -> str:
    d = os.path.join(root, WM_DIR, "session")
    if not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return d

def _wm_gitignore(root: str) -> None:
    base = os.path.join(root, WM_DIR)
    gi = os.path.join(base, ".gitignore")
    try:
        if not os.path.isfile(gi):
            with open(gi, "w", encoding="utf-8") as fh:
                fh.write("session/\n")
    except OSError:
        pass

def _wm_journal(root: str) -> str:
    return os.path.join(_wm_dir(root), "current.jsonl")

def _wm_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _wm_session_id() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d-%H%M")

def journal_append(root: str, type_: str, title: str, body: str = "",
                    status: str = "", reason: str = "", related=None) -> str:
    """Append a structured event to the working-memory journal."""
    _wm_dir(root)
    _wm_gitignore(root)
    import json as _json
    ev: dict = {"ts": _wm_now(), "type": type_, "title": title, "body": body,
                "session_id": _wm_session_id()}
    if status:
        ev["status"] = status
    if reason:
        ev["reason"] = reason
    if related:
        ev["related_items"] = list(related)
    try:
        with open(_wm_journal(root), "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError as e:
        return f"journal write failed: {e}"
    return ""

def journal_read(root: str) -> List[dict]:
    import json as _json
    path = _wm_journal(root)
    if not os.path.isfile(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(_json.loads(line))
                    except _json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return out

def journal_mark_seen(root: str, items: List[str]) -> str:
    """Mark files/symbols as already deeply understood (hot set)."""
    import json as _json
    d = _wm_dir(root)
    _wm_gitignore(root)
    sp = os.path.join(d, "seen.json")
    seen = set()
    if os.path.isfile(sp):
        try:
            with open(sp, "r", encoding="utf-8") as fh:
                seen = set(_json.load(fh))
        except Exception:
            seen = set()
    before = len(seen)
    for it in items:
        seen.add(it.strip())
    try:
        with open(sp, "w", encoding="utf-8") as fh:
            _json.dump(sorted(seen), fh)
    except OSError:
        pass
    journal_append(root, "seen", f"marked {len(items)} item(s) as understood", related=list(items))
    added = len(seen) - before
    return f"marked {added} new item(s) as seen (hot set {len(seen)})"

def get_hot_set(root: str) -> List[str]:
    import json as _json
    sp = os.path.join(_wm_dir(root), "seen.json")
    if not os.path.isfile(sp):
        return []
    try:
        with open(sp, "r", encoding="utf-8") as fh:
            return sorted(_json.load(fh))
    except Exception:
        return []

def build_layered_summary(root: str, include_structural: bool = False) -> str:
    """Build the working-state packet: goal, status, key decisions, actions,
    open items, hot set. Regenerates summary.md. This is what an agent treats
    as the single source of truth after a compaction."""
    events = journal_read(root)
    hot = set(get_hot_set(root))
    if not events and not hot:
        return "# Working State\n\nNo active session journal yet. Start one with `--decide`/`--hypothesis`/`--checkpoint`.\n"
    goal = None
    decisions = []
    actions = []
    open_items = []
    last_cp = None
    for e in events:
        t = e.get("type")
        if t == "goal" and not goal:
            goal = e.get("title") or e.get("body")
        elif t == "decision":
            decisions.append(f"- [{e.get('status','accepted')}] {e.get('title')}"
                             + (f" (reason: {e.get('reason','')})" if e.get("reason") else ""))
        elif t == "action":
            actions.append(f"- {e.get('title') or e.get('body')}")
            for r in (e.get("related_items") or e.get("related") or []):
                hot.add(r)
        elif t in ("hypothesis", "open"):
            open_items.append(f"- {e.get('title')} [{e.get('status','open')}]")
        elif t == "checkpoint":
            last_cp = e
    lines = [f"# Working State — Session {events[-1].get('session_id','unknown') if events else 'new'}",
             "", "## Goal", goal or "(not explicitly set)", "",
             "## Status",
             (last_cp.get("body") if last_cp and last_cp.get("body") else "In progress"), ""]
    if decisions:
        lines += ["## Key Decisions"] + decisions[-10:] + [""]
    if actions:
        lines += ["## Actions Taken (most recent)"] + actions[-12:] + [""]
    if open_items:
        lines += ["## Open Items / Hypotheses"] + open_items[-8:] + [""]
    if hot:
        lines += ["## Hot Set (already deeply understood)"] + [f"- {x}" for x in sorted(hot)] + [""]
    if include_structural:
        # single Structural Focus section (deduped — render_working_state no
        # longer appends a second copy)
        hot_files = [x for x in sorted(hot) if x.endswith(
            (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".rb",
             ".c", ".h", ".cpp", ".cc", ".cs"))]
        if hot_files:
            lines += ["## Structural Focus",
                      "(files already deeply understood — the code map hot set)"] \
                     + [f"- {x}" for x in hot_files[:15]] + [""]
        else:
            lines += ["## Structural Focus",
                      "(the codeloom map for hot files is injected by --resume --full)", ""]
    summary = "\n".join(lines)
    try:
        with open(os.path.join(_wm_dir(root), "summary.md"), "w", encoding="utf-8") as fh:
            fh.write(summary)
    except OSError:
        pass
    return summary

def render_working_state(root: str, full: bool = False) -> str:
    """--working-state: the layered packet (goal, status, decisions, actions,
    open items, hot set). full=True adds exactly one Structural Focus section.
    Also the --resume --full path — a single source of truth after compaction."""
    return build_layered_summary(root, include_structural=full)

def wm_goal(root: str, goal: str) -> str:
    """Record the session goal into the working journal (and GOALS.md)."""
    _wm_dir(root); _wm_gitignore(root)
    journal_append(root, "goal", goal)
    md = os.path.join(_memory_dir(root), "GOALS.md")
    try:
        with open(md, "a", encoding="utf-8") as fh:
            fh.write(f"- {goal}\n")
    except OSError:
        pass
    return f"recorded goal: {goal}"

def wm_decide(root: str, title: str, reason: str = "", status: str = "accepted") -> str:
    """Record a decision (or rejection) into the journal + persistent decisions.md."""
    _wm_dir(root); _wm_gitignore(root)
    journal_append(root, "decision", title, reason=reason, status=status)
    # mirror into persistent memory decisions file
    md = os.path.join(_memory_dir(root), "DECISIONS.md")
    try:
        with open(md, "a", encoding="utf-8") as fh:
            fh.write(f"- [{status}] {title}" + (f" — {reason}" if reason else "") + "\n")
        # growth bound: rotate oldest entries into archive when over the cap
        memory_rotate(root, "DECISIONS.md")
    except OSError:
        pass
    return f"recorded decision [{status}]: {title}"

def wm_hypothesis(root: str, title: str, status: str = "open") -> str:
    journal_append(root, "hypothesis", title, status=status)
    return f"recorded hypothesis [{status}]: {title}"

def list_open_items(root: str) -> str:
    opens = [e for e in journal_read(root) if e.get("type") in ("hypothesis", "open")]
    if not opens:
        return "No open items or hypotheses.\n"
    return "# Open items / hypotheses\n" + "\n".join(
        f"- {e.get('title')} [{e.get('status','open')}]" for e in opens) + "\n"

def list_decisions(root: str) -> str:
    decs = [e for e in journal_read(root) if e.get("type") == "decision"]
    if not decs:
        return "No decisions recorded.\n"
    return "# Recorded decisions\n" + "\n".join(
        f"- [{e.get('status','accepted')}] {e.get('title')}" + (f" — {e.get('reason','')}" if e.get("reason") else "")
        for e in decs) + "\n"

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

def _core_unavailable_help(engine: str = "c") -> str:
    """Actionable message for 'accelerator unavailable' — names the actual
    blocker (no compiler vs build failure) per-OS instead of a bare 'run
    --build-core' that fails identically. The pure-Python engine always
    works, so this is an optimization hint, never a dead end."""
    import shutil
    if engine == "rust":
        src, compiler, install = "codeloom_core_rs.rs", "rustc", "rustup (https://rustup.rs)"
    else:
        src, compiler, install = "codeloom_core.c", "cc", (
            "Xcode Command Line Tools: xcode-select --install" if sys.platform == "darwin"
            else "build-essential: sudo apt install build-essential" if sys.platform.startswith("linux")
            else "MSVC Build Tools or LLVM clang")
    if not shutil.which(compiler):
        return ("{0} core unavailable: '{1}' not found on PATH.\n"
                "  Install {2}, then rerun — or just continue: the pure-Python\n"
                "  engine needs nothing and everything still works.\n"
                "  Source is committed at {3} (auditable, no downloads).").format(
                    engine, compiler, install, src)
    return ("{0} core build failed even though '{1}' exists.\n"
            "  Try manually: see scripts/release.sh notes or open an issue with\n"
            "  your compiler version. Everything still works via pure-Python.").format(engine, compiler)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="codeloom", description=__doc__)
    p.add_argument("root", nargs="?", default=".", help="repo path (default: cwd)")
    p.add_argument("--write", metavar="FILE", help="write map to FILE too")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--no-outline", action="store_true", help="skip per-file outlines (faster)")
    p.add_argument("--max-files", type=int, default=20000, help="cap traversal (default 20000; raise for 10M+ LOC monorepos)")
    p.add_argument("--parallel", action="store_true", help="parallelize file parsing for heavy ops (--graph/--calls/--impact/--cross/--deadcode/--index/--verify-edit) on large repos")
    p.add_argument("--graph", action="store_true", help="show Python import dependency graph")
    p.add_argument("--focus", metavar="MODULE", help="show deps/dependents of one module (with --graph)")
    p.add_argument("--calls", action="store_true", help="show function-level call graph (multi-language)")
    p.add_argument("--diff", action="store_true", help="show structure of files changed vs HEAD (git)")
    p.add_argument("--install-agents", action="store_true", help="write/update AGENTS.md with a codeloom block")
    p.add_argument("--install-agent", metavar="AGENT", help="print MCP config for an agent (claude|cursor|codex|gemini|opencode|cline|openhands|devin|hermes|aider|roo|windsurf|amazon-q|jetbrains|junie|kimi|qwen)")
    p.add_argument("--detect-agent", action="store_true", help="detect which coding agent's config dir is present")
    p.add_argument("--build-core", action="store_true", help="build the optional C accelerator (codeloom_core.c -> codeloom_core) if not present")
    p.add_argument("--cost", action="store_true", help="append token-cost estimate to output")
    p.add_argument("--session", action="store_true", help="log this invocation to the local session log (JSONL)")
    p.add_argument("--session-report", action="store_true", help="summarize the local session log (calls, tokens, cost)")
    p.add_argument("--impact", metavar="MODULE", help="predict blast radius of changing a module (single symbol only)")
    p.add_argument("--check-edit", metavar="SYMBOL", help="preflight: is it safe to edit this symbol? (terminal GO/STOP verdict)")
    p.add_argument("--check-delete", metavar="SYMBOL", help="preflight: is it safe to delete this symbol? (terminal GO/STOP verdict)")
    p.add_argument("--task", metavar="TEXT", help="rank modules relevant to a task description")
    p.add_argument("--plan", metavar="TEXT", help="emit a prioritized reading plan for a task")
    p.add_argument("--pack", metavar="TEXT", help="emit a single-shot context file for a task (reading order + impact + symbols)")
    p.add_argument("--cognitive-load", metavar="TOPIC", help="cognitive-load-aware task decomposition (intrinsic/extraneous/germane load)")
    p.add_argument("--resume", action="store_true", help="emit a compact structural snapshot to restore context after compaction")
    p.add_argument("--checkpoint", metavar="NOTE", nargs="?", const="", help="snapshot in-progress work (git diff + status note) to survive compaction")
    p.add_argument("--checkpoint-restore", action="store_true", help="read the last checkpoint back to resume in-progress work")
    p.add_argument("--loom", metavar="TEXT", help="intent engine: layered context for a task (overview->files->symbols->code->git->memory)")
    p.add_argument("--remember", metavar="NOTE", help="append a note to repository memory (default DECISIONS); use --section ARCHITECTURE|DECISIONS|PATTERNS|CONVENTIONS")
    p.add_argument("--section", metavar="NAME", default="DECISIONS", help="memory section for --remember")
    p.add_argument("--decide", metavar="TITLE", help="record a decision (use --reason; --status accepted/rejected)")
    p.add_argument("--reject", metavar="TITLE", help="record a rejected decision (with --reason)")
    p.add_argument("--hypothesis", metavar="TITLE", help="record an open hypothesis")
    p.add_argument("--reason", metavar="TEXT", default="", help="reason for --decide/--reject")
    p.add_argument("--status", metavar="STATUS", default="accepted", help="status for --decide (accepted/rejected/open)")
    p.add_argument("--list-decisions", action="store_true", help="list recorded decisions")
    p.add_argument("--list-open", action="store_true", help="list open items/hypotheses")
    p.add_argument("--lesson", metavar="TEXT", help="record a lesson/trap: something tried and why it failed")
    p.add_argument("--lessons", dest="lesson", metavar="TEXT", help="alias for --lesson (README documents --lessons)")
    p.add_argument("--supersede", nargs=2, metavar=("OLD", "NEW"), help="mark decision OLD as superseded by NEW")
    p.add_argument("--query-memory", metavar="QUERY", help="search long-term memory for 'what do we already know about X'")
    p.add_argument("--mark-seen", nargs="+", metavar="ITEM", help="mark files/symbols as already understood (hot set); a trailing positional is treated as the repo root")
    p.add_argument("--goal", metavar="TEXT", help="record the session goal (shown by --working-state and --resume)")
    p.add_argument("--memory", metavar="SYMBOL",
                   help="graph-linked memory retrieval: entries pinned to SYMBOL plus entries "
                        "reachable via its import/call-graph neighbors (retrieval; the legacy "
                        "--remember NOTE appends)")
    p.add_argument("--target-root", metavar="ROOT", default=None,
                   help="with --memory: build the call/import graph against ROOT instead of the "
                        "memory root (retrieval in one repo, graph expansion in another)")
    p.add_argument("--include-archive", action="store_true",
                   help="with --memory: also search archived memory-*.jsonl entries")
    p.add_argument("--memory-add", action="store_true",
                   help="generic typed memory write: --type TYPE --title T [--body B] "
                        "[--symbols S,S] [--priority N] appends to memory.jsonl")
    p.add_argument("--type", metavar="TYPE", default=None,
                   help="with --memory-add: decision|bug|question|architecture|api|constraint|"
                        "lesson|todo|warning|goal|hypothesis (default goal)")
    p.add_argument("--title", metavar="T", default=None, help="with --memory-add: title")
    p.add_argument("--body", metavar="B", default=None, help="with --memory-add: body text")
    p.add_argument("--symbols", metavar="S", default=None,
                   help="with --memory-add/--decide/--lesson/--adr: comma-separated "
                        "affected symbols/modules to link in the memory graph")
    p.add_argument("--priority", metavar="N", type=int, default=None,
                   help="with --memory-add: override the computed importance (0-100); "
                        "default: deterministic importance formula — " + MEMORY_IMPORTANCE_HELP)
    p.add_argument("--memory-stats", action="store_true",
                   help="typed memory statistics: counts by type/tier, bytes, archive size, "
                        "top-5 linked symbols")
    p.add_argument("--working-state", action="store_true", help="emit the layered working-state packet (goal, decisions, actions, open items, hot set)")
    p.add_argument("--adr", metavar="TITLE", help="write an Architectural Decision Record (use --context and --decision)")
    p.add_argument("--context", metavar="TEXT", help="context for --adr")
    p.add_argument("--decision", metavar="TEXT", help="decision for --adr")
    p.add_argument("--adr-status", metavar="STATUS", default="Accepted", help="status for --adr (default Accepted)")
    p.add_argument("--adr-list", action="store_true", help="list saved Architectural Decision Records")
    p.add_argument("--churn", action="store_true", help="git churn: most-edited files (instability signal)")
    p.add_argument("--cross", action="store_true", help="show cross-file call graph (resolved across modules)")
    p.add_argument("--cross-repo", nargs="+", metavar="PATH", help="build a combined knowledge graph across multiple repo roots")
    p.add_argument("--search", metavar="SYMBOL", help="search the symbol index (definitions + snippet)")
    p.add_argument("--embed-search", metavar="QUERY", help="fuzzy semantic symbol search (subword-hash embedding, zero-dep)")
    p.add_argument("--context-card", nargs="+", metavar="TARGET", help="batch triage card for multiple symbols in ONE call")
    p.add_argument("--answer", metavar="QUESTION", help="one-call cited answer with honest confidence")
    p.add_argument("--why", metavar="QUERY", help="decision lookup with evidence stamps ([exact]/[fuzzy]/[unverified])")
    p.add_argument("--health", action="store_true", help="code health screen: 0-10 per file, deterministic detectors, zero LLM")
    p.add_argument("--risk", metavar="REVSPEC", nargs="?", const="HEAD~1..HEAD", default=None,
                   help="change-risk report for a commit/range (default HEAD~1..HEAD)")
    p.add_argument("--pattern", metavar="PATTERN", help="structural AST search: $VAR captures, $$$REST captures lists (Python)")
    p.add_argument("--hybrid-search", metavar="QUERY", help="hybrid search: BM25 lexical + structural signals scored together")
    p.add_argument("--seen", action="store_true", help="session memory: report already-read files/symbols to avoid re-reading")
    p.add_argument("--usages", metavar="SYMBOL", help="find where a symbol is used (not just defined)")
    p.add_argument("--grep", metavar="QUERY", help="search file contents for a snippet (ranked + context)")
    p.add_argument("--grep-symbolic", metavar="QUERY", help="code-only grep: comments/strings excluded, matches ranked by enclosing symbol")
    p.add_argument("--read", metavar="SYMBOL", help="extract exact source of a function/class/method (token-efficient)")
    p.add_argument("--explain", metavar="SYMBOL", help="plain-English explanation of a symbol (AST + call graph)")
    p.add_argument("--precision", metavar="SYMBOL", help="graph precision report: call edges with confidence + class relationships")
    p.add_argument("--similar", metavar="SYMBOL", help="find structurally similar functions/classes (refactoring)")
    p.add_argument("--deadcode", action="store_true", help="find functions defined but never called")
    p.add_argument("--get-symbol", metavar="SYMBOL", help="token-counted symbol snippet (byte offsets + token estimate)")
    p.add_argument("--full", action="store_true", help="with --get-symbol, return the full source (default is summary); with --compact renderers, force the complete output")
    p.add_argument("--compact", action="store_true", default=None,
                   help="summary-first output for agent context: health top-5 worst files, map counts + entry points, calls/cross/graph top-5 rows with a '... N more' footer (~400-token cap). Default for MCP calls; CODELOOM_COMPACT=1 opts the CLI in")
    p.add_argument("--snippet", nargs=3, metavar=("PATH", "START", "END"), help="extract a byte-range snippet from a file")
    p.add_argument("--incremental", action="store_true", help="show files changed since last run (hash-based cache)")
    p.add_argument("--verify", metavar="FILE", help="print SHA-256 of a file (security check)")
    p.add_argument("--trace", nargs="+", metavar="CMD", help="run a command under sys.settrace, record runtime call edges")
    p.add_argument("--force", action="store_true", help="acknowledge --trace executes code (isolation warning)")
    p.add_argument("--install-grammars", action="store_true", help="install tree-sitter language grammars (opt-in precision)")
    p.add_argument("--auto-grammars", action="store_true", help="scan the repo and install grammars for its languages (beats the manual per-language step)")
    p.add_argument("--yes", action="store_true", help="with --install-grammars, actually run pip install")
    p.add_argument("--index", action="store_true", help="build + save a persistent byte-offset index (scale)")
    p.add_argument("--engine", choices=["py", "c", "rust"], default="py", help="scanning engine: py (pure-Python, default), c (compiled codeloom_core), or rust (compiled codeloom_core_rs, multi-threaded)")
    p.add_argument("--watch", action="store_true", help="incremental daemon-less refresh: re-index only changed files, keep lookups near-resident")
    p.add_argument("--watch-merge", action="store_true", dest="watch_merge", help="read watcher JSON lines from stdin, apply changed files to the persistent index live")
    p.add_argument("--watch-core", metavar="ROOT", help="native C file watcher (kqueue/inotify): print changed code files live")
    p.add_argument("--serve", metavar="ROOT", help="C-resident index server: answer symbol lookups sub-ms (no Python per query)")
    p.add_argument("--index-status", action="store_true", help="show persistent index status/freshness")
    p.add_argument("--query", metavar="Q", help="fast structural query against the persisted graph: callers X, callees X, dependents X, hubs, routes")
    p.add_argument("--framework", action="store_true", help="detect framework + surface routes/models/config/conventions")
    p.add_argument("--routes", action="store_true", help="extract HTTP routes: METHOD path -> handler (framework-aware)")
    p.add_argument("--channels", action="store_true", help="pub-sub / event channel map (EMITS -> LISTENS_ON)")
    p.add_argument("--export", metavar="FILE", help="export a portable graph snapshot (symbols + edges + routes + channels) to FILE")
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
    p.add_argument("--files", metavar="GLOB", help="find files by name/glob: '--files *.py' or '--files engine'")
    p.add_argument("--context-diff", nargs=2, metavar=("BASE", "HEAD"), help="branch-to-branch architecture-level diff (e.g. main HEAD)")
    p.add_argument("--verify-edit", metavar="ROOT", nargs="?", const=".",
                   help="verify-edit: post-edit graph-integrity oracle for working-tree changes (GO/STOP/CHECK verdict + named drivers)")
    p.add_argument("--severity", choices=["warn", "strict"], default="warn",
                   help="with --verify-edit: warn (default) prints STOP but exits 0; strict makes STOP exit 1")
    p.add_argument("--blindspot", action="store_true",
                   help="compare the --mark-seen hot set against the impact-derived read set (STOP: editing a file never read)")
    p.add_argument("--no-blindspot", action="store_true",
                   help="opt out of the blindspot check (prints a skip verdict)")
    p.add_argument("--savings-report", action="store_true",
                   help="token/seconds savings vs a grep+read baseline, from the local session log (all local)")
    p.add_argument("--since", type=int, metavar="DAYS", default=None,
                   help="with --savings-report: only rows newer than DAYS days")
    p.add_argument("--repo", metavar="PATH", default=None,
                   help="with --savings-report: aggregate per day for this repo path")
    p.add_argument("--memory-prune", action="store_true",
                   help="report (dry-run) old .codeloom-memory/archive entries; deletes ONLY with --delete")
    p.add_argument("--older-than", type=int, metavar="DAYS", default=90,
                   help="with --memory-prune: archive entries older than DAYS days (default 90)")
    p.add_argument("--delete", action="store_true",
                   help="with --memory-prune: actually delete the reported archive entries")
    p.add_argument("--eval", choices=["token", "compaction", "sealed", "vs-crg"], metavar="KIND",
                   help="run benchmarks/eval_runner.py (token|compaction|sealed|vs-crg); supports --json and --root")
    p.add_argument("--root", dest="eval_root", metavar="PATH", default=None,
                   help="repo root for --eval (default: positional root)")
    p.add_argument("--install-hook", action="store_true",
                   help="install .git/hooks/pre-commit that runs scripts/pre-commit-hook.sh (warn-only, exits 0)")
    p.add_argument("--uninstall-hook", action="store_true",
                   help="remove the codeloom-managed .git/hooks/pre-commit")
    p.add_argument("--version", action="version", version=f"codeloom {VERSION}")
    args = p.parse_args(argv)

    root = os.path.abspath(args.root)

    # --session: log this invocation to the local session log (every command)
    if args.session:
        import time as _time
        _t0 = _time.time()
        _real_stdout = sys.stdout
        _buf = io.StringIO()
        class _Tee:
            def write(self, s):
                _buf.write(s)
                _real_stdout.write(s)
            def flush(self):
                _real_stdout.flush()
        sys.stdout = _Tee()  # type: ignore[assignment]
        try:
            # strip --session so the recursive dispatch doesn't re-enter this
            # branch (infinite recursion)
            rc = main([a for a in (argv or []) if a != "--session"])
        finally:
            sys.stdout = _real_stdout  # type: ignore[assignment]
            log_session(root, " ".join(sys.argv[1:]), _buf.getvalue(),
                        elapsed=_time.time() - _t0)
            # weekly rotation of the session log into .codeloom-memory/archive
            session_rotate_weekly(root)
        return rc

    # --verify-edit: post-edit graph-integrity oracle (GO/STOP/CHECK verdict)
    if args.verify_edit:
        vroot = os.path.abspath(args.verify_edit)
        report = verify_edit(vroot, args.severity)
        print(report)
        if "VERDICT: ERROR" in report:
            # the oracle could not run (not a repo): exit non-zero so a
            # pre-commit hook / CI gate can't mistake it for a clean GO
            return 1
        if args.severity == "strict" and "VERDICT: STOP" in report:
            return 1
        return 0

    # --blindspot: hot set vs impact-derived read set (--no-blindspot opts out)
    if args.blindspot or args.no_blindspot:
        if args.no_blindspot:
            print(f"# codeloom --blindspot ({root})")
            print("VERDICT: SKIP — --no-blindspot opt-out (no blindspot check).")
        else:
            print(render_blindspot(root))
        return 0

    # --savings-report: token/time savings vs grep+read baseline (all local)
    if args.savings_report:
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "savings": render_savings_report(root, since_days=args.since,
                                                                repo_filter=args.repo),
                              "memory": render_memory_line(root)}, indent=2))
            return 0
        print(render_savings_report(root, since_days=args.since,
                                    repo_filter=args.repo))
        print(render_memory_line(root))
        return 0

    # --memory-prune: dry-run by default; deletes ONLY with --delete
    if args.memory_prune:
        print(render_memory_prune(root, older_than_days=args.older_than,
                                  do_delete=args.delete))
        return 0

    # --eval: plumb to benchmarks/eval_runner.py (argv-style, no shell)
    if args.eval:
        eroot = os.path.abspath(args.eval_root) if args.eval_root else root
        return run_eval(args.eval, eroot, as_json=args.json)

    # --install-hook / --uninstall-hook: pre-commit hook installer
    if args.install_hook:
        print(install_hook(root))
        return 0
    if args.uninstall_hook:
        print(uninstall_hook(root))
        return 0

    # --install-grammars: opt-in tree-sitter grammar installer
    if args.install_grammars:
        print(install_grammars(do_install=args.yes))
        return 0

    # --auto-grammars: beat-the-tradeoff — scan repo, install grammars for its langs
    # (explicit opt-in flag: force=True so the env default of 0 does not block it)
    if args.auto_grammars:
        print(_ensure_grammars_for_root(root, force=True))
        return 0

    # --index-status: show persistent index status
    if args.index_status:
        if args.json:
            data = load_persistent_index(root)
            if data is None:
                print(json.dumps({"version": VERSION, "root": root, "index": False}, indent=2))
            else:
                stale = 0
                for f, h in data.get("files", {}).items():
                    if _file_hash(f) != h:
                        stale += 1
                print(json.dumps({"version": VERSION, "root": root, "index": True,
                                  "symbols": len(data.get("symbols", {})),
                                  "stale_files": stale}, indent=2))
            return 0
        print(render_index_status(root))
        return 0

    # --query: fast structural query against the persisted graph (no re-walk)
    if args.query:
        if args.json:
            print(json.dumps({"version": VERSION, "query": args.query,
                              "result": render_query(root, args.query)}, indent=2))
            return 0
        print(render_query(root, args.query))
        return 0

    # --framework: detect the web/app framework and surface its structure
    if args.framework:
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "framework": render_framework(root, args.max_files)}, indent=2))
            return 0
        print(render_framework(root, args.max_files))
        return 0

    if args.routes:
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "routes": render_routes(root, args.max_files)}, indent=2))
            return 0
        print(render_routes(root, args.max_files))
        return 0

    if args.channels:
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "channels": render_channels(root, args.max_files)}, indent=2))
            return 0
        print(render_channels(root, args.max_files))
        return 0

    if args.export:
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "export": render_export(root, args.export, args.max_files)}, indent=2))
            return 0
        print(render_export(root, args.export, args.max_files))
        return 0

    if args.architecture:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "architecture": render_architecture(files, root)}, indent=2))
            return 0
        print(render_architecture(files, root))
        return 0

    if args.heatmap:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "heatmap": dependency_heatmap(files, root)}, indent=2))
            return 0
        print(dependency_heatmap(files, root))
        return 0

    if args.explain_topic:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "topic": args.explain_topic,
                              "explain": render_explain_topic(files, root, args.explain_topic, args.max_files)}, indent=2))
            return 0
        print(render_explain_topic(files, root, args.explain_topic, args.max_files))
        return 0

    if args.docs is not None:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "kind": args.docs,
                              "docs": render_auto_docs(files, root, args.docs)}, indent=2))
            return 0
        print(render_auto_docs(files, root, args.docs))
        return 0

    if args.refactor:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "symbol": args.refactor,
                              "refactor": render_refactor(files, root, args.refactor, args.max_files)}, indent=2))
            return 0
        print(render_refactor(files, root, args.refactor, args.max_files))
        return 0

    if args.rename:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "old": args.rename[0], "new": args.rename[1],
                              "rename": render_rename(files, root, args.rename[0], args.rename[1])}, indent=2))
            return 0
        print(render_rename(files, root, args.rename[0], args.rename[1]))
        return 0

    if args.ask is not None:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "task": args.ask,
                              "ask": render_ask(files, root, args.ask, args.max_files)}, indent=2))
            return 0
        print(render_ask(files, root, args.ask, args.max_files))
        return 0

    if args.bug_predict:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "bug_predict": render_bug_predict(files, root)}, indent=2))
            return 0
        print(render_bug_predict(files, root))
        return 0

    if args.timeline:
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "timeline": render_repo_timeline(root)}, indent=2))
            return 0
        print(render_repo_timeline(root))
        return 0

    if args.dedup:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "dedup": render_dedup(root, files)}, indent=2))
            return 0
        print(render_dedup(root, files))
        return 0

    if args.plugin_sdk:
        print(render_plugin_sdk(root))
        return 0

    if args.lsp:
        print(render_lsp(root))
        return 0

    if args.langs:
        if args.json:
            print(json.dumps({"version": VERSION, "langs": render_langs()}, indent=2))
            return 0
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
        if args.json:
            print(json.dumps({"version": VERSION, "query": args.find,
                              "find": render_find(files, root, args.find, args.max_files)}, indent=2))
            return 0
        print(render_find(files, root, args.find, args.max_files))
        return 0

    if args.files:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            import fnmatch
            q = args.files.strip()
            is_glob = any(ch in q for ch in "*?[")
            matched = []
            for f in files:
                rel = os.path.relpath(f, root).replace(os.sep, "/")
                if is_glob:
                    if fnmatch.fnmatch(base_slash(rel), q) or fnmatch.fnmatch(rel, q):
                        matched.append(rel)
                elif q.lower() in rel.lower():
                    matched.append(rel)
            print(json.dumps({"version": VERSION, "glob": args.files,
                              "files": sorted(matched), "count": len(matched)}, indent=2))
            return 0
        print(render_files(files, root, args.files))
        return 0

    if args.context_diff:
        if args.json:
            print(json.dumps({"version": VERSION, "base": args.context_diff[0], "head": args.context_diff[1],
                              "context_diff": render_context_diff(root, args.context_diff[0], args.context_diff[1])}, indent=2))
            return 0
        print(render_context_diff(root, args.context_diff[0], args.context_diff[1]))
        return 0

    # --session-report: summarize the local session log
    if args.session_report:
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "session_report": render_session_report(root)}, indent=2))
            return 0
        print(render_session_report(root))
        return 0

    # --index: build + save the persistent index
    if args.index:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        if args.engine in ("c", "rust"):
            core_finder = _find_core_engine(args.engine)
            if core_finder:
                files = _c_walk(root, engine=args.engine)  # fast walker
            if not files:
                _walk(root, rules, args.max_files, files)
            # integrated: ensure AST grammars for the repo's languages (default-on)
            _ensure_grammars_for_root(root)
        else:
            _walk(root, rules, args.max_files, files)
            _ensure_grammars_for_root(root)
        print(render_index(files, root, args.max_files, parallel=args.parallel, engine=args.engine))
        return 0

    if args.watch:
        # incremental daemon-less refresh: only changed files re-indexed.
        # Engine auto-selection: native C watcher (kqueue/inotify) >
        # codeloom_rs polling watcher > pure-Python one-shot. With --watch-merge
        # semantics folded in via --watch-live, output feeds the index.
        core = _find_core()
        rs_watch = _find_rs_watcher()
        if core or rs_watch:
            # one command = watcher piped straight into the index merger:
            #   <watcher> | codeloom --watch-merge ROOT
            watcher_cmd = [core, "--watch", root] if core else [rs_watch, "watch", root]
            engine_name = "native C (kqueue/inotify)" if core else "Rust polling"
            print("watch-live: %s engine feeding --watch-merge" % engine_name)
            import subprocess as _sp
            try:
                w = _sp.Popen(watcher_cmd, stdout=_sp.PIPE, text=True)
                m = _sp.Popen([sys.executable, os.path.abspath(__file__),
                               "--watch-merge", root], stdin=w.stdout)
                w.stdout.close()
                m.wait()
                w.wait()
            except KeyboardInterrupt:
                pass
            return 0
        print(refresh_index_incremental(root, args.max_files))
        return 0

    if getattr(args, "watch_merge", False):
        # Consume codeloom_rs watch / codeloom --watch-core JSON lines from
        # stdin and apply them to the persistent index incrementally — this
        # is the missing wire that makes the watcher feed the index live
        # (codegraph-style freshness, still daemon-less: run it as a sidecar,
        #   codeloom_rs watch ROOT | codeloom --watch-merge ROOT
        # ). Each line is one changed file; removed files drop from the index.
        pidx = load_persistent_index(root) or {"files": {}, "symbols": {}}
        n_applied = 0
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            f = rec.get("file")
            if not f:
                continue
            if rec.get("removed"):
                pidx["files"].pop(f, None)
                for name in [k for k, locs in pidx["symbols"].items()
                             if any(l.get("path") == f for l in locs)]:
                    pidx["symbols"][name] = [l for l in pidx["symbols"][name]
                                             if l.get("path") != f]
                    if not pidx["symbols"][name]:
                        del pidx["symbols"][name]
            else:
                mod = module_name_of(f, root)
                file_syms: dict = {}
                ext = os.path.splitext(f)[1].lower()
                # binary docs: (re)extract the sidecar so search sees new text
                if ext in DOC_EXTS:
                    side = ensure_doc_sidecar(f)
                    if side:
                        try:
                            pidx["files"][side] = (os.path.getmtime(side),
                                                   os.path.getsize(side))
                        except OSError:
                            pass
                if ext == ".py":
                    try:
                        _index_python_bytes(f, mod, file_syms)
                    except Exception:
                        pass
                elif ext in CALL_LANG_RULES:
                    _index_other_bytes(f, mod, ext, file_syms)
                # replace this file's contributions in the flat index
                for name in [k for k, locs in pidx["symbols"].items()
                             if any(l.get("path") == f for l in locs)]:
                    pidx["symbols"][name] = [l for l in pidx["symbols"][name]
                                             if l.get("path") != f]
                    if not pidx["symbols"][name]:
                        del pidx["symbols"][name]
                for name, locs in file_syms.items():
                    for l in locs:
                        l.pop("source", None)
                        l["sig"] = ""
                    pidx["symbols"].setdefault(name, []).extend(locs)
                try:
                    pidx["files"][f] = (os.path.getmtime(f), os.path.getsize(f))
                except OSError:
                    pass
            n_applied += 1
            if n_applied % 20 == 0:
                save_persistent_index(root, pidx["symbols"],
                                      list(pidx["files"].keys()), skip_json=False)
        if n_applied:
            save_persistent_index(root, pidx["symbols"], list(pidx["files"].keys()))
            print("watch-merge: applied %d change(s) to %s" % (n_applied, root))
        else:
            print("watch-merge: no changes received")
        return 0

    if args.watch_core:
        # native C file watcher (kqueue on macOS / inotify on Linux)
        core = _find_core()
        if not core:
            print(_core_unavailable_help("c"))
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
            print(_core_unavailable_help("c"))
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
            if args.json:
                print(json.dumps({"version": VERSION, "query": args.hybrid_search,
                                  "results": render_hybrid_search(hfiles, root, args.hybrid_search)}, indent=2))
                return 0
            print(render_hybrid_search(hfiles, root, args.hybrid_search))
            return 0
        lazy_locs = None
        if args.get_symbol and not args.full:
            lazy_locs = load_symbol_lazy(root, args.get_symbol)
            if lazy_locs:
                loc = lazy_locs[0]
                # freshness gate: the lazy store has no mtime validation of its
                # own, so stale byte offsets used to re-slice the EDITED file
                # into mid-token garbage while claiming exact bytes. If the
                # file changed since indexing, fall through to the full fresh
                # path below (same contract as --full).
                _lp = loc.get("path") or ""
                if _lp and not os.path.isabs(_lp):
                    _lp = os.path.join(root, _lp)
                _stale = True
                try:
                    if os.path.isfile(_lp):
                        pidx_chk = load_persistent_index(root)
                        _meta = (pidx_chk or {}).get("files", {}).get(_lp) if pidx_chk else None
                        if _meta is not None:
                            _stale = (os.path.getmtime(_lp), os.path.getsize(_lp)) != tuple(_meta)
                        else:
                            _stale = False  # no JSON index to compare against; keep fast path
                except OSError:
                    _stale = True
                if not _stale and loc.get("tokens", 0) <= ADAPTIVE_FULL_THRESHOLD:
                    src = _read_source_from_loc(loc, root)
                    if args.json:
                        print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                          "module": loc["module"], "line": loc["line"],
                                          "kind": loc["kind"], "source": src,
                                          "tokens": loc.get("tokens", 0)}, indent=2))
                        return 0
                    print(f"# get_symbol: {args.get_symbol}\n"
                          f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                          f"bytes {loc.get('start_byte',0)}-{loc.get('end_byte',0)}  "
                          f"~{loc.get('tokens',0)} tokens\n\n"
                          f"{src}\n")
                else:
                    sig = loc.get("sig") or args.get_symbol
                    if args.json:
                        print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                          "module": loc["module"], "line": loc["line"],
                                          "kind": loc["kind"], "summary": True,
                                          "signature": sig}, indent=2))
                        return 0
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
                        if args.json:
                            print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                              "module": loc["module"], "line": loc["line"],
                                              "kind": loc["kind"], "source": src,
                                              "tokens": loc.get("tokens", 0)}, indent=2))
                            return 0
                        print(f"# get_symbol: {args.get_symbol}\n"
                              f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                              f"bytes {loc.get('start_byte',0)}-{loc.get('end_byte',0)}  "
                              f"~{loc.get('tokens',0)} tokens\n\n"
                              f"{src}\n")
                        return 0
                    # adaptive: small symbols return the implementation by default
                    if loc.get("tokens", 0) <= ADAPTIVE_FULL_THRESHOLD:
                        src = _read_source_from_loc(loc, root)
                        if args.json:
                            print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                              "module": loc["module"], "line": loc["line"],
                                              "kind": loc["kind"], "source": src,
                                              "tokens": loc.get("tokens", 0)}, indent=2))
                            return 0
                        print(f"# get_symbol: {args.get_symbol}\n"
                              f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                              f"bytes {loc.get('start_byte',0)}-{loc.get('end_byte',0)}  "
                              f"~{loc.get('tokens',0)} tokens\n\n"
                              f"{src}\n")
                        return 0
                    # render summary directly from the index (no re-parse)
                    sig = loc.get("sig") or args.get_symbol
                    if args.json:
                        print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                          "module": loc["module"], "line": loc["line"],
                                          "kind": loc["kind"], "summary": True,
                                          "signature": sig}, indent=2))
                        return 0
                    print(f"# get_symbol: {args.get_symbol}\n"
                          f"{loc['module']}:{loc['line']}  [{loc['kind']}]  ~10 tokens (summary)\n\n"
                          f"Signature: {sig}\n"
                          f"Use `--get-symbol {args.get_symbol} --full` for the full source.\n")
                    return 0
                # symbol not in index — return fast, don't scan the whole repo
                if args.json:
                    print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                      "found": False}, indent=2))
                    return 0
                print(f"# get_symbol: {args.get_symbol}\nSymbol not found in index. "
                      f"Run `codeloom --index` to refresh, or use --full to scan.\n")
                return 0
            if args.search:
                if args.json:
                    results = search_symbols(pidx.get("symbols", {}), args.search)
                    print(json.dumps({"version": VERSION, "query": args.search,
                                      "results": results, "count": len(results)}, indent=2))
                    return 0
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
        if not sha256_file(args.verify):
            return 1  # missing/unreadable file must exit non-zero
        return 0

    # --snippet: byte-range extraction
    if args.snippet:
        path, start, end = args.snippet
        path = os.path.join(root, path) if not os.path.isabs(path) else path
        try:
            start, end = int(start), int(end)
        except ValueError:
            print("Error: START and END must be integers.")
            return 1
        if start < 0 or end < start:
            print(f"Error: invalid byte range {start}-{end} (need 0 <= START <= END).")
            return 1
        s = get_snippet_by_offset(path, start, end)
        if s is None:
            print(f"Error: cannot read {path}")
            return 1
        if s.get("invalid_range"):
            print(f"Error: byte range {start}-{end} exceeds file size.")
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
        valid = {"claude", "claude-code", "claudecode", "cursor", "codex",
                 "gemini", "gemini-cli", "opencode", "cline", "openhands",
                 "openhands-cli", "devin", "hermes", "hermes-agent", "aider",
                 "roo", "roo-code", "windsurf", "amazon-q", "amazonq",
                 "amazon", "q-developer", "jetbrains", "intellij", "goland",
                 "pycharm", "webstorm", "junie", "kimi", "kimi-cli", "qwen",
                 "qwen-code"}
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
        # check for an EXISTING FRESH binary directly — _find_core() auto-builds,
        # so calling it here would make --build-core lie "already built" right
        # after silently compiling the core itself. A STALE binary (source
        # newer) is rebuilt too — same mtime rule the finders apply.
        here = os.path.dirname(os.path.abspath(__file__))
        existing = [os.path.join(here, _CORE_NAME), os.path.join(here, _CORE_NAME + ".exe")]
        if any(os.path.isfile(c) and os.access(c, os.X_OK)
               and _binary_matches_platform(c)
               and not _core_is_stale(here, _CORE_NAME, c) for c in existing):
            print("C accelerator already built — nothing to do.")
            return 0
        import shutil as _shutil
        if not _shutil.which("cc"):
            print(_core_unavailable_help("c"))
            return 1
        print("building codeloom_core (cc -O3 codeloom_core.c)…")
        import subprocess as _sp
        out = os.path.join(here, "codeloom_core")
        try:
            r = _sp.run(["cc", "-O3", "-o", out, core_src], capture_output=True, text=True)
        except FileNotFoundError:
            print(_core_unavailable_help("c"))
            return 1
        if r.returncode == 0:
            print(f"built {out} — now use --index --engine c")
            return 0
        print(f"build failed (is cc/clang installed?): {r.stderr[:300]}")
        return 1

    # --diff: git-aware, structure of changed files
    if args.diff:
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "diff": render_diff(root, args.max_files)}, indent=2))
            return 0
        print(render_diff(root, args.max_files))
        return 0

    # --incremental: hash-based cache, no daemon
    if args.incremental:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        cache = load_cache(root)
        changed = changed_files(files, cache, root)
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "changed": [os.path.relpath(c, root) for c in sorted(changed)],
                              "count": len(changed)}, indent=2))
            update_cache(files, cache, root)
            save_cache(root, cache)
            return 0
        print(render_incremental(files, root, args.max_files))
        return 0

    # --cross / --search / --usages / --grep / --read / --explain / --similar / --deadcode / --get-symbol
    if args.cross or args.search or args.embed_search or args.context_card or args.answer \
       or args.why or args.health or args.risk is not None or args.pattern or args.grep_symbolic \
       or args.usages or args.grep or args.read \
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
                            if args.json:
                                print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                                  "module": loc["module"], "line": loc["line"],
                                                  "kind": loc["kind"], "source": loc.get("source", ""),
                                                  "tokens": loc.get("tokens", 0)}, indent=2))
                                return 0
                            print(f"# get_symbol: {args.get_symbol}\n"
                                  f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                                  f"bytes {loc['start_byte']}-{loc['end_byte']}  ~{loc['tokens']} tokens\n\n"
                                  f"{loc['source']}\n")
                        else:
                            if args.json:
                                print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                                  "module": loc["module"], "line": loc["line"],
                                                  "kind": loc["kind"], "summary": True,
                                                  "signature": loc.get("sig") or args.get_symbol}, indent=2))
                                return 0
                            print(render_get_symbol(files, root, args.get_symbol, summary=True))
                    elif use_summary:
                        if args.json:
                            print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                              "module": loc["module"], "line": loc["line"],
                                              "kind": loc["kind"], "summary": True,
                                              "signature": loc.get("sig") or args.get_symbol}, indent=2))
                            return 0
                        print(render_get_symbol(files, root, args.get_symbol, summary=True))
                    else:
                        if args.json:
                            print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                              "module": loc["module"], "line": loc["line"],
                                              "kind": loc["kind"], "source": loc.get("source", ""),
                                              "tokens": loc.get("tokens", 0)}, indent=2))
                            return 0
                        print(f"# get_symbol: {args.get_symbol}\n"
                              f"{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                              f"bytes {loc['start_byte']}-{loc['end_byte']}  ~{loc['tokens']} tokens\n\n"
                              f"{loc['source']}\n")
                    return 0
            # adaptive: pass summary=True so render_get_symbol flips to full
            # source only for small symbols (<= threshold)
            if args.json:
                loc = get_symbol(files, root, args.get_symbol)
                if loc is None:
                    print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                      "found": False}, indent=2))
                    return 0
                small = loc.get("tokens", 0) <= ADAPTIVE_FULL_THRESHOLD
                if (use_summary == "adaptive" and small) or use_summary is False:
                    print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                      "module": loc["module"], "line": loc["line"],
                                      "kind": loc["kind"], "source": loc.get("source", ""),
                                      "tokens": loc.get("tokens", 0)}, indent=2))
                else:
                    sig = _signature_shape(loc.get("source", ""))
                    print(json.dumps({"version": VERSION, "symbol": args.get_symbol,
                                      "module": loc["module"], "line": loc["line"],
                                      "kind": loc["kind"], "summary": True,
                                      "signature": sig[0] if sig else args.get_symbol}, indent=2))
                return 0
            print(render_get_symbol(files, root, args.get_symbol, summary=(use_summary is True or use_summary == "adaptive"), adaptive=(use_summary == "adaptive")))
            return 0

        if args.search:
            # use persistent index if present (fast path), else build fresh
            pidx = load_persistent_index(root)
            if pidx is not None:
                if args.json:
                    results = search_symbols(pidx.get("symbols", {}), args.search)
                    print(json.dumps({"version": VERSION, "query": args.search,
                                      "results": results, "count": len(results)}, indent=2))
                    return 0
                print(render_search(pidx.get("symbols", {}), args.search))
                return 0
            cache = load_cache(root)
            index = cached_symbols(files, root, cache)
            save_cache(root, cache)
            if args.json:
                results = search_symbols(index, args.search)
                print(json.dumps({"version": VERSION, "query": args.search,
                                  "results": results, "count": len(results)}, indent=2))
                return 0
            print(render_search(index, args.search))
            return 0

        if args.embed_search:
            if args.json:
                print(json.dumps({"version": VERSION, "query": args.embed_search,
                                  "results": render_embed_search(files, root, args.embed_search)}, indent=2))
                return 0
            print(render_embed_search(files, root, args.embed_search))
            return 0

        if args.context_card:
            if args.json:
                print(json.dumps({"version": VERSION, "targets": args.context_card,
                                  "card": render_context_card(files, root, args.context_card)}, indent=2))
                return 0
            print(render_context_card(files, root, args.context_card))
            return 0

        if args.answer:
            if args.json:
                print(json.dumps({"version": VERSION, "question": args.answer,
                                  "answer": render_answer(files, root, args.answer)}, indent=2))
                return 0
            print(render_answer(files, root, args.answer))
            return 0

        if args.why:
            if args.json:
                print(json.dumps({"version": VERSION, "query": args.why,
                                  "why": render_why(files, root, args.why)}, indent=2))
                return 0
            print(render_why(files, root, args.why))
            return 0

        if args.health:
            if args.json:
                index = build_byte_index(files, root)
                calls = build_call_graph_multi(files, root)
                result = compute_health(files, root, index, calls)
                s = result["_summary"]
                print(json.dumps({"version": VERSION, "root": root,
                                  "avg_score": s["avg_score"], "files_scanned": s["files_scanned"],
                                  "total_findings": s["total_findings"],
                                  "worst": s["worst"]}, indent=2))
                return 0
            print(render_health(files, root, compact=args.compact))
            return 0

        if args.risk is not None:
            # --risk [REVSPEC] with a bare directory argument: argparse binds
            # the trailing positional to REVSPEC (nargs='?'), which would
            # make `codeloom --risk /path/to/repo` report "unknown revspec
            # '/path/to/repo'" AND scan the wrong root (cwd). If the value is
            # a directory (repo root), it is really the positional root —
            # recover both: scan that root and use the default revspec.
            revspec = args.risk
            if os.path.isdir(os.path.abspath(revspec)):
                vroot = os.path.abspath(revspec)
                gi = os.path.join(vroot, ".gitignore")
                rules = parse_gitignore(gi) if os.path.isfile(gi) else []
                vfiles: List[str] = []
                _walk(vroot, rules, args.max_files, vfiles)
                if args.json:
                    print(json.dumps({"version": VERSION, "root": vroot, "revspec": "HEAD~1..HEAD",
                                      "risk": render_change_risk(vfiles, vroot, "HEAD~1..HEAD")}, indent=2))
                    return 0
                print(render_change_risk(vfiles, vroot, "HEAD~1..HEAD"))
            else:
                if args.json:
                    print(json.dumps({"version": VERSION, "root": root, "revspec": revspec,
                                      "risk": render_change_risk(files, root, revspec)}, indent=2))
                    return 0
                print(render_change_risk(files, root, revspec))
            return 0

        if args.pattern:
            if args.json:
                print(json.dumps({"version": VERSION, "pattern": args.pattern,
                                  "results": render_pattern_search(files, root, args.pattern)}, indent=2))
                return 0
            print(render_pattern_search(files, root, args.pattern))
            return 0

        if args.usages:
            if args.json:
                us = find_usages(files, root, args.usages)
                print(json.dumps({"version": VERSION, "symbol": args.usages,
                                  "usages": us, "count": len(us)}, indent=2))
                return 0
            print(render_usages(files, root, args.usages))
            return 0

        if args.grep:
            if args.json:
                print(json.dumps({"version": VERSION, "query": args.grep,
                                  "results": render_grep(files, root, args.grep)}, indent=2))
                return 0
            print(render_grep(files, root, args.grep))
            return 0

        if args.grep_symbolic:
            if args.json:
                print(json.dumps({"version": VERSION, "query": args.grep_symbolic,
                                  "results": render_grep_symbolic(files, root, args.grep_symbolic)}, indent=2))
                return 0
            print(render_grep_symbolic(files, root, args.grep_symbolic))
            return 0

        if args.read:
            if args.json:
                print(json.dumps({"version": VERSION, "symbol": args.read,
                                  "read": render_read(files, root, args.read)}, indent=2))
                return 0
            print(render_read(files, root, args.read))
            return 0

        if args.explain:
            if args.json:
                print(json.dumps({"version": VERSION, "symbol": args.explain,
                                  "explain": render_explain(files, root, args.explain)}, indent=2))
                return 0
            print(render_explain(files, root, args.explain))
            return 0

        if args.precision:
            if args.json:
                print(json.dumps({"version": VERSION, "symbol": args.precision,
                                  "precision": render_precision(files, root, args.precision)}, indent=2))
                return 0
            print(render_precision(files, root, args.precision))
            return 0

        if args.similar:
            if args.json:
                print(json.dumps({"version": VERSION, "symbol": args.similar,
                                  "similar": render_similar(files, root, args.similar)}, indent=2))
                return 0
            print(render_similar(files, root, args.similar))
            return 0

        if args.deadcode:
            pidx = ensure_fresh_index(root, args.max_files)
            # load call edges from the knowledge graph if present (no re-parse)
            kg_calls = None
            if pidx and pidx.get("kg"):
                kg_calls = pidx["kg"].get("calls")
            if args.json:
                dead = dead_code(files, root, index=pidx, calls=kg_calls)
                print(json.dumps({"version": VERSION, "root": root,
                                  "dead_symbols": [{"symbol": d["symbol"]} for d in dead],
                                  "count": len(dead)}, indent=2))
                return 0
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
            if args.json:
                print(json.dumps({"version": VERSION, "root": root,
                                  "modules": calls,
                                  "module_count": len(calls)}, indent=2, default=list))
                return 0
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
            print(render_cross_calls(calls, root, start=focus, compact=args.compact))
            return 0

    # --impact / --task / --plan / --pack: task-aware intelligence
    if args.impact or args.task or args.plan or args.pack or args.cognitive_load or args.check_edit or args.check_delete or args.resume or args.loom or args.remember or args.checkpoint is not None or args.checkpoint_restore or args.adr or args.adr_list or args.decide or args.reject or args.hypothesis or args.list_decisions or args.list_open or args.mark_seen or args.working_state or args.lesson or args.supersede or args.query_memory or args.goal or args.memory or args.memory_add or args.memory_stats:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)

        if args.impact:
            graph = build_graph_multi(files, root, parallel=True)
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
            if args.json:
                print(json.dumps({"version": VERSION, "root": root, "target": target,
                                  "impact": render_impact(graph, root, target)}, indent=2))
                return 0
            print(render_impact(graph, root, target))
            return 0

        if args.check_edit:
            if args.json:
                print(json.dumps({"version": VERSION, "target": args.check_edit,
                                  "check": preflight_check(files, root, args.check_edit, "edit")}, indent=2))
                return 0
            print(preflight_check(files, root, args.check_edit, "edit"))
            return 0

        if args.check_delete:
            if args.json:
                print(json.dumps({"version": VERSION, "target": args.check_delete,
                                  "check": preflight_check(files, root, args.check_delete, "delete")}, indent=2))
                return 0
            print(preflight_check(files, root, args.check_delete, "delete"))
            return 0

        # --checkpoint is checked BEFORE --task/--impact/--plan/--pack so the
        # documented combo `--checkpoint --task fix login bug` writes the
        # checkpoint file even when a task string is also passed.
        if args.checkpoint is not None:
            note = args.checkpoint or ""
            if args.task:
                note = (note + " " if note else "") + args.task
            print(render_checkpoint(root, note or None))
            return 0

        if args.task:
            if args.json:
                ranked = task_structural_rank(files, root, args.task)
                print(json.dumps({"version": VERSION, "task": args.task,
                                  "modules": ranked, "count": len(ranked)}, indent=2))
                return 0
            print(render_task(files, root, args.task))
            return 0

        if args.plan:
            if args.json:
                print(json.dumps({"version": VERSION, "task": args.plan,
                                  "plan": build_plan(files, root, args.plan)}, indent=2))
                return 0
            print(build_plan(files, root, args.plan))
            return 0

        if args.pack:
            if args.json:
                print(json.dumps({"version": VERSION, "task": args.pack,
                                  "pack": render_pack(files, root, args.pack)}, indent=2))
                return 0
            print(render_pack(files, root, args.pack))
            return 0

        if args.cognitive_load:
            if args.json:
                print(json.dumps({"version": VERSION, "topic": args.cognitive_load,
                                  "cognitive_load": render_cognitive_load(files, root, args.cognitive_load)}, indent=2))
                return 0
            print(render_cognitive_load(files, root, args.cognitive_load))
            return 0

        if args.resume:
            if args.json:
                print(json.dumps({"version": VERSION, "root": root,
                                  "resume": render_resume(files, root, args.max_files, full=args.full)}, indent=2))
                return 0
            print(render_resume(files, root, args.max_files, full=args.full))
            return 0

        if args.checkpoint_restore:
            if args.json:
                print(json.dumps({"version": VERSION, "root": root,
                                  "checkpoint": render_checkpoint_restore(root)}, indent=2))
                return 0
            print(render_checkpoint_restore(root))
            return 0

        if args.loom:
            if args.json:
                print(json.dumps({"version": VERSION, "task": args.loom,
                                  "loom": render_loom_context(files, root, args.loom, args.max_files)}, indent=2))
                return 0
            print(render_loom_context(files, root, args.loom, args.max_files))
            return 0

        # --memory <symbol> FIRST: graph-linked memory retrieval (never a
        # write). The legacy --remember NOTE keeps appending; --remember with
        # a symbol-resolvable value dispatches here too (graph retrieval).
        if args.memory:
            if args.json:
                print(json.dumps({"version": VERSION, "symbol": args.memory,
                                  "memory": render_memory_graph(files, root, args.memory,
                                                                target_root=args.target_root,
                                                                include_archive=args.include_archive,
                                                                max_files=args.max_files)}, indent=2))
                return 0
            print(render_memory_graph(files, root, args.memory,
                                      target_root=args.target_root,
                                      include_archive=args.include_archive,
                                      max_files=args.max_files))
            return 0

        # --memory-add: generic typed write (needs --title)
        if args.memory_add:
            if not args.title:
                print("--memory-add requires --title (use "
                      "--type TYPE --title T [--body B] [--symbols S,S])",
                      file=sys.stderr)
                return 1
            e = memory_append(root, args.type or "goal", args.title,
                              body=args.body or "",
                              symbols=args.symbols.split(",")
                              if args.symbols else None,
                              priority=args.priority, created="memory")
            print("added [%s] %s — importance: %d, tier: %s"
                  % (e["type"], e["title"], e["importance"], e["tier"]))
            return 0

        # --memory-stats: counts by type/tier, bytes, top linked symbols
        if args.memory_stats:
            if args.json:
                print(json.dumps({"version": VERSION, "root": root,
                                  "memory_stats": render_memory_stats(root)}, indent=2))
                return 0
            print(render_memory_stats(root))
            return 0

        if args.remember:
            # legacy --remember NOTE appends to a section (default DECISIONS);
            # a symbol-resolvable value switches to graph retrieval
            if memory_symbol_resolve(files, root, args.remember):
                if args.json:
                    print(json.dumps({"version": VERSION, "note": args.remember,
                                      "memory": render_memory_graph(files, root, args.remember,
                                                                    target_root=args.target_root,
                                                                    include_archive=args.include_archive,
                                                                    max_files=args.max_files)}, indent=2))
                    return 0
                print(render_memory_graph(files, root, args.remember,
                                          target_root=args.target_root,
                                          include_archive=args.include_archive,
                                          max_files=args.max_files))
                return 0
            if args.json:
                print(json.dumps({"version": VERSION, "note": args.remember,
                                  "remember": memory_remember(root, args.section, args.remember)}, indent=2))
                return 0
            print(memory_remember(root, args.section, args.remember))
            return 0

        if args.goal:
            print(wm_goal(root, args.goal))
            # typed mirror: goal entries append to memory.jsonl too
            e = memory_append(root, "goal", args.goal, created="goal")
            if e.get("error"):
                print("memory: jsonl mirror FAILED (%s); the markdown "
                      "goal was still recorded" % e["error"], file=sys.stderr)
            else:
                print("memory: [goal] importance: %d, tier: %s"
                      % (e["importance"], e["tier"]))
            return 0

        if args.decide:
            e = wm_decide(root, args.decide, args.reason, args.status)
            print(e)
            # typed mirror: decisions append to memory.jsonl (backward compat)
            me = memory_append(root, "decision", args.decide,
                               body=("status: %s" % args.status
                                     if args.status != "accepted" else ""),
                               reason=args.reason,
                               symbols=args.symbols.split(",")
                               if args.symbols else None,
                               priority=args.priority,
                               created="decide")
            if me.get("error"):
                print("memory: jsonl mirror FAILED (%s); the markdown "
                      "decision was still recorded" % me["error"],
                      file=sys.stderr)
            else:
                print("memory: importance: %d, tier: %s"
                      % (me["importance"], me["tier"]))
            return 0

        if args.reject:
            # --reject is --decide with status=rejected: keep the markdown
            # journal entry AND the typed jsonl mirror (dual-write parity
            # with --decide — a rejected decision is still a decision).
            print(wm_decide(root, args.reject, args.reason, "rejected"))
            me = memory_append(root, "decision", args.reject,
                               body="status: rejected",
                               reason=args.reason,
                               symbols=args.symbols.split(",")
                               if args.symbols else None,
                               priority=args.priority,
                               created="decide")
            if me.get("error"):
                print("memory: jsonl mirror FAILED (%s); the markdown "
                      "rejection remains recorded" % me["error"],
                      file=sys.stderr)
            else:
                print("memory: importance: %d, tier: %s"
                      % (me["importance"], me["tier"]))
            return 0

        if args.hypothesis:
            print(wm_hypothesis(root, args.hypothesis, "open"))
            # typed hypothesis: append to memory.jsonl too
            me = memory_append(root, "hypothesis", args.hypothesis,
                               body="status: open", created="hypothesis")
            if me.get("error"):
                print("memory: jsonl mirror FAILED (%s); the markdown "
                      "hypothesis was still recorded" % me["error"],
                      file=sys.stderr)
            else:
                print("memory: importance: %d, tier: %s"
                      % (me["importance"], me["tier"]))
            return 0

        if args.list_decisions:
            print(list_decisions(root))
            return 0

        if args.list_open:
            print(list_open_items(root))
            return 0

        if args.lesson:
            print(memory_lesson(root, args.lesson))
            # typed mirror: lessons append to memory.jsonl too
            me = memory_append(root, "lesson", args.lesson,
                               symbols=args.symbols.split(",")
                               if args.symbols else None,
                               priority=args.priority,
                               created="lesson")
            if me.get("error"):
                print("memory: jsonl mirror FAILED (%s); the markdown "
                      "lesson was still recorded" % me["error"],
                      file=sys.stderr)
            else:
                print("memory: importance: %d, tier: %s"
                      % (me["importance"], me["tier"]))
            return 0

        if args.supersede:
            # --supersede records a decision-replacement fact: keep the
            # markdown record AND append a typed jsonl entry (dual-write
            # parity — supersession is a decision-level event too).
            print(memory_supersede(root, args.supersede[0], args.supersede[1]))
            me = memory_append(root, "decision",
                               "%s superseded by %s" % (args.supersede[0],
                                                        args.supersede[1]),
                               body="supersedes %s" % args.supersede[0],
                               symbols=args.symbols.split(",")
                               if args.symbols else None,
                               created="decide")
            if me.get("error"):
                print("memory: jsonl mirror FAILED (%s); the markdown "
                      "supersede record was still written" % me["error"],
                      file=sys.stderr)
            else:
                print("memory: importance: %d, tier: %s"
                      % (me["importance"], me["tier"]))
            return 0

        if args.query_memory:
            if args.json:
                print(json.dumps({"version": VERSION, "query": args.query_memory,
                                  "memory": memory_query(root, args.query_memory)}, indent=2))
                return 0
            print(memory_query(root, args.query_memory))
            return 0

        if args.mark_seen:
            items = list(args.mark_seen)
            mr_root = root
            # positional-ambiguity fix: a trailing positional that resolves to
            # an existing directory is treated as the repo root, not as a seen
            # item — so `--mark-seen core/auth.py <repo>` journals into <repo>
            # instead of the CWD.
            if len(items) > 1:
                cand = items[-1]
                cand_abs = os.path.abspath(cand) if not os.path.isabs(cand) else cand
                if os.path.isdir(cand_abs):
                    mr_root = cand_abs
                    items = items[:-1]
            if args.json:
                print(json.dumps({"version": VERSION, "root": mr_root,
                                  "marked": items,
                                  "result": journal_mark_seen(mr_root, items)}, indent=2))
                return 0
            print(journal_mark_seen(mr_root, items))
            return 0

        if args.working_state:
            if args.json:
                print(json.dumps({"version": VERSION, "root": root,
                                  "working_state": render_working_state(root, full=True)}, indent=2))
                return 0
            print(render_working_state(root, full=True))
            return 0

        if args.adr_list:
            if args.json:
                print(json.dumps({"version": VERSION, "root": root,
                                  "adrs": render_adr_list(root)}, indent=2))
                return 0
            print(render_adr_list(root))
            return 0

        if args.adr:
            print(render_adr(root, args.adr, args.context or "", args.decision or "",
                             args.adr_status))
            # typed mirror: ADRs append to memory.jsonl too (type architecture)
            me = memory_append(root, "architecture", args.adr,
                               body=(args.decision or ""),
                               reason=(args.context or ""),
                               symbols=args.symbols.split(",")
                               if args.symbols else None,
                               priority=args.priority,
                               created="adr")
            if me.get("error"):
                print("memory: jsonl mirror FAILED (%s); the markdown "
                      "ADR was still written" % me["error"], file=sys.stderr)
            else:
                print("memory: importance: %d, tier: %s"
                      % (me["importance"], me["tier"]))
            return 0

    if args.churn:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "churn": git_churn(root, files)}, indent=2))
            return 0
        print(git_churn(root, files))
        return 0

    if args.seen:
        # session memory: report already-read files/symbols to avoid re-reading
        if args.json:
            print(json.dumps({"version": VERSION, "root": root,
                              "seen": render_seen(root)}, indent=2))
            return 0
        print(render_seen(root))
        return 0

    # Cross-repo mode: one graph across multiple repo roots
    if args.cross_repo:
        if args.json:
            print(json.dumps({"version": VERSION, "roots": args.cross_repo,
                              "cross_repo": render_cross_repo(args.cross_repo, args.max_files, compact=args.compact)}, indent=2))
            return 0
        print(render_cross_repo(args.cross_repo, args.max_files, compact=args.compact))
        return 0

    # Call-graph mode (multi-language)
    if args.graph or args.calls:
        gi = os.path.join(root, ".gitignore")
        rules = parse_gitignore(gi) if os.path.isfile(gi) else []
        files: List[str] = []
        _walk(root, rules, args.max_files, files)
        if args.calls:
            calls = build_call_graph_multi(files, root, parallel=True)
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
            if args.json:
                print(json.dumps({"version": VERSION, "root": root, "kind": "calls",
                                  "focus": focus, "modules": calls,
                                  "module_count": len(calls)}, indent=2, default=list))
                return 0
            text = render_calls(calls, root, start=focus, compact=args.compact)
            if args.cost:
                text += render_token_report({}, text)
            print(text)
            return 0
        graph = build_graph_multi(files, root, parallel=True)
        if args.focus:
            # accept file path, directory (package), or dotted module name
            focus = args.focus
            focus_path = os.path.join(root, focus) if not os.path.isabs(focus) else focus
            if os.path.isdir(focus_path):
                focus = module_name_of(focus_path, root)
            elif focus.endswith(".py") or os.path.isfile(focus_path) or os.path.isfile(focus_path + ".py"):
                focus = module_name_of(focus_path + (".py" if os.path.isfile(focus_path + ".py") else ""), root)
            if focus not in graph:
                # a real file that's absent from the graph is a module with no
                # edges (imports nothing, nothing imports it) — focus it anyway
                # instead of reporting "module not found"
                fpath = os.path.join(root, focus.replace(".", os.sep) + ".py")
                if not (os.path.isfile(focus_path) or os.path.isfile(focus_path + ".py")
                        or os.path.isfile(fpath)):
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
            if args.json:
                print(json.dumps({"version": VERSION, "root": root, "kind": "graph",
                                  "focus": focus, "modules": graph,
                                  "module_count": len(graph)}, indent=2, default=list))
                return 0
            text = render_graph_multi(graph, root, start=focus, compact=args.compact)
        else:
            if args.json:
                print(json.dumps({"version": VERSION, "root": root, "kind": "graph",
                                  "modules": graph, "module_count": len(graph)}, indent=2, default=list))
                return 0
            text = render_graph_multi(graph, root, compact=args.compact)
        if args.cost:
            text += render_token_report({}, text)
        print(text)
        return 0

    m = build_map(args.root, not args.no_outline, args.max_files)

    payload = None
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
        text = render_text(m, compact=args.compact)
        if args.cost:
            text += render_token_report(m, text)
        print(text)
    if args.write:
        # honor --write in BOTH modes: --json used to silently drop the
        # write (printed JSON, wrote nothing, exit 0) — CI breakage
        try:
            if args.json and payload is not None:
                out = json.dumps(payload, indent=2)
            else:
                out = render_text(m, compact=args.compact)
            with open(args.write, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"\n[written to {args.write}]", file=sys.stderr)
        except OSError as e:
            print(f"error: cannot write {args.write}: {e}", file=sys.stderr)
            return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())
