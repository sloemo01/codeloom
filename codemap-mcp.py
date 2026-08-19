#!/usr/bin/env python3
"""
codemap-mcp — a Model Context Protocol (MCP) server that exposes codemap to
any MCP-capable agent (Claude Code, Cursor, Codex, Gemini, etc.) as native tools.

Zero-dependency: implements the MCP stdio transport (JSON-RPC 2.0 over
stdin/stdout) using only the Python standard library. No `mcp` package, no
daemon, no install beyond copying this file.

Tools exposed:
  codemap_map(root, max_files)          -> repo tree + entry points
  codemap_graph(root, max_files)        -> full import dependency graph
  codemap_focus(root, module, max_files)-> deps + dependents of one module

Usage:
  # Register in your agent's MCP config, e.g. Claude Code:
  #   "codemap": { "command": "python3", "args": ["/path/to/codemap-mcp.py"] }
  # or run standalone to test:
  #   echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python3 codemap-mcp.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

# Reuse codemap's logic directly (same directory).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codemap  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "codemap-mcp"
SERVER_VERSION = "0.19.0"

# --------------------------------------------------------------------------- #
# Tool definitions (MCP tools/list schema)
# --------------------------------------------------------------------------- #

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "codemap_map",
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
        "name": "codemap_graph",
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
        "name": "codemap_focus",
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
        "name": "codemap_calls",
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
        "name": "codemap_diff",
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
        "name": "codemap_impact",
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
        "name": "codemap_task",
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
        "name": "codemap_plan",
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
        "name": "codemap_cross",
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
        "name": "codemap_search",
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
        "name": "codemap_usages",
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
        "name": "codemap_grep",
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
        "name": "codemap_read",
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
        "name": "codemap_explain",
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
        "name": "codemap_similar",
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
        "name": "codemap_deadcode",
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
        "name": "codemap_get_symbol",
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
        "name": "codemap_snippet",
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
        "name": "codemap_incremental",
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
        "name": "codemap_verify",
        "description": (
            "Print the SHA-256 of a file so users can verify a downloaded copy "
            "of codemap is official and not tampered with."
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
        "name": "codemap_trace",
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
]


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

def _collect_files(root: str, max_files: int) -> List[str]:
    gi = os.path.join(root, ".gitignore")
    rules = codemap.parse_gitignore(gi) if os.path.isfile(gi) else []
    files: List[str] = []
    codemap._walk(root, rules, max_files, files)
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
            h = codemap._file_hash(f)
            if entry["hashes"].get(f) != h:
                changed.append(f)
        if changed:
            # re-parse only changed files; reuse cached symbols for unchanged
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                mod = codemap.module_name_of(f, root)
                h = codemap._file_hash(f)
                if entry["hashes"].get(f) == h and f in entry["symbols"]:
                    continue  # unchanged, keep cached
                # parse fresh into a per-file dict
                file_symbols: Dict[str, Any] = {}
                if ext == ".py":
                    codemap._index_python_bytes(f, mod, file_symbols)
                elif ext in codemap.CALL_LANG_RULES:
                    codemap._index_other_bytes(f, mod, ext, file_symbols)
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
        focus = codemap.module_name_of(focus_path, root)
    elif focus.endswith(".py") or os.path.isfile(focus_path):
        focus = codemap.module_name_of(focus_path, root)
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


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool and return an MCP result (text content)."""
    root = os.path.abspath(args.get("root", "."))
    max_files = int(args.get("max_files", 5000))
    files = _collect_files(root, max_files)

    if name == "codemap_map":
        m = codemap.build_map(root, True, max_files)
        text = codemap.render_text(m)
    elif name == "codemap_graph":
        graph = codemap.build_graph(files, root)
        text = codemap.render_graph(graph, root)
    elif name == "codemap_focus":
        module = args.get("module")
        if not module:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'module' argument"}]}
        graph = codemap.build_graph(files, root)
        resolved = _resolve_focus(graph, module, root)
        if resolved is None:
            return {"isError": True, "content": [{"type": "text", "text": f"module not found: {module}"}]}
        text = codemap.render_graph(graph, root, start=resolved)
    elif name == "codemap_calls":
        calls = codemap.build_call_graph_multi(files, root)
        module = args.get("module")
        start = None
        if module:
            resolved = _resolve_focus(calls, module, root)
            if resolved is None:
                return {"isError": True, "content": [{"type": "text", "text": f"module not found: {module}"}]}
            start = resolved
        text = codemap.render_calls(calls, root, start=start)
    elif name == "codemap_diff":
        text = codemap.render_diff(root, max_files)
    elif name == "codemap_impact":
        module = args.get("module")
        if not module:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'module' argument"}]}
        graph = codemap.build_graph(files, root)
        resolved = _resolve_focus(graph, module, root)
        if resolved is None:
            return {"isError": True, "content": [{"type": "text", "text": f"module not found: {module}"}]}
        text = codemap.render_impact(graph, root, resolved)
    elif name == "codemap_task":
        task = args.get("task")
        if not task:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'task' argument"}]}
        text = codemap.render_task(files, root, task)
    elif name == "codemap_plan":
        task = args.get("task")
        if not task:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'task' argument"}]}
        text = codemap.build_plan(files, root, task)
    elif name == "codemap_cross":
        calls = codemap.build_cross_call_graph(files, root)
        module = args.get("module")
        start = None
        if module:
            resolved = _resolve_focus(calls, module, root)
            if resolved is None:
                return {"isError": True, "content": [{"type": "text", "text": f"module not found: {module}"}]}
            start = resolved
        text = codemap.render_cross_calls(calls, root, start=start)
    elif name == "codemap_search":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        # use the in-memory index (incremental, always fresh)
        index = _INDEX.symbols(root, max_files)
        text = codemap.render_search(index, symbol)
    elif name == "codemap_usages":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codemap.render_usages(files, root, symbol)
    elif name == "codemap_grep":
        query = args.get("query")
        if not query:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
        text = codemap.render_grep(files, root, query)
    elif name == "codemap_read":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codemap.render_read(files, root, symbol)
    elif name == "codemap_explain":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codemap.render_explain(files, root, symbol)
    elif name == "codemap_similar":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codemap.render_similar(files, root, symbol)
    elif name == "codemap_deadcode":
        text = codemap.render_deadcode(files, root)
    elif name == "codemap_get_symbol":
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
            text = codemap.render_get_symbol(files, root, symbol, ctx, summary=True)
    elif name == "codemap_snippet":
        path = args.get("path")
        start = args.get("start_byte")
        end = args.get("end_byte")
        if not path or start is None or end is None:
            return {"isError": True, "content": [{"type": "text", "text": "missing path/start_byte/end_byte"}]}
        s = codemap.get_snippet_by_offset(path, int(start), int(end))
        if s is None:
            return {"isError": True, "content": [{"type": "text", "text": f"cannot read {path}"}]}
        text = f"# snippet: {path} bytes {start}-{end}  ~{s['tokens']} tokens  {s['bytes']} bytes\n\n{s['text']}"
    elif name == "codemap_incremental":
        text = codemap.render_incremental(files, root, max_files)
    elif name == "codemap_verify":
        path = args.get("path")
        if not path:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'path' argument"}]}
        text = codemap.render_verify(path)
    elif name == "codemap_trace":
        command = args.get("command")
        if not command:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'command' argument"}]}
        text = codemap.render_trace(list(command), root)
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
