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
import time
from typing import Any, Dict, List, Optional

# Reuse codeloom's logic directly (same directory).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codeloom  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "codeloom-mcp"
SERVER_VERSION = "0.78.0"

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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "codeloom_check_edit",
        "description": (
            "Preflight: is it safe to edit this symbol? Returns a terminal "
            "GO/CHECK/STOP verdict with the exact callers that will break, so "
            "the agent stops looping and knows definitively before rewriting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol to check, e.g. 'retry' or 'Engine'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_check_delete",
        "description": (
            "Preflight: is it safe to delete this symbol? Returns a terminal "
            "GO/STOP verdict — GO only if nothing references the symbol, else "
            "STOP with the exact dependents that will break."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Function to check, e.g. 'retry' or 'Engine'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["symbol"],
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_embed_search",
        "description": (
            "Fuzzy semantic symbol search using a zero-dependency subword-hash "
            "embedding (fastText n-gram technique, pure-Python). Finds symbols "
            "whose identifier is semantically/cosmetically similar to the query "
            "even on typos, camelCase splits, or cross-language names that exact "
            "match misses."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "query": {"type": "string", "description": "fuzzy/semantic query, e.g. 'engin' finds Engine"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codeloom_context",
        "description": (
            "Batch triage card for MULTIPLE symbols in ONE call (repowise "
            "get_context parity): per-target definition, same-module signatures, "
            "callers count, and governing ADR titles. Collapses the "
            "search->read->impact chain into a single round-trip."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "targets": {"type": "array", "items": {"type": "string"},
                            "description": "Symbol names to triage together"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["targets"],
        },
    },
    {
        "name": "codeloom_answer",
        "description": (
            "One-call cited answer to a natural-language code question (repowise "
            "get_answer parity): hybrid search -> best match with honest "
            "confidence (high/medium/low), source location, snippet, and "
            "callers/callees. Collapses search+read+reason into one round-trip."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "question": {"type": "string", "description": "Natural-language question, e.g. 'where is retry logic'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "codeloom_why",
        "description": (
            "Decision lookup with evidence stamps (repowise get_why parity): "
            "searches recorded memory/ADRs and stamps every matching line "
            "[exact]/[fuzzy]/[unverified] so the agent knows how much to trust it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "query": {"type": "string", "description": "Decision topic, e.g. 'rate limiting approach'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codeloom_health",
        "description": (
            "Code health screen (repowise get_health parity, speed-first): "
            "deterministic detectors — long functions, too-many-params, dead "
            "symbols, duplicate names — scored 0-10 per file, worst-first. Zero "
            "LLM calls, sub-second on typical repos, served from the resident "
            "in-memory index. A fast structural screen, NOT defect-validated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_risk",
        "description": (
            "Pre-merge change-risk report for a commit or range (clean-room, "
            "zero LLM): scores diff size, file spread, health findings in "
            "touched files, high-fan-in symbols touched, and recent fix "
            "history of touched files. Returns 0-100 score + band + drivers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "revspec": {"type": "string", "description": "Commit or range, e.g. HEAD~1..HEAD, main..feature"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_pattern",
        "description": (
            "Structural AST pattern search (ast-grep-style, zero-dep): find "
            "every code site matching a code shape. $VAR captures one node, "
            "$$$REST captures lists. Example: 'try: $B except Exception: pass' "
            "finds over-broad exception handlers with the handler body bound. "
            "Python files; no external binary needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "pattern": {"type": "string", "description": "Code pattern with $VAR / $$$REST metavariables"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["pattern"],
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_grep_symbolic",
        "description": (
            "Code-only grep: matches real CODE, excluding comments and string "
            "literals (the usual false-positive sources). Results ranked by "
            "symbol relevance — hits inside a known definition outrank loose "
            "module-level mentions, and the enclosing symbol is attached to "
            "each hit. Use --grep for raw text search including docs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "query": {"type": "string", "description": "Text/identifier to find in code"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": ["query"],
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_precision",
        "description": (
            "Graph precision report for a symbol: call edges annotated with "
            "confidence (definite vs maybe) plus class relationships and "
            "dependents. Helps agents trust which call-graph edges are real."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol name to inspect"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
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
        "name": "codeloom_verify_edit",
        "description": (
            "Edit-safety preflight: given a target file (or repo root), report "
            "whether an edit there is safe — dependents, call sites, blast "
            "radius — with a GO/STOP verdict. Ask before editing: 'did I break "
            "X', 'check my edit', 'is my edit safe'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "File to edit or repo root to audit"},
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "codeloom_blindspot",
        "description": (
            "Coverage audit: find the files/symbols you have NOT read yet so "
            "nothing important is missed before you act. Answers 'read "
            "coverage', 'read everything', 'what haven't I read', 'blindspot'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Path or repo root to audit for blind spots"},
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "codeloom_ask",
        "description": (
            "Single natural-language entry point. Ask in plain English and "
            "codeloom routes deterministically to the right tool — the agent "
            "never has to pick among 79 tools. Examples: 'what matters for "
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
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codeloom_loom",
        "description": (
            "The intent engine. Given a task in plain English, return LAYERED "
            "context in one call: overview -> important files (edit-relevance) "
            "-> relevant code (pack) -> git churn -> repository memory. This is "
            "the keystone feature — the agent gets everything for a task without "
            "orchestrating retrieval itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "task": {"type": "string", "description": "The task, e.g. 'fix the auth bug'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "codeloom_remember",
        "description": (
            "Append a note to the repository's persistent memory (architecture "
            "decisions, patterns, conventions). Future loom_context calls return "
            "it. Section: ARCHITECTURE | DECISIONS | PATTERNS | CONVENTIONS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "note": {"type": "string", "description": "The decision/pattern to remember"},
                "section": {"type": "string", "description": "ARCHITECTURE|DECISIONS|PATTERNS|CONVENTIONS (default DECISIONS)"},
            },
            "required": ["note"],
        },
    },
    {
        "name": "codeloom_adr",
        "description": (
            "Write a structured Architectural Decision Record (context + decision "
            "+ status) to .codeloom-memory/adr/. Captures the human 'why' behind "
            "an architecture choice so it survives compaction and cross-session "
            "work. List with codeloom_adr_list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "title": {"type": "string", "description": "ADR title, e.g. 'Use Postgres over MySQL'"},
                "context": {"type": "string", "description": "The context/problem that motivated the decision"},
                "decision": {"type": "string", "description": "The decision made"},
                "status": {"type": "string", "description": "Accepted | Proposed | Superseded (default Accepted)"},
            },
            "required": ["title", "decision"],
        },
    },
    {
        "name": "codeloom_adr_list",
        "description": (
            "List all saved Architectural Decision Records. Use before writing a "
            "new ADR to avoid duplicating an existing decision."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
        },
    },
    {
        "name": "codeloom_churn",
        "description": (
            "Git intelligence: the most-edited files (by commit count) — an "
            "instability signal. Helps decide which files are risky to change."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_hybrid_search",
        "description": (
            "Hybrid search: BM25 lexical score + structural signals (symbol "
            "kind, size) + git churn scored together. Ranks symbols by combined "
            "relevance, better than bare substring search for vague queries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "query": {"type": "string", "description": "Search query, e.g. 'parse cli args' or 'retry'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codeloom_seen",
        "description": (
            "Session memory: report which files and symbols were already read "
            "this session, so the agent can skip re-reading them and save tokens. "
            "Reads the local session log."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
        },
    },
    {
        "name": "codeloom_cross_repo",
        "description": (
            "Build a combined knowledge graph across multiple repository roots "
            "(frontend + backend + SDK + CLI + docs). Returns per-repo modules "
            "and cross-repo service-to-service edges."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repos": {"type": "array", "items": {"type": "string"}, "description": "Absolute paths to repo roots"},
                "max_files": {"type": "integer", "description": "Cap traversal per repo (default 20000)"},
            },
            "required": ["repos"],
        },
    },
    {
        "name": "codeloom_framework",
        "description": (
            "Detect the web/app framework (Next.js, FastAPI, Django, Laravel, "
            "Express, etc.) and surface its structure: entry points, routes, "
            "models, config, and conventions. Framework-level understanding, "
            "not just language-level."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_architecture",
        "description": (
            "Detect the architectural pattern (MVC / layered / Clean / DDD / "
            "Hexagonal / monolith / microservices) from the repo layout, plus "
            "the top-level structure. Orients the agent to which layer a change "
            "belongs in."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_heatmap",
        "description": (
            "Dependency heatmap: god/hub classes (widest blast radius), circular "
            "imports, and possibly-unused modules. Refactor-risk signals."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_explain_topic",
        "description": (
            "Explain a topic/domain end-to-end: relevant files + call flow, "
            "instead of a single symbol. E.g. 'authentication' -> the files and "
            "how they connect."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "topic": {"type": "string", "description": "The topic/domain, e.g. 'authentication'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "codeloom_docs",
        "description": (
            "Generate a README or ARCHITECTURE doc from the repo structure. "
            "kind: 'readme' (default) or 'arch'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "kind": {"type": "string", "description": "'readme' or 'arch' (default readme)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_refactor",
        "description": (
            "Refactor engine for a symbol: files touched, dependencies, risk "
            "assessment (preflight), and suggested safe order."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol/area to refactor"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_rename",
        "description": (
            "What a rename touches: every definition, every file containing the "
            "name, every dependent module/edge. Run before renaming a symbol to "
            "know the blast radius and update all references."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "old": {"type": "string", "description": "Current symbol name"},
                "new": {"type": "string", "description": "New symbol name"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["old", "new"],
        },
    },
    {
        "name": "codeloom_bug_predict",
        "description": (
            "Bug prediction: files statistically likely to break, scored by "
            "churn + coupling + complexity. Priority hotfix/refactor targets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_timeline",
        "description": (
            "Repository timeline: replay architecture evolution via git log — "
            "who changed what, when."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "limit": {"type": "integer", "description": "Max commits (default 15)"},
            },
        },
    },
    {
        "name": "codeloom_dedup",
        "description": (
            "Session dedupe: skip files already read this session and return "
            "only the new delta — saves tokens across repeated calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_routes",
        "description": (
            "Extract HTTP routes: METHOD path -> handler across frameworks "
            "(FastAPI, Flask, Express, Django, Next.js, Starlette). Links URL "
            "patterns to the handler that serves them — framework-aware routes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_channels",
        "description": (
            "Pub-sub / event channel map: EMITS -> LISTENS_ON edges for "
            "socket.io, Node EventEmitter, Kafka/RabbitMQ-style pub-sub. Links "
            "senders to receivers across files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_export",
        "description": (
            "Export a portable, self-contained graph snapshot (symbols + call/"
            "import edges + routes + channels) to a single JSON file. Commit it "
            "to the repo so teammates clone a pre-built graph and skip the reindex."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "out_path": {"type": "string", "description": "Destination file path for the snapshot"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["out_path"],
        },
    },
    {
        "name": "codeloom_plugin_sdk",
        "description": (
            "Show the plugin SDK surface: how to write a framework-aware "
            "extraction hook that extends codeloom."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
        },
    },
    {
        "name": "codeloom_langs",
        "description": (
            "List supported languages/extensions: the broad 130+ regex/C "
            "structural-extraction set plus the opt-in tree-sitter precision set."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "codeloom_lsp",
        "description": (
            "LSP bridge status: detect installed language servers (pyright, "
            "clangd, rust-analyzer, gopls...) for optional semantic enrichment. "
            "codeloom stays zero-dep — LSP is never required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
        },
    },
    {
        "name": "codeloom_lsp_symbol",
        "description": (
            "Resolve a symbol's real definition via an installed LSP server "
            "(pyright/clangd/rust-analyzer/gopls/ts-server) — the cross-file "
            "edge static parsing can miss. Falls back to the static index if "
            "no server is present. Optional, zero-dep."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "symbol": {"type": "string", "description": "Symbol to resolve via LSP"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "codeloom_graph_html",
        "description": (
            "Write a local zoomable HTML graph view of imports/calls to "
            "codeloom-graph.html. Self-contained, no daemon — open in a browser."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_find",
        "description": (
            "Natural-language flow discovery: 'find where login starts' / 'show "
            "every payment flow'. Returns the domain's entry points + call flow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "query": {"type": "string", "description": "Natural-language query, e.g. 'find where login starts'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codeloom_files",
        "description": (
            "Find files by name or glob, e.g. '--files engine' or '--files *.py'. "
            "Returns matching paths relative to root. For locating a file you "
            "don't know the exact path of."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "glob": {"type": "string", "description": "File name substring or glob, e.g. 'engine' or '*.py'"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
            "required": ["glob"],
        },
    },
    {
        "name": "codeloom_query",
        "description": (
            "Fast structural query against the persisted graph (build with --index "
            "first): callers X, callees X, dependents X, hubs, routes, symbol X. "
            "One graph query replaces many file scans — sub-ms once indexed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "query": {"type": "string", "description": "e.g. 'callers Engine', 'hubs', 'routes', 'dependents auth'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codeloom_context_diff",
        "description": (
            "Branch-to-branch architecture-level diff: which modules changed "
            "between two refs, not just lines."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "base": {"type": "string", "description": "Base ref (default main)"},
                "head": {"type": "string", "description": "Head ref (default HEAD)"},
            },
            "required": ["base", "head"],
        },
    },
    {
        "name": "codeloom_session_report",
        "description": (
            "Summarize the local session log: total calls, tokens, and estimated "
            "input cost, broken down by command. Local observability — no network, "
            "no daemon. Run codeloom with --session to start logging."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
        },
    },
    {
        "name": "codeloom_watch",
        "description": (
            "Incremental daemon-less refresh: re-index only files changed since "
            "the last index, keeping the lazy per-symbol store current. Call "
            "before queries to guarantee a fresh index without a full rebuild "
            "or a managed daemon."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_resume",
        "description": (
            "Emit a compact structural snapshot (entry points + modules + hub "
            "modules + top call sites) to restore an agent's context after a "
            "compaction. Paste the output in one shot instead of re-deriving "
            "the codebase map."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "max_files": {"type": "integer", "description": "Cap traversal (default 20000)"},
            },
        },
    },
    {
        "name": "codeloom_checkpoint",
        "description": (
            "Snapshot in-progress work (uncommitted git diff + a status note) to "
            "a file so it survives a context compaction. Call this before a "
            "compaction or at the end of a work chunk. Restore later with "
            "codeloom_checkpoint_restore."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "note": {"type": "string", "description": "Status note: what you were doing / decided"},
            },
        },
    },
    {
        "name": "codeloom_checkpoint_restore",
        "description": (
            "Read the last checkpoint back so the agent can resume in-progress "
            "work (uncommitted diff + status note) after a compaction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
        },
    },
    {
        "name": "codeloom_get_working_state",
        "description": (
            "Return the layered working-state packet: goal, status, key decisions, "
            "actions taken, open items/hypotheses, and hot set (already-understood "
            "files). Call this FIRST after any context compaction or 'I forgot' "
            "signal and treat the result as the source of truth. This is how the "
            "agent does not forget what it did."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
        },
    },
    {
        "name": "codeloom_record_decision",
        "description": (
            "Record an accepted or rejected decision with a reason into the session "
            "journal (and persistent memory). Prevents re-trying failed ideas after "
            "a compaction. Use status 'accepted' when you choose an approach, "
            "'rejected' when you abandon one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "title": {"type": "string", "description": "The decision, e.g. 'Use Redis for rate limiting'"},
                "reason": {"type": "string", "description": "Why you chose/rejected it"},
                "status": {"type": "string", "enum": ["accepted", "rejected"], "default": "accepted"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "codeloom_record_hypothesis",
        "description": (
            "Record an open hypothesis about the codebase so it survives compaction "
            "and can be verified later. Shows up in the working-state packet's open "
            "items."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "title": {"type": "string", "description": "The hypothesis, e.g. 'connection pool not shared correctly'"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "codeloom_mark_seen",
        "description": (
            "Mark files or symbols as already deeply understood so they appear in the "
            "hot set of future working-state packets. Use after reading a file/symbol "
            "so a post-compaction resume highlights it and you don't re-read it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "items": {"type": "array", "items": {"type": "string"}, "description": "File paths or symbol names"},
            },
            "required": ["items"],
        },
    },
    {
        "name": "codeloom_list_open_items",
        "description": (
            "List the open items/hypotheses recorded in the current session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
            },
        },
    },
    {
        "name": "codeloom_record_lesson",
        "description": (
            "Record a lesson/trap: something tried and why it failed, so a wiped "
            "agent never re-explores the same dead end. Use when you abandon an "
            "approach after trying it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "lesson": {"type": "string", "description": "e.g. 'in-memory bucket failed because not multi-instance'"},
            },
            "required": ["lesson"],
        },
    },
    {
        "name": "codeloom_query_memory",
        "description": (
            "Search long-term memory (decisions, patterns, lessons, conventions, "
            "ADRs) for what the agent already knows about a topic. Use for 'what "
            "do we already know about X' before re-deriving it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "query": {"type": "string", "description": "topic, e.g. 'auth rate limiting'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codeloom_cognitive_load",
        "description": (
            "Cognitive-load-aware task decomposition: splits a topic into "
            "working-memory-sized steps (intrinsic load), flags noise to skip "
            "(extraneous load), and surfaces the mental model to build (germane "
            "load: decisions, lessons, open items, hot set). Use when starting a "
            "multi-step task or deciding what minimal context to load."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Absolute path to the repo (default: cwd)"},
                "topic": {"type": "string", "description": "task/topic to decompose"},
            },
            "required": ["topic"],
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
    """Per-root in-memory index with incremental refresh. Bounded: at most
    MAX_ROOTS roots stay resident (LRU by last access) so a long session
    across many big repos can't grow memory forever."""

    MAX_ROOTS = 8

    def __init__(self):
        self._roots: Dict[str, Dict[str, Any]] = {}
        self._access: List[str] = []  # LRU order, most recent last

    def _get(self, root: str, max_files: int) -> Dict[str, Any]:
        entry = self._roots.get(root)
        if entry is None:
            if len(self._roots) >= self.MAX_ROOTS:
                # evict least-recently-used root
                lru = self._access[0]
                self._roots.pop(lru, None)
                self._access = [r for r in self._access[1:] if r != lru]
            entry = {"files": [], "hashes": {}, "symbols": {}, "kg": None}
            self._roots[root] = entry
        if root in self._access:
            self._access.remove(root)
        self._access.append(root)
        return entry

    def files(self, root: str, max_files: int) -> List[str]:
        """Return the resident file list. The walk itself is cached and only
        redone when the repo's top-level mtime signature changes — repeated
        MCP calls hit memory, not the filesystem (daemon-speed without a
        daemon). Signature = max mtime + count of root entries, O(1) to check."""
        entry = self._get(root, max_files)
        sig = self._root_signature(root)
        if sig != entry.get("_walk_sig"):
            current = _collect_files(root, max_files)
            if len(current) != len(entry["files"]) or set(current) != set(entry["files"]):
                entry["files"] = current
                entry["hashes"] = {}
            else:
                entry["files"] = current
            entry["_walk_sig"] = sig
        return entry["files"]

    @staticmethod
    def _root_signature(root: str) -> tuple:
        """Cheap change detector for the walk: (entry count, newest mtime)
        of the root dir itself plus one level of subdirs. Catches new/deleted
        files and dirs without walking the whole tree."""
        try:
            st = os.stat(root)
            sig = [st.st_mtime]
            try:
                with os.scandir(root) as it:
                    n = 0
                    for e in it:
                        n += 1
                        if n > 500:
                            break
                        try:
                            sig.append(e.stat().st_mtime)
                        except OSError:
                            pass
            except OSError:
                pass
            return (len(sig), max(sig))
        except OSError:
            return (0, 0.0)

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

    def kg(self, root: str, max_files: int) -> dict:
        """Return the knowledge-graph call edges, cached in memory and
        invalidated when files change — including in-place EDITS (content
        hashes), not just file-set changes. This is the resident-graph win:
        heavy ops (--cross, --deadcode) hit memory, not disk — daemon-speed
        without a separate daemon process."""
        entry = self._get(root, max_files)
        files = self.files(root, max_files)
        # invalidate on file-set change OR any content change: sample mtimes
        # of tracked files (cheap) — if the newest mtime moved past what the
        # cached graph was built at, rebuild.
        cached = entry.get("kg")
        if cached is not None and cached.get("_files") == files:
            newest = 0.0
            for f in files[:2000]:
                try:
                    m = os.stat(f).st_mtime
                    if m > newest:
                        newest = m
                except OSError:
                    pass
            if newest <= cached.get("_built_mtime", float("inf")):
                return entry["kg"]["calls"]
        calls = codeloom.build_call_graph_multi(files, root)
        entry["kg"] = {
            "_files": files,
            "_built_mtime": time.time(),
            "calls": {m: {c: sorted(s) for c, s in funcs.items()} for m, funcs in calls.items()},
        }
        return entry["kg"]["calls"]


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


_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codeloom.py")


def _run_core_or_cli(candidates: tuple, cli_argv: List[str], target: str, label: str) -> str:
    """Invoke the newest codeloom machinery from the MCP server.

    Prefers the core render function by direct import (candidate names, since
    the core agent may name it render_X or X); falls back to the CLI equivalent
    via argv (never shell=True). If neither exists yet (core agent still
    building), returns a clear, non-crashing placeholder.
    """
    for name in candidates:
        fn = getattr(codeloom, name, None)
        if fn is not None:
            try:
                out = fn(target)
            except Exception as exc:  # noqa: BLE001 - surface honestly, never crash
                return f"# codeloom_{label}\ncore {name} raised: {exc}\n"
            return out if isinstance(out, str) else str(out)
    import subprocess as _sp
    try:
        proc = _sp.run(cli_argv, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return (f"# codeloom_{label}\n{label} not yet available in core "
                f"({'/'.join(candidates)} missing; subprocess failed: {exc}).\n")
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout
    errs = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
    hint = errs[0] if errs else f"exit {proc.returncode}"
    return (f"# codeloom_{label}\n{label} not yet available in core "
            f"({'/'.join(candidates)} missing and `{' '.join(cli_argv)}` failed: {hint}).\n")


def _verify_edit(target: str) -> str:
    """Edit-safety preflight: core verify_edit/render_verify_edit, else CLI."""
    return _run_core_or_cli(("verify_edit", "render_verify_edit"),
                            [sys.executable, _CLI_PATH, "--verify-edit", target],
                            target, "verify_edit")


def _blindspot(target: str) -> str:
    """Coverage audit: core render_blindspot, else CLI --blindspot."""
    return _run_core_or_cli(("render_blindspot",),
                            [sys.executable, _CLI_PATH, "--blindspot", target],
                            target, "blindspot")


def _route_ask(args: Dict[str, Any], root: str, max_files: int) -> Dict[str, Any]:
    """Route a natural-language request to the right codeloom tool.

    Fail-safe by design: every branch returns USEFUL context, and the default
    always returns the map + task relevance — never an error. Even an ambiguous
    query yields something the agent can act on, so a 'wrong' pick is still
    helpful. This is the answer to jcodemunch's 91-tool routing problem: the
    agent never picks among 79 tools, and codeloom never returns nothing."""
    q = (args.get("query") or "").strip().lower()
    if not q:
        # empty query -> still return the map (never an error)
        return {"content": [{"type": "text", "text": codeloom.render_text(codeloom.build_map(root, True, max_files))}]}
    files = _collect_files(root, max_files)

    # 0. Edit-safety guard — verify/blindspot questions route to the newest
    # tools, BEFORE the symbol/read branches so 'read coverage' etc. win.
    if any(k in q for k in ["verify", "did i break", "check my edit",
                            "is my edit safe", "edit safety", "verify edit"]):
        import re as _re
        m = _re.search(r"([\w./-]+\.py|[\w./-]+)",
                       q.replace("did i break", "").replace("check my edit", "")
                        .replace("verify", "").replace("edit safety", ""))
        target = m.group(1) if m else root
        if not os.path.isabs(target):
            target = os.path.join(root, target)
        return {"content": [{"type": "text", "text": _verify_edit(target)}]}
    if any(k in q for k in ["blindspot", "read coverage", "read everything",
                            "what haven't i read", "not yet read", "coverage gap",
                            "what am i missing"]):
        import re as _re
        m = _re.search(r"([\w./-]+\.py|[\w./-]+)",
                       q.replace("blindspot", "").replace("read coverage", "")
                        .replace("read everything", ""))
        target = m.group(1) if m else root
        if not os.path.isabs(target):
            target = os.path.join(root, target)
        return {"content": [{"type": "text", "text": _blindspot(target)}]}

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
                    # combined: impact + task relevance (fail-safe)
                    impact = codeloom.render_impact(graph, root, resolved)
                    task = codeloom.render_task(files, root, q, top=3)
                    return {"content": [{"type": "text", "text": impact + "\n" + task}]}
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

    # 4. HTTP routes / pub-sub — "what endpoints / what channels"
    if any(k in q for k in ["http route", "endpoint", "api route", "what routes", "url pattern",
                            "rest api", "get post put delete", "web framework routes"]):
        return {"content": [{"type": "text", "text": codeloom.render_routes(root, max_files)}]}
    if any(k in q for k in ["pub sub", "pubsub", "event channel", "emits", "listens on",
                            "socket.io", "kafka", "rabbitmq", "message queue", "event emitter"]):
        return {"content": [{"type": "text", "text": codeloom.render_channels(root, max_files)}]}

    # 5. Framework / architecture / docs
    if any(k in q for k in ["what framework", "which framework", "framework", "web stack"]):
        return {"content": [{"type": "text", "text": codeloom.render_framework(root, max_files)}]}
    if any(k in q for k in ["architecture", "big picture", "overall design", "system design",
                            "how is this organized", "layers"]):
        return {"content": [{"type": "text", "text": codeloom.render_architecture(files, root)}]}
    if any(k in q for k in ["generate readme", "write readme", "make docs", "documentation",
                            "arch doc", "architecture doc"]):
        kind = "arch" if any(k in q for k in ["arch", "architecture doc"]) else "readme"
        return {"content": [{"type": "text", "text": codeloom.render_auto_docs(files, root, kind)}]}

    # 6. Rename / refactor / similar / deadcode / precision
    if any(k in q for k in ["rename", "what does renaming", "rename impact"]):
        import re as _re
        m = _re.findall(r"([A-Za-z_][\w.]*)", q.replace("rename", "").replace("what does renaming", ""))
        if len(m) >= 2:
            return {"content": [{"type": "text", "text": codeloom.render_rename(files, root, m[0], m[1])}]}
        return {"content": [{"type": "text", "text": codeloom.render_rename(files, root, m[0] if m else "", "new")}]}
    if any(k in q for k in ["refactor", "how to refactor", "refactoring"]):
        import re as _re
        m = _re.search(r"([A-Za-z_][\w.]*)", q.replace("refactor", "").replace("how to refactor", ""))
        return {"content": [{"type": "text", "text": codeloom.render_refactor(files, root, m.group(1) if m else "")}]}
    if any(k in q for k in ["similar", "same shape", "similar function", "similar class", "duplicate"]):
        import re as _re
        m = _re.search(r"([A-Za-z_][\w.]*)", q.replace("similar", "").replace("same shape", ""))
        return {"content": [{"type": "text", "text": codeloom.render_similar(files, root, m.group(1) if m else "")}]}
    if any(k in q for k in ["dead code", "unused", "never called", "deadcode"]):
        return {"content": [{"type": "text", "text": codeloom.render_deadcode(files, root)}]}
    if any(k in q for k in ["precision", "confidence", "class relationship", "implements", "overrides"]):
        import re as _re
        m = _re.search(r"([A-Za-z_][\w.]*)", q.replace("precision", ""))
        return {"content": [{"type": "text", "text": codeloom.render_precision(files, root, m.group(1) if m else "")}]}

    # 7. Memory / persistence / compaction — the "never forgets" layer
    # Highest priority: working-state / compaction survival
    if any(k in q for k in ["working state", "what did i do", "where was i",
                            "current state", "remind me", "what is my status",
                            "after compaction", "recover context", "what did i decide",
                            "record decision", "what is still open", "hot set"]):
        if any(k in q for k in ["record decision", "decided ", "reject "]):
            import re as _re
            title = _re.sub(r"(record decision|decided|reject|to use|because.*)", "", q).strip()
            status = "rejected" if ("reject" in q or "rejected" in q) else "accepted"
            return {"content": [{"type": "text", "text": codeloom.wm_decide(root, title or q, "", status)}]}
        if any(k in q for k in ["open", "hypothes"]):
            return {"content": [{"type": "text", "text": codeloom.list_open_items(root)}]}
        return {"content": [{"type": "text", "text": codeloom.render_working_state(root, full=True)}]}
    if any(k in q for k in ["remember", "save this", "note this", "record that", "write down"]):
        import re as _re
        note = q.replace("remember", "").replace("save this", "").replace("note this", "").replace("record that", "").replace("write down", "").strip()
        section = "DECISIONS"
        if any(k in q for k in ["architecture"]):
            section = "ARCHITECTURE"
        elif any(k in q for k in ["pattern"]):
            section = "PATTERNS"
        elif any(k in q for k in ["convention"]):
            section = "CONVENTIONS"
        return {"content": [{"type": "text", "text": codeloom.memory_remember(root, section, note or q)}]}
    if any(k in q for k in ["what did i remember", "my memory", "read memory", "what do i know"]):
        return {"content": [{"type": "text", "text": codeloom.memory_read(root)}]}
    if any(k in q for k in ["adr", "architectural decision", "record decision", "decision record"]):
        if any(k in q for k in ["list", "what adrs", "show adrs"]):
            return {"content": [{"type": "text", "text": codeloom.render_adr_list(root)}]}
        import re as _re
        m = _re.search(r"adr[:\s]+([A-Za-z0-9 _-]+)", q)
        title = m.group(1).strip() if m else "Decision"
        return {"content": [{"type": "text", "text": codeloom.render_adr(root, title, "", "recorded via ask")}]}
    if any(k in q for k in ["checkpoint", "save my progress", "snapshot my work", "in progress"]):
        import re as _re
        note = q.replace("checkpoint", "").replace("save my progress", "").replace("snapshot my work", "").strip()
        return {"content": [{"type": "text", "text": codeloom.render_checkpoint(root, note or None)}]}
    if any(k in q for k in ["restore checkpoint", "resume my work", "what was i doing", "checkpoint restore"]):
        return {"content": [{"type": "text", "text": codeloom.render_checkpoint_restore(root)}]}
    if any(k in q for k in ["resume", "restore context", "compaction", "i forgot", "where was i", "structural map"]):
        return {"content": [{"type": "text", "text": codeloom.render_resume(files, root, max_files)}]}
    if any(k in q for k in ["what have i explored", "what did i read", "seen", "already read"]):
        return {"content": [{"type": "text", "text": codeloom.render_seen(root)}]}
    if any(k in q for k in ["session report", "what did i do", "session summary", "tokens spent", "cost"]):
        return {"content": [{"type": "text", "text": codeloom.render_session_report(root)}]}

    # 8. Export / cross-repo / context-diff / files / langs
    if any(k in q for k in ["export", "share graph", "snapshot", "portable graph"]):
        import re as _re
        m = _re.search(r"export[:\s]+([\w./-]+)", q)
        out = m.group(1) if m else os.path.join(root, "codeloom-snapshot.json")
        return {"content": [{"type": "text", "text": codeloom.render_export(root, out, max_files)}]}
    if any(k in q for k in ["cross repo", "across repos", "multiple repos", "services", "frontend and backend"]):
        return {"content": [{"type": "text", "text": codeloom.render_cross_repo([root], max_files)}]}
    if any(k in q for k in ["context diff", "branch diff", "what changed between", "architecture diff"]):
        return {"content": [{"type": "text", "text": codeloom.render_context_diff(root)}]}
    if any(k in q for k in ["find file", "which file", "file named", "locate file", "glob"]):
        import re as _re
        m = _re.search(r"([\w.*/]+)", q.replace("find file", "").replace("which file", "").replace("file named", "").replace("locate file", ""))
        return {"content": [{"type": "text", "text": codeloom.render_files(files, root, m.group(1) if m else "*")}]}
    if any(k in q for k in ["what languages", "supported languages", "langs"]):
        return {"content": [{"type": "text", "text": codeloom.render_langs()}]}

    # 7b. Task-shaped fallbacks (added AFTER all specific routes so they
    # never steal an existing match): why -> decision lookup; question words
    # or bare topic -> one-call cited answer.
    if any(k in q for k in ["why ", "why is", "reason for"]):
        return {"content": [{"type": "text", "text": codeloom.render_why(files, root, q)}]}
    if any(w in q for w in ["who ", "what ", "how ", "where "]) or len(q.split()) <= 4:
        return {"content": [{"type": "text", "text": codeloom.render_answer(files, root, q)}]}

    # 4. Default — map + task relevance (never an error, always useful)
    map_text = codeloom.render_text(codeloom.build_map(root, True, max_files))
    task_text = codeloom.render_task(files, root, q, top=3)
    return {"content": [{"type": "text", "text": map_text + "\n" + task_text}]}


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool and return an MCP result (text content)."""
    root = os.path.abspath(args.get("root", "."))
    max_files = int(args.get("max_files", 5000))
    files = _collect_files(root, max_files)

    # codeloom_ask: single natural-language entry point that routes
    # deterministically — the agent never has to pick among 79 tools.
    if name == "codeloom_ask":
        return _route_ask(args, root, max_files)

    if name == "codeloom_verify_edit":
        target = args.get("target") or root
        if not os.path.isabs(target):
            target = os.path.join(root, target)
        return {"content": [{"type": "text", "text": _verify_edit(target)}]}
    if name == "codeloom_blindspot":
        target = args.get("target") or root
        if not os.path.isabs(target):
            target = os.path.join(root, target)
        return {"content": [{"type": "text", "text": _blindspot(target)}]}

    if name == "codeloom_framework":
        return {"content": [{"type": "text", "text": codeloom.render_framework(root, max_files)}]}

    if name == "codeloom_architecture":
        f = _collect_files(root, max_files)
        text = codeloom.render_architecture(f, root)
    elif name == "codeloom_heatmap":
        f = _collect_files(root, max_files)
        text = codeloom.dependency_heatmap(f, root)
    elif name == "codeloom_explain_topic":
        topic = args.get("topic")
        if not topic:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'topic' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_explain_topic(f, root, topic, max_files)
    elif name == "codeloom_docs":
        kind = args.get("kind", "readme")
        f = _collect_files(root, max_files)
        text = codeloom.render_auto_docs(f, root, kind)
    elif name == "codeloom_refactor":
        sym = args.get("symbol")
        if not sym:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_refactor(f, root, sym, max_files)
    elif name == "codeloom_rename":
        old = args.get("old"); new = args.get("new")
        if not old or not new:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'old'/'new' arguments"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_rename(f, root, old, new)
    elif name == "codeloom_bug_predict":
        f = _collect_files(root, max_files)
        text = codeloom.render_bug_predict(f, root)
    elif name == "codeloom_timeline":
        text = codeloom.render_repo_timeline(root, int(args.get("limit", 15)))
    elif name == "codeloom_dedup":
        f = _collect_files(root, max_files)
        text = codeloom.render_dedup(root, f)
    elif name == "codeloom_routes":
        text = codeloom.render_routes(root, max_files)
    elif name == "codeloom_channels":
        text = codeloom.render_channels(root, max_files)
    elif name == "codeloom_export":
        out = args.get("out_path")
        if not out:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'out_path' argument"}]}
        text = codeloom.render_export(root, out, max_files)
    elif name == "codeloom_plugin_sdk":
        text = codeloom.render_plugin_sdk(root)
    elif name == "codeloom_lsp":
        text = codeloom.render_lsp(root)
    elif name == "codeloom_langs":
        text = codeloom.render_langs()
    elif name == "codeloom_lsp_symbol":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_lsp_symbol(f, root, symbol)
    elif name == "codeloom_graph_html":
        f = _collect_files(root, max_files)
        text = codeloom.render_graph_html(f, root)
    elif name == "codeloom_find":
        query = args.get("query")
        if not query:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_find(f, root, query, max_files)
    elif name == "codeloom_files":
        glob = args.get("glob")
        if not glob:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'glob' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_files(f, root, glob)
    elif name == "codeloom_query":
        query = args.get("query")
        if not query:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
        text = codeloom.render_query(root, query)
    elif name == "codeloom_context_diff":
        base = args.get("base", "main")
        head = args.get("head", "HEAD")
        text = codeloom.render_context_diff(root, base, head)
    elif name == "codeloom_session_report":
        return {"content": [{"type": "text", "text": codeloom.render_session_report(root)}]}

    elif name == "codeloom_watch":
        text = codeloom.refresh_index_incremental(root, max_files)

    elif name == "codeloom_resume":
        f = _collect_files(root, max_files)
        text = codeloom.render_resume(f, root, max_files)

    elif name == "codeloom_checkpoint":
        note = args.get("note")
        text = codeloom.render_checkpoint(root, note)

    elif name == "codeloom_checkpoint_restore":
        text = codeloom.render_checkpoint_restore(root)

    elif name == "codeloom_get_working_state":
        text = codeloom.render_working_state(root, full=True)

    elif name == "codeloom_record_decision":
        title = args.get("title")
        if not title:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'title' argument"}]}
        text = codeloom.wm_decide(root, title, args.get("reason", ""), args.get("status", "accepted"))

    elif name == "codeloom_record_hypothesis":
        title = args.get("title")
        if not title:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'title' argument"}]}
        text = codeloom.wm_hypothesis(root, title, "open")

    elif name == "codeloom_mark_seen":
        items = args.get("items") or []
        if not items:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'items' argument"}]}
        text = codeloom.journal_mark_seen(root, items)

    elif name == "codeloom_list_open_items":
        text = codeloom.list_open_items(root)

    elif name == "codeloom_record_lesson":
        lesson = args.get("lesson")
        if not lesson:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'lesson' argument"}]}
        text = codeloom.memory_lesson(root, lesson)

    elif name == "codeloom_query_memory":
        q = args.get("query")
        if not q:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
        text = codeloom.memory_query(root, q)

    elif name == "codeloom_cognitive_load":
        topic = args.get("topic")
        if not topic:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'topic' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_cognitive_load(f, root, topic)

    elif name == "codeloom_loom":
        task = args.get("task")
        if not task:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'task' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_loom_context(f, root, task, max_files)

    elif name == "codeloom_remember":
        note = args.get("note")
        section = args.get("section", "DECISIONS")
        if not note:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'note' argument"}]}
        text = codeloom.memory_remember(root, section, note)

    elif name == "codeloom_adr":
        title = args.get("title")
        decision = args.get("decision")
        if not title or not decision:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'title'/'decision' arguments"}]}
        text = codeloom.render_adr(root, title, args.get("context", ""), decision,
                                   args.get("status", "Accepted"))

    elif name == "codeloom_adr_list":
        text = codeloom.render_adr_list(root)

    elif name == "codeloom_churn":
        f = _collect_files(root, max_files)
        text = codeloom.git_churn(root, f)

    elif name == "codeloom_hybrid_search":
        query = args.get("query")
        if not query:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_hybrid_search(f, root, query)

    elif name == "codeloom_seen":
        text = codeloom.render_seen(root)

    elif name == "codeloom_cross_repo":
        repos = args.get("repos", [])
        if not repos:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'repos' argument"}]}
        text = codeloom.render_cross_repo(repos, max_files)

    elif name == "codeloom_map":
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
    elif name == "codeloom_check_edit":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codeloom.preflight_check(files, root, symbol, "edit")
    elif name == "codeloom_check_delete":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codeloom.preflight_check(files, root, symbol, "delete")
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
    elif name == "codeloom_embed_search":
        query = args.get("query")
        if not query:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_embed_search(f, root, query)
    elif name == "codeloom_context":
        targets = args.get("targets")
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            return {"isError": True, "content": [{"type": "text", "text": "'targets' must be an array of strings"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_context_card(f, root, targets)
    elif name == "codeloom_answer":
        question = args.get("question")
        if not question:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'question' argument"}]}
        f = _collect_files(root, max_files)
        text = codeloom.render_answer(f, root, question)
    elif name == "codeloom_why":
        q = args.get("query")
        if not q:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
        text = codeloom.render_why(files, root, q)
    elif name == "codeloom_health":
        # speed win: serve from the RESIDENT index + graph (no re-parse).
        # Falls back to a fresh build on the first call for a root.
        try:
            idx = _INDEX.symbols(root, max_files)
            kg = _INDEX.kg(root, max_files)
            text = codeloom.render_health(files, root, index=idx, calls=kg)
        except Exception:
            text = codeloom.render_health(files, root)
    elif name == "codeloom_risk":
        revspec = args.get("revspec") or "HEAD~1..HEAD"
        text = codeloom.render_change_risk(files, root, revspec)
    elif name == "codeloom_pattern":
        pattern = args.get("pattern")
        if not pattern:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'pattern' argument"}]}
        text = codeloom.render_pattern_search(files, root, pattern)
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
    elif name == "codeloom_grep_symbolic":
        query = args.get("query")
        if not query:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'query' argument"}]}
        text = codeloom.render_grep_symbolic(files, root, query)
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
    elif name == "codeloom_precision":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codeloom.render_precision(files, root, symbol)
    elif name == "codeloom_similar":
        symbol = args.get("symbol")
        if not symbol:
            return {"isError": True, "content": [{"type": "text", "text": "missing 'symbol' argument"}]}
        text = codeloom.render_similar(files, root, symbol)
    elif name == "codeloom_deadcode":
        # use the resident in-memory knowledge graph (no re-parse)
        kg_calls = _INDEX.kg(root, max_files)
        text = codeloom.render_deadcode(files, root, calls=kg_calls)
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
# loom:// resources — pull-based working memory for the agent (state, delta,
# hotset, resume). Registered via resources/list + resources/read; no push.
# --------------------------------------------------------------------------- #

RESOURCES: List[Dict[str, Any]] = [
    {
        "uri": "loom://state",
        "name": "codeloom working state",
        "description": "Layered working-state packet: goal, decisions, actions, open items, hot set, persistent memory. Read this first after any compaction.",
        "mimeType": "text/markdown",
    },
    {
        "uri": "loom://delta",
        "name": "codeloom session delta",
        "description": "What changed since the last call: recent git diff + files changed since the previous run (incremental).",
        "mimeType": "text/markdown",
    },
    {
        "uri": "loom://hotset",
        "name": "codeloom hot set",
        "description": "Files/symbols already marked as deeply understood — do NOT re-read these. JSON list.",
        "mimeType": "application/json",
    },
    {
        "uri": "loom://resume",
        "name": "codeloom resume prompt",
        "description": "The compact resume prompt: structural snapshot + working state + session delta, for restoring context after compaction.",
        "mimeType": "text/markdown",
    },
]


def read_resource(uri: str, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Satisfy resources/read for loom:// URIs (pull-based, graceful fallbacks)."""
    root = os.path.abspath(root or ".")
    if uri == "loom://state":
        if hasattr(codeloom, "render_working_state"):
            text = codeloom.render_working_state(root, full=True)
        else:
            text = "# loom://state\nWorking-state packet not yet available in core (render_working_state missing).\n"
    elif uri == "loom://delta":
        if hasattr(codeloom, "render_delta"):
            text = codeloom.render_delta(root)
        else:
            parts = []
            try:
                import subprocess as _sp
                proc = _sp.run(["git", "-C", root, "diff", "--stat", "HEAD"],
                               capture_output=True, text=True, timeout=30)
                if proc.returncode == 0 and proc.stdout.strip():
                    parts.append("# codeloom --delta (git diff vs HEAD)\n" + proc.stdout)
            except Exception:
                pass
            parts.append("# loom://delta\nSession-delta machinery not yet available in core (render_delta missing); showing git diff --stat above if the repo is git-backed.")
            text = "\n".join(parts)
    elif uri == "loom://hotset":
        if hasattr(codeloom, "get_hot_set"):
            import json as _json
            text = _json.dumps({"uri": "loom://hotset", "hotset": codeloom.get_hot_set(root)}, indent=2)
        else:
            text = '{"uri": "loom://hotset", "hotset": [], "note": "hot-set machinery not yet available in core"}'
    elif uri == "loom://resume":
        try:
            files = _collect_files(root, 5000)
            text = codeloom.render_resume(files, root, 5000)
        except Exception as exc:  # noqa: BLE001
            text = f"# loom://resume\nResume prompt not yet available in core: {exc}\n"
    else:
        return {"isError": True, "content": [{"type": "text", "text": f"unknown resource: {uri}"}]}
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
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            })
        elif method == "resources/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"resources": RESOURCES}})
        elif method == "resources/read":
            uri = params.get("uri", "")
            result = read_resource(uri, params.get("root"))
            _send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            result = call_tool(name, args)
            # freshness envelope on every successful response (repowise parity):
            # agents always know index age + commit + staleness before trusting.
            if isinstance(result, dict) and not result.get("isError"):
                try:
                    root = str(args.get("root") or ".")
                    result["_meta"] = codeloom.meta_envelope(root)
                except Exception:
                    pass
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
