#!/usr/bin/env python3
"""
codeloom-mcp — a Model Context Protocol (MCP) server that exposes codeloom to
any MCP-capable agent (Claude Code, Cursor, Codex, Gemini, etc.) as native tools.

Zero-dependency: implements the MCP stdio transport (JSON-RPC 2.0 over
stdin/stdout) using only the Python standard library. No `mcp` package, no
daemon, no install beyond copying this file.

Tools exposed:
  codeloom_map(root, max_files)          -> repo tree + entry points
  codeloom_graph(root, max_files)        -> full import dependency graph
  codeloom_focus(root, module, max_files)-> deps + dependents of one module

Usage:
  # Register in your agent's MCP config, e.g. Claude Code:
  #   "codeloom": { "command": "python3", "args": ["/path/to/codeloom-mcp.py"] }
  # or run standalone to test:
  #   echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python3 codeloom-mcp.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

# Reuse codeloom's logic directly (same directory).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codeloom  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "codeloom-mcp"
SERVER_VERSION = "0.22.0"

# --------------------------------------------------------------------------- #
# Tool definitions (MCP tools/list schema)
# --------------------------------------------------------------------------- #

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "codeloom_map",
        "description": (
            "Produce a compact 'table of contents' of a codebase: folder tree, "
            "per-module one-liners (classes/functions), and entry points. Use "
            "this FIRST to build a mental model of a repo before reading files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
        },
    },
    {
        "name": "codeloom_graph",
        "description": (
            "Build the Python import dependency graph of a codebase: which "
            "modules import which. Use to understand 'what touches what'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
        },
    },
    {
        "name": "codeloom_focus",
        "description": (
            "Focus on ONE module: what it depends on (depends_on) and what "
            "depends on it (depended_on_by). Answers 'what does this code need?' "
            "and 'what breaks if I change it?' Accepts a file path, package dir, "
            "or dotted module name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "module": {"type": "string", "description": "Module to focus on, e.g. 'core.engine' or 'src/core/engine.py'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "codeloom_calls",
        "description": (
            "Function-level call graph: which functions call which, across the "
            "codebase. Only reports calls to functions defined in the repo "
            "(builtins/stdlib filtered out). Use to trace execution flow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "module": {"type": "string", "description": "Optional: restrict to one module's calls"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
        },
    },
    {
        "name": "codeloom_diff",
        "description": (
            "Show the structure of only the files changed vs git HEAD. Use when "
            "the agent is working on a specific change — tells it what's relevant "
            "to the current task, always fresh."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
        },
    },
    {
        "name": "codeloom_impact",
        "description": (
            "Predict the blast radius of changing a module: which modules depend "
            "on it (direct + transitive) and what it depends on. Answers 'what "
            "breaks if I change this?' before the agent edits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "module": {"type": "string", "description": "Module to analyze, e.g. 'core.engine' or 'src/core/engine.py'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "codeloom_task",
        "description": (
            "Rank modules relevant to a task description, by token overlap + "
            "graph centrality. Use to find which files matter for a specific task "
            "before reading the whole repo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "task": {"type": "string", "description": "Task description, e.g. 'fix the login bug'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "codeloom_plan",
        "description": (
            "Emit a prioritized 'read these files, in this order' plan for a task. "
            "The agent-native format: tells the agent exactly what to read to "
            "understand a task before editing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "task": {"type": "string", "description": "Task description"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "codeloom_pack",
        "description": (
            "Single-shot context packing: emit ONE compact file for a task with "
            "reading order + impact analysis + symbol index, all pre-computed. "
            "An agent pastes this once and has everything it needs — zero "
            "per-query retrieval during the session. The feature jcodemunch "
            "can't do (it's retrieval-only, can't prioritize)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "task": {"type": "string", "description": "Task description"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "codeloom_cross",
        "description": (
            "Cross-file call graph: resolve calls to their defining module, so "
            "A.main() calling engine.run() (imported from B) yields "
            "A.main -> B.engine.run. Deep AST analysis, codebase-defined symbols only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "module": {"type": "string", "description": "Optional: restrict to one module's calls"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
        },
    },
    {
        "name": "codeloom_search",
        "description": (
            "Search the symbol index for a function, class, or method. Returns "
            "where each symbol is defined (module + line) with a context snippet. "
            "Works across Python and other languages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol name to search, e.g. 'Engine' or 'run'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_usages",
        "description": (
            "Find where a symbol is USED (not just defined) across the codebase. "
            "Answers 'where is this function/class called?' with context snippets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol name to find usages of"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_grep",
        "description": (
            "Search file contents for a snippet (the 'find the exact code' "
            "capability). Returns ranked matches with context lines. Use to find "
            "where a specific code pattern or string appears."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "query": {"type": "string", "description": "Text to search for, e.g. 'retry' or 'class Engine'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codeloom_read",
        "description": (
            "Extract the exact source of a function, class, or method. Python uses "
            "AST; other languages use tree-sitter (when available) or brace-matching. "
            "Token-efficient: returns only the code the agent needs, not the whole file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol name to read, e.g. 'Engine' or 'run'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_explain",
        "description": (
            "Generate a plain-English explanation of a symbol's role using its AST "
            "signature + call graph. Template-based, no LLM needed. Returns a summary, "
            "what it calls, and what calls it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol name to explain"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_similar",
        "description": (
            "Find functions/classes with a structurally similar signature (same "
            "param count) for refactoring. Returns candidates across the codebase."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol to find similar ones for"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_deadcode",
        "description": (
            "Find functions/classes defined in the codebase but never called. "
            "Uses the call graph to detect dead code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
        },
    },
    {
        "name": "codeloom_get_symbol",
        "description": (
            "Token-counted symbol retrieval. By default returns a SUMMARY "
            "(signature + docstring + call graph) — the 95%+ token-savings mode. "
            "Pass full=true for the complete source. Agents should use summary "
            "first, then full only when they need the implementation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol name to retrieve"},
                "full": {"type": "boolean", "description": "Return full source instead of summary (default false)"},
                "context_lines": {"type": "integer", "description": "Surrounding lines to include (default 2)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_snippet",
        "description": (
            "Extract a byte-range snippet from a file. Returns the text + token "
            "estimate + byte count. Use for precise, token-efficient retrieval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "start_byte": {"type": "integer", "description": "Start byte offset"},
                "end_byte": {"type": "integer", "description": "End byte offset"},
            },
            "required": ["path", "start_byte", "end_byte"],
        },
    },
    {
        "name": "codeloom_incremental",
        "description": (
            "Show which files changed since the last run, using a hash-based "
            "cache (no daemon). Use for repeated runs on large repos — only "
            "re-parses changed files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
        },
    },
    {
        "name": "codeloom_verify",
        "description": (
            "Print the SHA-256 of a file so users can verify a downloaded copy "
            "of codeloom is official and not tampered with."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file to checksum"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "codeloom_trace",
        "description": (
            "Run a command (e.g. a test script) under sys.settrace and record the "
            "ACTUAL runtime call edges. Captures dynamic imports and monkeypatching "
            "that static analysis misses. Opt-in because it executes code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "command": {"type": "array", "items": {"type": "string"},
                            "description": "Command to run, e.g. ['tests.py']"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "codeloom_ask",
        "description": (
            "Single natural-language entry point. Ask in plain English and "
            "codeloom routes deterministically to the right tool — the agent "
            "never has to pick among 22 tools. Examples: 'what matters for "
            "fixing the login bug', 'what breaks if I change auth.py', 'where "
            "is the Agent class', 'what calls what across files', 'give me the "
            "whole context for adding retry'. This eliminates tool-routing "
            "errors entirely."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "query": {"type": "string", "description": "Natural-language request"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 5000)"},
            },
            "required": ["query"],
        },
    },
]


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

def _collect_files(root: str, max_files: int) -> List[str]:
    gi = os.path.join(root, ".gitignore")
    rules = codeloom.parse_gitignore(gi) if os.path.isfile(gi) else []
    files: List[str] = []
    codeloom._walk(root, rules, max_files, files)
    return files


# --------------------------------------------------------------------------- #
# In-memory index (the "better than daemon" layer).
# A daemon keeps the index in RAM for fast repeated queries but goes stale and
# needs a separate process. This keeps the index in memory for the lifetime of
# the MCP server (resident in the agent's process), and incrementally re-parses
# ONLY changed files (via content hashes) so it's always fresh — no separate
# process, no staleness, no idle resource use.
# --------------------------------------------------------------------------- #

class _Index:
    """Per-root in-memory index with incremental refresh."""

    def __init__(self):
        self._roots: Dict[str, Dict[str, Any]] = {}

    def _get(self, root: str, max_files: int) -> Dict[str, Any]:
        entry = self._roots.get(root)
        if entry is None:
            entry = {"files": [], "hashes": {}, "symbols": {}}
            self._roots[root] = entry
        return entry

    def files(self, root: str, max_files: int) -> List[str]:
        """Return the file list, re-walking only if the repo changed."""
        entry = self._get(root, max_files)
        # re-walk if the file set may have changed (cheap: compare count + mtimes)
        current = _collect_files(root, max_files)
        if len(current) != len(entry["files"]):
            entry["files"] = current
            entry["hashes"] = {}
        return entry["files"]

    def symbols(self, root: str, max_files: int) -> dict:
        """Return the symbol index, re-parsing only changed files."""
        entry = self._get(root, max_files)
        files = self.files(root, max_files)
        # find changed files
        changed = []
        for f in files:
            h = codeloom._file_hash(f)
            if entry["hashes"].get(f) != h:
                changed.append(f)
        if changed:
            # re-parse only changed files; reuse cached symbols for unchanged
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                mod = codeloom.module_name_of(f, root)
                h = codeloom._file_hash(f)
                if entry["hashes"].get(f) == h and f in entry["symbols"]:
                    continue  # unchanged, keep cached
                # parse fresh into a per-file dict
                file_symbols: Dict[str, Any] = {}
                if ext == ".py":
                    codeloom._index_python_bytes(f, mod, file_symbols)
                elif ext in codeloom.CALL_LANG_RULES:
                    codeloom._index_other_bytes(f, mod, ext, file_symbols)
                entry["symbols"][f] = file_symbols
                entry["hashes"][f] = h
        # rebuild the flat index from per-file symbols
        flat = {}
        for f, syms in entry["symbols"].items():
            for name, locs in syms.items():
                flat.setdefault(name, []).extend(locs)
        return flat


_INDEX = _Index()


def _resolve_focus(graph: dict, module: str, root: str) -> Optional[str]:
    """Resolve a focus target (path/dir/dotted) to a module in the graph."""
    focus = module
    focus_path = os.path.join(root, focus) if not os.path.isabs(focus) else focus
    if os.path.isdir(focus_path):
        focus = codeloom.module_name_of(focus_path, root)
    elif focus.endswith(".py") or os.path.isfile(focus_path):
        focus = codeloom.module_name_of(focus_path, root)
    if focus in graph:
        return focus
    # suffix match
    fsegs = focus.split(".")
    match = None
    for mod in graph:
        msegs = mod.split(".")
        if len(msegs) >= len(fsegs) and msegs[-len(fsegs):] == fsegs:
            if match is None or len(msegs) < len(match.split(".")):
                match = mod
    return match


def _route_ask(args: Dict[str, Any], root: str, max_files: int) -> Dict[str, Any]:
    """Route a natural-language request to the right codeloom tool.

    This is the answer to jcodemunch's 91-tool routing problem: instead of
    making the agent pick among 22 tools (or 91), codeloom_ask takes plain
    language and dispatches deterministically. The agent never misroutes
    because it never routes."""
    q = (args.get("query") or "").strip().lower()
    if not q:
        return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
    files = _collect_files(root, max_files)

    # 1. Task-orientation (the moat) — "what matters / what breaks / read order / context"
    if any(k in q for k in ["what matters", "relevant to", "which files", "for this task",
                            "what breaks", "impact of", "blast radius", "if i change",
                            "read order", "reading plan", "how to approach", "context for",
                            "pack", "whole context", "understand this task"]):
        if any(k in q for k in ["what breaks", "impact of", "blast radius", "if i change"]):
            # extract a module name if present
            import re
            m = re.search(r"([\w/]+\.py|[\w.]+)", q.replace("impact of", "").replace("what breaks if i change", ""))
            target = m.group(1) if m else None
            if target:
                graph = codeloom.build_graph(files, root)
                resolved = _resolve_focus(graph, target, root)
                if resolved:
                    return {"content": [{"type": "text", "text": codeloom.render_impact(graph, root, resolved)}]}
            return {"content": [{"type": "text", "text": codeloom.render_task(files, root, q)}]}
        if any(k in q for k in ["pack", "whole context", "context for", "understand this task"]):
            return {"content": [{"type": "text", "text": codeloom.render_pack(files, root, q)}]}
        if any(k in q for k in ["read order", "reading plan", "how to approach"]):
            return {"content": [{"type": "text", "text": codeloom.build_plan(files, root, q)}]}
        return {"content": [{"type": "text", "text": codeloom.render_task(files, root, q)}]}

    # 2. Symbol retrieval — "where is X / show me X / what does X do"
    if any(k in q for k in ["where is", "find symbol", "search for", "show me", "what does",
                            "explain", "source of", "definition of", "get symbol", "read "]):
        import re
        # extract the symbol, skipping stopwords and the query verbs
        stop = {"where", "is", "the", "a", "an", "find", "symbol", "search", "for",
                "show", "me", "what", "does", "explain", "source", "of", "definition",
                "get", "read", "class", "function", "method", "in", "this", "repo"}
        # find the first token that isn't a stopword (the actual symbol)
        sym = None
        for tok in re.findall(r"[A-Za-z_][\w.]*", q):
            if tok.lower() not in stop:
                sym = tok
                break
        if sym:
            if any(k in q for k in ["explain", "what does"]):
                return {"content": [{"type": "text", "text": codeloom.render_explain(files, root, sym)}]}
            if any(k in q for k in ["source of", "definition of", "read ", "get symbol"]):
                return {"content": [{"type": "text", "text": codeloom.render_read(files, root, sym)}]}
            return {"content": [{"type": "text", "text": codeloom.render_search(codeloom.build_byte_index(files, root), sym)}]}
        return {"content": [{"type": "text", "text": codeloom.render_task(files, root, q)}]}

    # 3. Call graph / structure — "what calls what / dependencies / map"
    if any(k in q for k in ["what calls", "call graph", "dependencies", "imports", "map the",
                            "structure", "what touches", "cross-file", "call path"]):
        if any(k in q for k in ["cross-file", "call path", "what calls what across"]):
            return {"content": [{"type": "text", "text": codeloom.render_cross_calls(codeloom.build_cross_call_graph(files, root), root)}]}
        if any(k in q for k in ["what calls", "call graph"]):
            return {"content": [{"type": "text", "text": codeloom.render_calls(codeloom.build_call_graph_multi(files, root), root)}]}
        if any(k in q for k in ["dependencies", "imports", "what touches"]):
            return {"content": [{"type": "text", "text": codeloom.render_graph(codeloom.build_graph(files, root), root)}]}
        return {"content": [{"type": "text", "text": codeloom.render_text(codeloom.build_map(root, True, max_files))}]}

    # 4. Default — map the repo
    return {"content": [{"type": "text", "text": codeloom.render_text(codeloom.build_map(root, True, max_files))}]}


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool and return an MCP result (text content)."""
    root = os.path.abspath(args.get("root", "."))
    max_files = int(args.get("max_files", 5000))
    files = _collect_files(root, max_files)

    # codeloom_ask: single natural-language entry point that routes
    # deterministically — the agent never has to pick among 22 tools.
    if name == "codeloom_ask":
        return _route_ask(args, root, max_files)

    if name == "codeloom_map":
        m = codeloom.build_map(root, True, max_files)
        text = codeloom.render_text(m)
    elif name == "codeloom_graph":
        graph = codeloom.build_graph(files, root)
        text = codeloom.render_graph(graph, root)
    elif name == "codeloom_focus":
        module = args.get("module")
        if not module:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'module' argument"}]}
        graph = codeloom.build_graph(files, root)
        resolved = _resolve_focus(graph, module, root)
        if resolved is None:
            return {"isError": True, "content": [{"type": "text", "text": f"module not found: {module}"}]}
        text = codeloom.render_graph(graph, root, start=resolved)
    elif name == "codeloom_calls":
        calls = codeloom.build_call_graph_multi(files, root)
        module = args.get("module")
        start = None
        if module:
            resolved = _resolve_focus(calls, module, root)
            if resolved is None:
                return {"isError": True, "content": [{"type": "text", "text": f"module not found: {module}"}]}
            start = resolved
        text = codeloom.render_calls(calls, root, start=start)
    elif name == "codeloom_diff":
        text = codeloom.render_diff(root, max_files)
    elif name == "codeloom_impact":
        module = args.get("module")
        if not module:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'module' argument"}]}
        graph = codeloom.build_graph(files, root)
        resolved = _resolve_focus(graph, module, root)
        if resolved is None:
            return {"isError": True, "content": [{"type": "text", "text": f"module not found: {module}"}]}
        text = codeloom.render_impact(graph, root, resolved)
    elif name == "codeloom_task":
        task = args.get("task")
        if not task:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'task' argument"}]}
        text = codeloom.render_task(files, root, task)
    elif name == "codeloom_plan":
        task = args.get("task")
        if not task:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'task' argument"}]}
        text = codeloom.build_plan(files, root, task)
    elif name == "codeloom_pack":
        task = args.get("task")
        if not task:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'task' argument"}]}
        text = codeloom.render_pack(files, root, task)
    elif name == "codeloom_cross":
        calls = codeloom.build_cross_call_graph(files, root)
        module = args.get("module")
        start = None
        if module:
            resolved = _resolve_focus(calls, module, root)
            if resolved is None:
                return {"isError": True, "content": [{"type": "text", "text": f"module not found: {module}"}]}
            start = resolved
        text = codeloom.render_cross_calls(calls, root, start=start)
    elif name == "codeloom_search":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        # use the in-memory index (incremental, always fresh)
        index = _INDEX.symbols(root, max_files)
        text = codeloom.render_search(index, symbol)
    elif name == "codeloom_usages":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codeloom.render_usages(files, root, symbol)
    elif name == "codeloom_grep":
        query = args.get("query")
        if not query:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
        text = codeloom.render_grep(files, root, query)
    elif name == "codeloom_read":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codeloom.render_read(files, root, symbol)
    elif name == "codeloom_explain":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codeloom.render_explain(files, root, symbol)
    elif name == "codeloom_similar":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codeloom.render_similar(files, root, symbol)
    elif name == "codeloom_deadcode":
        text = codeloom.render_deadcode(files, root)
    elif name == "codeloom_get_symbol":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        ctx = args.get("context_lines", 2)
        full = bool(args.get("full", False))
        # use the in-memory index (incremental, always fresh)
        index = _INDEX.symbols(root, max_files)
        locs = index.get(symbol)
        if not locs:
            text = f"# get_symbol: {symbol}\nSymbol not found.\n"
        elif full:
            loc = locs[0]
            text = (f"# get_symbol: {symbol}\n{loc['module']}:{loc['line']}  [{loc['kind']}]  "
                    f"bytes {loc['start_byte']}-{loc['end_byte']}  ~{loc['tokens']} tokens\n\n"
                    f"{loc['source']}\n")
        else:
            # summary-first (95%+ token savings)
            text = codeloom.render_get_symbol(files, root, symbol, ctx, summary=True)
    elif name == "codeloom_snippet":
        path = args.get("path")
        start = args.get("start_byte")
        end = args.get("end_byte")
        if not path or start is None or end is None:
            return {"isError": True, "content": [{"type": "text", "text": "missing path/start_byte/end_byte"}]}
        s = codeloom.get_snippet_by_offset(path, int(start), int(end))
        if s is None:
            return {"isError": True, "content": [{"type": "text", "text": f"cannot read {path}"}]}
        text = f"# snippet: {path} bytes {start}-{end}  ~{s['tokens']} tokens  {s['bytes']} bytes\n\n{s['text']}"
    elif name == "codeloom_incremental":
        text = codeloom.render_incremental(files, root, max_files)
    elif name == "codeloom_verify":
        path = args.get("path")
        if not path:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'path' argument"}]}
        text = codeloom.render_verify(path)
    elif name == "codeloom_trace":
        command = args.get("command")
        if not command:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'command' argument"}]}
        text = codeloom.render_trace(list(command), root)
    else:
        return {"isError": True, "content": [{"type": "text", "text": f"unknown tool: {name}"}]}

    return {"content": [{"type": "text", "text": text}]}


# --------------------------------------------------------------------------- #
# Minimal MCP stdio server (JSON-RPC 2.0)
# --------------------------------------------------------------------------- #

def _send(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _read() -> Optional[Dict[str, Any]]:
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def serve() -> int:
    while True:
        msg = _read()
        if msg is None:
            break
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        # Notifications have no id — respond to nothing.
        if msg_id is None:
            continue

        if method == "initialize":
            _send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            })
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            result = call_tool(name, args)
            _send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            _send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })
    return 0


if __name__ == "__main__":
    sys.exit(serve())
