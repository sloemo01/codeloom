---
name: codemap
description: "Use and maintain codemap: map, graph, calls, diff, MCP."
version: 0.1.0
author: Nehal (sloemo01), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [codemap, codebase, ai-agents, mcp, structural-intelligence]
    related_skills: [community-signal-research, deep-web-research]
---

# codemap Skill

codemap is a single-file, zero-dependency, no-daemon tool that gives AI coding
agents a structural model of a repo (tree + outlines + import graph + call graph
+ git diff) in under a second. This skill covers how to run it, wire it into
agents via MCP, and maintain/extend the repo.

## When to Use

- User asks to map a codebase, trace imports, or see what changed in a repo.
- User asks to wire codemap into Claude Code / Cursor / Codex / Gemini (MCP).
- User asks to extend codemap, run its tests, or re-record its demo GIF.
- Don't use for: general codebase questions that don't need structural output.

## Prerequisites

- Python 3.8+ (stdlib only — no pip deps).
- Repo files: `codemap.py`, `codemap-mcp.py`, `tests.py`, `demo.tape`,
  `README.md`, `LAUNCH.md`.
- `codemap` symlinked to `~/bin/codemap` (macOS/Linux) for bare-command use.
- `vhs` (charmbracelet) installed via `brew install vhs` to re-record the GIF.

## How to Run

```bash
# Map a repo (tree + outlines + entry points)
python3 codemap.py /path/to/repo

# Import dependency graph
python3 codemap.py --graph /path/to/repo

# Focus one module: what it needs + what needs it
python3 codemap.py --graph --focus core.engine /path/to/repo

# Function-level call graph (multi-language)
python3 codemap.py --calls --focus core.engine /path/to/repo

# Git-aware: structure of files changed vs HEAD
python3 codemap.py --diff /path/to/repo

# One-command agent setup
python3 codemap.py --install-agents /path/to/repo

# Token-cost estimate
python3 codemap.py --cost /path/to/repo

# Task-aware: rank modules relevant to a task
python3 codemap.py --task "fix the login bug" /path/to/repo

# Change-impact: predict blast radius of changing a module
python3 codemap.py --impact core/engine.py /path/to/repo

# Agent-native reading plan for a task
python3 codemap.py --plan "add retry to engine" /path/to/repo

# Cross-file call graph (resolved across modules)
python3 codemap.py --cross /path/to/repo

# Search the symbol index
python3 codemap.py --search Engine /path/to/repo

# Find where a symbol is used (call sites + snippet)
python3 codemap.py --usages retry /path/to/repo

# Snippet search (find exact code, ranked + context)
python3 codemap.py --grep "retry" /path/to/repo

# Token-efficient read (exact symbol source via AST / tree-sitter / brace-match)
python3 codemap.py --read Engine /path/to/repo

# Plain-English explanation of a symbol (no LLM)
python3 codemap.py --explain Engine /path/to/repo

# Find structurally similar functions/classes (refactoring)
python3 codemap.py --similar run /path/to/repo

# Find dead code (defined but never called)
python3 codemap.py --deadcode /path/to/repo

# Token-counted symbol snippet (byte offsets + token estimate)
python3 codemap.py --get-symbol Engine /path/to/repo

# Byte-range snippet extraction
python3 codemap.py --snippet src/core/engine.py 0 60 /path/to/repo

# Incremental mode (hash-based cache, no daemon)
python3 codemap.py --incremental /path/to/repo

# Security check (SHA-256)
python3 codemap.py --verify codemap.py

# Runtime trace (captures dynamic imports/monkeypatching; needs --force)
python3 codemap.py --trace tests.py /path/to/repo --force

# Install tree-sitter grammars (opt-in precision)
python3 codemap.py --install-grammars

# Build a persistent byte-offset index (scale without a daemon)
python3 codemap.py --index /path/to/repo

# Check index freshness
python3 codemap.py --index-status /path/to/repo
```

## Quick Reference

| Flag | Purpose |
|---|---|
| `(none)` | tree + per-module one-liners + entry points |
| `--graph` | import dependency graph (multi-language) |
| `--graph --focus X` | deps + dependents of module X |
| `--calls` | function-level call graph (multi-language) |
| `--calls --focus X` | calls inside one module |
| `--diff` | structure of files changed vs git HEAD |
| `--install-agents` | write/update AGENTS.md with a codemap block |
| `--cost` | append token-cost estimate |
| `--task "text"` | rank modules relevant to a task |
| `--impact X` | predict blast radius of changing module X |
| `--plan "text"` | prioritized reading plan for a task |
| `--cross` | cross-file call graph (AST-resolved) |
| `--search X` | search the symbol index (definitions + snippet) |
| `--usages X` | find where a symbol is used (call sites + snippet) |
| `--grep X` | search file contents for a snippet (ranked + context) |
| `--read X` | extract exact source of a symbol (AST / tree-sitter / brace-match) |
| `--explain X` | plain-English explanation of a symbol (no LLM) |
| `--similar X` | find structurally similar functions/classes (refactoring) |
| `--deadcode` | find functions defined but never called |
| `--get-symbol X` | token-counted symbol snippet (byte offsets + tokens) |
| `--snippet P S E` | extract bytes S-E from file P |
| `--incremental` | files changed since last run (hash cache) |
| `--verify FILE` | print SHA-256 of a file |
| `--trace CMD` | run a command, record runtime call edges (needs --force) |
| `--force` | acknowledge --trace executes code (isolation warning) |
| `--install-grammars` | install tree-sitter grammars (opt-in precision) |
| `--index` | build + save a persistent byte-offset index (scale) |
| `--index-status` | show persistent index status/freshness |
| `--json` | machine-readable JSON |
| `--write FILE` | write map to FILE |
| `--no-outline` | skip per-file one-liners (faster) |
| `--max-files N` | cap traversal (default 5000) |

