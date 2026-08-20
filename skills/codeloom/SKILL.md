---
name: codeloom
description: "Use and maintain codeloom: map, graph, calls, diff, MCP."
version: 0.1.0
author: Nehal (sloemo01), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [codeloom, codebase, ai-agents, mcp, structural-intelligence]
    related_skills: [community-signal-research, deep-web-research]
---

# codeloom Skill

codeloom is a single-file, zero-dependency, no-daemon tool that gives AI coding
agents a structural model of a repo (tree + outlines + import graph + call graph
+ git diff) in under a second. This skill covers how to run it, wire it into
agents via MCP, and maintain/extend the repo.

## When to Use

- User asks to map a codebase, trace imports, or see what changed in a repo.
- User asks to wire codeloom into Claude Code / Cursor / Codex / Gemini (MCP).
- User asks to extend codeloom, run its tests, or re-record its demo GIF.
- Don't use for: general codebase questions that don't need structural output.

## Prerequisites

- Python 3.8+ (stdlib only — no pip deps).
- Repo files: `codeloom.py`, `codeloom-mcp.py`, `tests.py`, `demo.tape`,
  `README.md`, `LAUNCH.md`.
- `codeloom` symlinked to `~/bin/codeloom` (macOS/Linux) for bare-command use.
- `vhs` (charmbracelet) installed via `brew install vhs` to re-record the GIF.

## How to Run

