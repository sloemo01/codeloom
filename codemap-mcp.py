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
SERVER_VERSION = "0.5.0"

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
]


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

def _collect_files(root: str, max_files: int) -> List[str]:
    gi = os.path.join(root, ".gitignore")
    globs, ignore_dirs = codemap.parse_gitignore(gi) if os.path.isfile(gi) else ([], [])
    files: List[str] = []
    codemap._walk(root, globs, ignore_dirs, max_files, files)
    return files


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