**Optional precision backends** (auto-enabled when present, zero-dep otherwise):
- `tree-sitter` + grammars → precise multi-language AST parsing
- `CODEmap_EMBED_BASE_URL`/`CODEmap_EMBED_API_KEY` or `sentence-transformers` → semantic task scoring
- `--trace` → runtime call edges (static blind spots)

`--focus` accepts a file path, package dir, or dotted module name
(`browser_use/agent`, `browser_use/agent/service.py`, `agent.service`).

## Procedure

### 1. Map a repo for an agent
1. Run `codemap <root>` (or `--graph`, `--calls`, `--diff` as needed).
2. Confirm output shows the tree/outlines/graph and a file count.
3. Point the agent at the output, or write it to `AGENTS.md` via `--install-agents`.

### 2. Wire into an agent via MCP
1. Register `codemap-mcp.py` in the agent's MCP config:
   ```json
   { "codemap": { "command": "python3", "args": ["/path/to/codemap-mcp.py"] } }
   ```
   (Windows: `"command": "python"`.)
2. Verify with a smoke test:
   ```bash
   printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
     | python3 codemap-mcp.py
   ```
   Expect `serverInfo` with name `codemap-mcp`.
3. Tools exposed: `codemap_map`, `codemap_graph`, `codemap_focus`,
   `codemap_calls`, `codemap_diff`, `codemap_impact`, `codemap_task`,
   `codemap_plan`, `codemap_cross`, `codemap_search`, `codemap_usages`,
   `codemap_grep`, `codemap_read`, `codemap_explain`, `codemap_similar`,
   `codemap_deadcode`, `codemap_get_symbol`, `codemap_snippet`,
   `codemap_incremental`, `codemap_verify`, `codemap_trace`.

### 3. Run the test suite
```bash
python3 tests.py
```
Expect `OK` (currently 11 tests). Add tests for any new feature.

### 4. Re-record the demo GIF
1. Edit `demo.tape` to showcase the features you want (map, graph+focus, calls, diff).
2. Ensure the demo repo (`demo-repo/`) is a git repo with a committed baseline
   and a real change so `--diff` has output.
3. Run `vhs demo.tape` (with `~/bin` on PATH so `codemap` resolves).
4. Verify a late frame with `vision_analyze` (extract via ffmpeg) before committing.

### 5. Extend codemap
- New CLI flag: add to `argparse` in `main()`, implement the logic, add a test.
- New MCP tool: add to `TOOLS` list + a branch in `call_tool()` in `codemap-mcp.py`.
- Bump `VERSION` in `codemap.py` and `SERVER_VERSION` in `codemap-mcp.py`.
- Update `README.md` (usage, feature table, roadmap) and `LAUNCH.md` if launch copy changes.

## Pitfalls

- `--diff` requires the target to be a git repo with a committed baseline;
  otherwise it reports "No changes vs HEAD."
- `--calls` uses lightweight regex (not tree-sitter) — less precise than
  codebase-memory-mcp but zero-dep. Good enough for structural understanding.
- The call graph filters to repo-defined functions only; builtins/stdlib are
  dropped as noise.
- `--focus` suffix-matches dotted names (e.g. `core.engine` → `src.core.engine`).
- The current session's skill loader is cached — a newly created skill isn't
  visible until a new session.
- Windows: use `python` not `python3`; paths use `\`.

## Verification

- `python3 tests.py` → `OK` (38 tests).
- `codemap --graph --focus <module> <root>` returns `depends_on`/`depended_on_by`.
- `codemap --impact <module> <root>` returns `risk` + `Direct dependents`.
- `codemap --task "text" <root>` returns a ranked module list.
- `codemap --cross <root>` returns cross-file call edges.
- `codemap --search <symbol> <root>` returns symbol locations + snippet.
- `codemap --usages <symbol> <root>` returns call sites + snippet.
- `codemap --grep <query> <root>` returns ranked snippet matches.
- `codemap --read <symbol> <root>` returns exact symbol source.
- `codemap --explain <symbol> <root>` returns a summary + call graph.
- `codemap --similar <symbol> <root>` returns refactoring candidates.
- `codemap --deadcode <root>` returns unused symbols.
- `codemap --get-symbol <symbol> <root>` returns byte offsets + token count.
- `codemap --snippet <path> <start> <end> <root>` returns a byte-range snippet.
- `codemap --incremental <root>` returns changed files (hash cache).
- `codemap --trace <cmd> <root>` returns runtime call edges (or none).
- MCP server keeps an in-memory index (incremental, always fresh).
- MCP smoke test returns `serverInfo` name `codemap-mcp`.
- `demo.gif` exists and a late frame shows the intended feature output.