```bash
# Map a repo (tree + outlines + entry points)
python3 codeloom.py /path/to/repo

# Import dependency graph
python3 codeloom.py --graph /path/to/repo

# Focus one module: what it needs + what needs it
python3 codeloom.py --graph --focus core.engine /path/to/repo

# Function-level call graph (multi-language)
python3 codeloom.py --calls --focus core.engine /path/to/repo

# Git-aware: structure of files changed vs HEAD
python3 codeloom.py --diff /path/to/repo

# One-command agent setup
python3 codeloom.py --install-agents /path/to/repo

# Token-cost estimate
python3 codeloom.py --cost /path/to/repo

# Task-aware: rank modules relevant to a task
python3 codeloom.py --task "fix the login bug" /path/to/repo

# Change-impact: predict blast radius of changing a module
python3 codeloom.py --impact core/engine.py /path/to/repo

# Agent-native reading plan for a task
python3 codeloom.py --plan "add retry to engine" /path/to/repo

# Cross-file call graph (resolved across modules)
python3 codeloom.py --cross /path/to/repo

# Search the symbol index
python3 codeloom.py --search Engine /path/to/repo

# Find where a symbol is used (call sites + snippet)
python3 codeloom.py --usages retry /path/to/repo

# Snippet search (find exact code, ranked + context)
python3 codeloom.py --grep "retry" /path/to/repo

# Token-efficient read (exact symbol source via AST / tree-sitter / brace-match)
python3 codeloom.py --read Engine /path/to/repo

# Plain-English explanation of a symbol (no LLM)
python3 codeloom.py --explain Engine /path/to/repo

# Find structurally similar functions/classes (refactoring)
python3 codeloom.py --similar run /path/to/repo

# Find dead code (defined but never called)
python3 codeloom.py --deadcode /path/to/repo

# Token-counted symbol snippet (summary-first by default; --full for source)
python3 codeloom.py --get-symbol Engine /path/to/repo
python3 codeloom.py --get-symbol Engine --full /path/to/repo

# Single-shot context packing (the moat feature: reading order + impact + symbols)
python3 codeloom.py --pack "add retry logic" /path/to/repo

# Incremental mode (hash-based cache, no daemon)
python3 codeloom.py --incremental /path/to/repo

# Security check (SHA-256)
python3 codeloom.py --verify codeloom.py

# Runtime trace (captures dynamic imports/monkeypatching; needs --force)
python3 codeloom.py --trace tests.py /path/to/repo --force

# Install tree-sitter grammars (opt-in precision)
python3 codeloom.py --install-grammars

# Build a persistent byte-offset index (scale without a daemon)
python3 codeloom.py --index /path/to/repo

# Check index freshness
python3 codeloom.py --index-status /path/to/repo
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
| `--install-agents` | write/update AGENTS.md with a codeloom block |
| `--cost` | append token-cost estimate |
| `--task "text"` | rank modules relevant to a task |
| `--impact X` | predict blast radius of changing X |
| `--check-edit X` | preflight: is it safe to edit X? (terminal GO/CHECK/STOP) |
| `--check-delete X` | preflight: is it safe to delete X? (terminal GO/STOP) |
| `--plan "X"` | prioritized reading plan for a task |
| `--pack "X"` | single-shot context file (reading order + impact + symbols) |
| `--cross` | cross-file call graph (AST-resolved) |
| `--search X` | search the symbol index (definitions + snippet) |
| `--usages X` | find where a symbol is used (call sites + snippet) |
| `--grep X` | search file contents for a snippet (ranked + context) |
| `--read X` | extract exact source of a symbol (AST / tree-sitter / brace-match) |
| `--explain X` | plain-English explanation of a symbol (no LLM) |
| `--similar X` | find structurally similar functions/classes (refactoring) |
| `--deadcode` | find functions defined but never called |
| `--get-symbol X` | token-counted symbol snippet (summary-first by default) |
| `--get-symbol X --full` | full source (opt-in) |
| `--snippet P S E` | extract bytes S-E from file P |
| `--incremental` | files changed since last run (hash cache) |
| `--verify FILE` | print SHA-256 of a file |
| `--trace CMD` | run a command, record runtime call edges (needs --force) |
| `--force` | acknowledge --trace executes code (isolation warning) |
| `--install-grammars` | install tree-sitter grammars (opt-in precision) |
| `--install-grammars --yes` | actually run pip install (one-command) |
| `--index` | build + save a persistent knowledge graph (symbols + call/import edges) |
| `--index-status` | show persistent index status/freshness |
| `--framework` | detect the web/app framework + surface routes/models/config/conventions |
| `--session` | log this invocation to the local session log (JSONL) |
| `--session-report` | summarize the local session log (calls, tokens, cost) |
| `--json` | machine-readable JSON |
| `--write FILE` | write map to FILE |
| `--no-outline` | skip per-file one-liners (faster) |
| `--max-files N` | cap traversal (default 5000) |

**Optional precision backends** (auto-enabled when present, zero-dep otherwise):
- `tree-sitter` + grammars → precise multi-language AST parsing
- `CODELOOM_EMBED_BASE_URL`/`CODELOOM_EMBED_API_KEY` or `sentence-transformers` → semantic task scoring
- `--trace` → runtime call edges (static blind spots)

`--focus` accepts a file path, package dir, or dotted module name
(`browser_use/agent`, `browser_use/agent/service.py`, `agent.service`).

## Procedure

### 1. Map a repo for an agent
1. Run `codeloom <root>` (or `--graph`, `--calls`, `--diff` as needed).
2. Confirm output shows the tree/outlines/graph and a file count.
3. Point the agent at the output, or write it to `AGENTS.md` via `--install-agents`.

### 2. Wire into an agent via MCP
1. Register `codeloom-mcp.py` in the agent's MCP config:
   ```json
   { "codeloom": { "command": "python3", "args": ["/path/to/codeloom-mcp.py"] } }
   ```
   (Windows: `"command": "python"`.)
2. Verify with a smoke test:
   ```bash
   printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
     | python3 codeloom-mcp.py
   ```
   Expect `serverInfo` with name `codeloom-mcp`.
3. Tools exposed: `codeloom_map`, `codeloom_graph`, `codeloom_focus`,
   `codeloom_calls`, `codeloom_diff`, `codeloom_impact`, `codeloom_task`,
   `codeloom_plan`, `codeloom_pack`, `codeloom_cross`, `codeloom_search`,
   `codeloom_usages`, `codeloom_grep`, `codeloom_read`, `codeloom_explain`,
   `codeloom_similar`, `codeloom_deadcode`, `codeloom_get_symbol`,
   `codeloom_snippet`, `codeloom_incremental`, `codeloom_verify`,
   `codeloom_trace`, and `codeloom_ask` (single natural-language entry point
   that routes deterministically — the agent never picks among tools).

### 3. Run the test suite
```bash
python3 tests.py
```
Expect `OK` (currently 11 tests). Add tests for any new feature.

### 4. Re-record the demo GIF
1. Edit `demo.tape` to showcase the features you want (map, graph+focus, calls, diff).
2. Ensure the demo repo (`demo-repo/`) is a git repo with a committed baseline
   and a real change so `--diff` has output.
3. Run `vhs demo.tape` (with `~/bin` on PATH so `codeloom` resolves).
4. Verify a late frame with `vision_analyze` (extract via ffmpeg) before committing.

### 5. Extend codeloom
- New CLI flag: add to `argparse` in `main()`, implement the logic, add a test.
- New MCP tool: add to `TOOLS` list + a branch in `call_tool()` in `codeloom-mcp.py`.
- Bump `VERSION` in `codeloom.py` and `SERVER_VERSION` in `codeloom-mcp.py`.
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

- `python3 tests.py` → `OK` (45 tests).
- `codeloom --graph --focus <module> <root>` returns `depends_on`/`depended_on_by`.
- `codeloom --impact <module> <root>` returns `risk` + `Direct dependents`.
- `codeloom --task "text" <root>` returns a ranked module list.
- `codeloom --cross <root>` returns cross-file call edges.
- `codeloom --search <symbol> <root>` returns symbol locations + snippet.
- `codeloom --usages <symbol> <root>` returns call sites + snippet.
- `codeloom --grep <query> <root>` returns ranked snippet matches.
- `codeloom --read <symbol> <root>` returns exact symbol source.
- `codeloom --explain <symbol> <root>` returns a summary + call graph.
- `codeloom --similar <symbol> <root>` returns refactoring candidates.
- `codeloom --deadcode <root>` returns unused symbols.
- `codeloom --get-symbol <symbol> <root>` returns byte offsets + token count.
- `codeloom --snippet <path> <start> <end> <root>` returns a byte-range snippet.
- `codeloom --incremental <root>` returns changed files (hash cache).
- `codeloom --trace <cmd> <root>` returns runtime call edges (or none).
- MCP server keeps an in-memory index (incremental, always fresh).
- MCP smoke test returns `serverInfo` name `codeloom-mcp`.
- `demo.gif` exists and a late frame shows the intended feature output.
