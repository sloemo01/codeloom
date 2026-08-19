#!/usr/bin/env python3
"""
codemap — a map of your codebase, in one command, for AI agents.

Single-file, zero-dependency, no-daemon. Walks a project, respects .gitignore,
and emits a compact "table of contents" (folder tree + per-module one-liners +
entry points) that Claude Code, Cursor, Codex, Gemini or any coding agent can
read in a second to build a mental model BEFORE burning tokens on grep/read.

Usage:
    codemap                       # map current directory -> stdout
    codemap /path/to/repo         # map a specific repo
    codemap --write MAP.md        # also write to MAP.md (gitignored-friendly)
    codemap --json                # machine-readable JSON for tooling
    codemap --max-files 2000      # cap traversal (default 5000)

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

VERSION = "0.3.0"

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
# .gitignore parsing (best-effort, pattern-subset; no dep)
# --------------------------------------------------------------------------- #

def parse_gitignore(path: str) -> Tuple[List[str], List[str]]:
    """Return (ignore_globs, ignore_dirs). Handles common patterns."""
    globs: List[str] = []
    dirs: Set[str] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # strip trailing comment (crude)
                if line.endswith("/"):
                    dirs.add(line.rstrip("/").lstrip("/"))
                    continue
                globs.append(line.lstrip("/"))
    except OSError:
        pass
    return globs, sorted(dirs)

def is_ignored(path: str, globs: List[str], ignore_dirs: Set[str]) -> bool:
    """Simple gitignore check: basename, dir, and common glob patterns."""
    name = os.path.basename(path)
    for d in ignore_dirs:
        if d in path.split(os.sep):
            return True
    for g in globs:
        g = g.rstrip("/")
        if g.startswith("*") and name.endswith(g.lstrip("*")):
            return True
        if g.endswith("*") and name.startswith(g.rstrip("*")):
            return True
        if g == name:
            return True
        # pattern like node_modules or *.pyc already caught above
    return False

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

def _walk(root: str, globs: List[str], ignore_dirs: Set[str],
          max_files: int, files: List[str]) -> None:
    """Collect file paths, respecting ignores, capped by max_files."""
    if len(files) >= max_files:
        return
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return
    for e in entries:
        if e.startswith(".") and e not in (".gitignore", ".env.example"):
            continue
        full = os.path.join(root, e)
        if is_ignored(full, globs, ignore_dirs):
            continue
        if os.path.isdir(full):
            _walk(full, globs, ignore_dirs, max_files, files)
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
    globs, ignore_dirs = parse_gitignore(gi) if os.path.isfile(gi) else ([], [])
    files: List[str] = []
    _walk(root, globs, ignore_dirs, max_files, files)
    tree = build_tree(files, root, want_outline)
    return {
        "root": root,
        "file_count": len(files),
        "entry_points": entry_points(files),
        "tree": tree,
    }

# --------------------------------------------------------------------------- #
# Call-graph intelligence (Python stdlib `ast`, zero deps)
# --------------------------------------------------------------------------- #

def module_name_of(path: str, root: str) -> str:
    """Map a file path to its dotted module name, e.g. src/core/engine.py -> src.core.engine."""
    rel = os.path.relpath(path, root)
    if rel.endswith(".py"):
        rel = rel[:-3]
    elif rel.endswith("/__init__"):
        rel = rel[:-9]
    if rel.endswith("__init__"):
        rel = rel[:-9]
    return rel.replace(os.sep, ".")

def _resolve_import(target: str, importer_mod: str, root: str, module_map: dict) -> Optional[str]:
    """Resolve an imported module name to an existing local module, or None.
    Handles absolute, relative (from .x / from ..x), and the common case where
    the import is relative to the source root (e.g. 'core.engine' -> 'src.core.engine').
    Strategy:
      1. exact match
      2. drop trailing segments (pkg.module -> pkg)
      3. suffix match against known modules (core.engine matches src.core.engine)
    """
    if target.startswith("."):
        # relative import — handled by caller via parse_module; skip here
        return None
    cands = [target]
    parts = target.split(".")
    for i in range(len(parts) - 1, 0, -1):
        cands.append(".".join(parts[:i]))
    for c in cands:
        if c in module_map:
            return c
    # suffix match: does any local module end with '.target' (or equal it)?
    tgt_segs = target.split(".")
    best = None
    for mod in module_map:
        msegs = mod.split(".")
        if len(msegs) >= len(tgt_segs) and msegs[-len(tgt_segs):] == tgt_segs:
            # prefer shallowest (fewest segments above the match)
            if best is None or len(msegs) < len(best.split(".")):
                best = mod
    return best

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

def render_text(m: dict) -> str:
    ep = m["entry_points"]
    buf = io.StringIO()
    buf.write(f"# codemap — {m['root']}\n")
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
    p = argparse.ArgumentParser(prog="codemap", description=__doc__)
    p.add_argument("root", nargs="?", default=".", help="repo path (default: cwd)")
    p.add_argument("--write", metavar="FILE", help="write map to FILE too")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--no-outline", action="store_true", help="skip per-file outlines (faster)")
    p.add_argument("--max-files", type=int, default=5000, help="cap traversal (default 5000)")
    p.add_argument("--graph", action="store_true", help="show Python import dependency graph")
    p.add_argument("--focus", metavar="MODULE", help="show deps/dependents of one module (with --graph)")
    p.add_argument("--calls", action="store_true", help="show function-level call graph (Python)")
    p.add_argument("--version", action="version", version=f"codemap {VERSION}")
    args = p.parse_args(argv)

    # Call-graph mode (Python only)
    if args.graph or args.calls:
        root = os.path.abspath(args.root)
        gi = os.path.join(root, ".gitignore")
        globs, ignore_dirs = parse_gitignore(gi) if os.path.isfile(gi) else ([], [])
        files: List[str] = []
        _walk(root, globs, ignore_dirs, args.max_files, files)
        if args.calls:
            calls = build_call_graph(files, root)
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
            print(render_calls(calls, root, start=focus))
            return 0
        graph = build_graph(files, root)
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
            text = render_graph(graph, root, start=focus)
        else:
            text = render_graph(graph, root)
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
        print(text)
        if args.write:
            with open(args.write, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"\n[written to {args.write}]", file=sys.stderr)

    return 0

if __name__ == "__main__":
    sys.exit(main())
