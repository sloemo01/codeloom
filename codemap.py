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
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

VERSION = "0.1.0"

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
    p.add_argument("--version", action="version", version=f"codemap {VERSION}")
    args = p.parse_args(argv)

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
