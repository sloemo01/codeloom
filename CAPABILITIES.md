# codeloom capabilities

Everything codeloom can do, in one place. Zero-dependency, single file, no daemon.

## Core map

| Command | What it does |
|---|---|
| `codeloom` | Map the repo: tree + per-module one-liners + entry points |
| `codeloom --json` | Machine-readable JSON map for tooling |
| `codeloom --write MAP.md` | Write the map to a file |
| `codeloom --no-outline` | Skip per-file one-liners (faster) |
| `codeloom --max-files N` | Cap traversal (default 20000) |

## Structural intelligence

| Command | What it does |
|---|---|
| `codeloom --graph` | Import dependency graph (Python via `ast`) |
| `codeloom --graph --focus X` | Deps + dependents of module X |
| `codeloom --calls` | Function-level call graph (multi-language) |
| `codeloom --calls --focus X` | Calls inside one module |
| `codeloom --cross` | Cross-file call graph (AST-resolved) |
| `codeloom --framework` | Detect web/app framework + routes/models/config/conventions |

## Search & retrieval

| Command | What it does |
|---|---|
| `codeloom --search X` | Symbol index search (definitions + snippet) |
| `codeloom --usages X` | Where a symbol is used (call sites + snippet) |
| `codeloom --grep QUERY` | Snippet search (ranked + context) |
| `codeloom --read X` | Exact source of a symbol (token-efficient) |
| `codeloom --get-symbol X` | Summary-first retrieval (signature + docstring + call graph) |
| `codeloom --get-symbol X --full` | Full source + call-graph context |
| `codeloom --snippet P S E` | Byte-range extraction |

## Task-orientation (the moat)

| Command | What it does |
|---|---|
| `codeloom --task "text"` | Rank modules relevant to a task |
| `codeloom --impact X` | Predict blast radius of changing X |
| `codeloom --check-edit X` | Is it safe to edit X? (terminal GO/STOP verdict) |
| `codeloom --check-delete X` | Is it safe to delete X? (terminal GO/STOP verdict) |
| `codeloom --plan "text"` | Prioritized reading plan |
| `codeloom --pack "text"` | Single-shot context file (reading order + impact + symbols) |
| `codeloom --resume` | Restore structural context after a compaction (compact snapshot) |

## Understanding & refactoring

| Command | What it does |
|---|---|
| `codeloom --explain X` | Plain-English explanation (no LLM) |
| `codeloom --similar X` | Structurally similar symbols (refactoring) |
| `codeloom --deadcode` | Functions defined but never called |

## Change-aware & runtime

| Command | What it does |
|---|---|
| `codeloom --diff` | Structure of files changed vs git HEAD |
| `codeloom --incremental` | Files changed since last run (hash cache) |
| `codeloom --trace CMD --force` | Runtime call edges (captures dynamic imports) |

## Scale & trust

| Command | What it does |
|---|---|
| `codeloom --index` | Build persistent knowledge graph (symbols + call/import edges) |
| `codeloom --watch` | Incremental daemon-less refresh (re-index only changed files) |
| `codeloom --index-status` | Index freshness |
| `codeloom --verify FILE` | SHA-256 checksum (security) |
| `codeloom --install-grammars --yes` | Install tree-sitter grammars (opt-in precision) |
| `codeloom --auto-grammars` | Scan repo + install grammars for its languages |
| `codeloom --install-agents` | Write AGENTS.md block |
| `codeloom --install-agent claude` | Print MCP config for 17 agents (claude/cursor/codex/gemini/opencode/cline/openhands/devin/hermes/aider/roo/windsurf/amazon-q/jetbrains/junie/kimi/qwen) |
| `codeloom --detect-agent` | Detect which coding agent's config dir is present |

## MCP server (54 tools)

`codeloom-mcp.py` is a zero-dependency MCP server (stdlib JSON-RPC over stdio).
Tools: `codeloom_map`, `codeloom_graph`, `codeloom_focus`, `codeloom_calls`,
`codeloom_diff`, `codeloom_impact`, `codeloom_task`, `codeloom_plan`,
`codeloom_pack`, `codeloom_cross`, `codeloom_search`, `codeloom_usages`,
`codeloom_grep`, `codeloom_read`, `codeloom_explain`, `codeloom_similar`,
`codeloom_deadcode`, `codeloom_get_symbol`, `codeloom_snippet`,
`codeloom_incremental`, `codeloom_verify`, `codeloom_trace`,
`codeloom_ask` (single natural-language entry point), `codeloom_framework`.

## Language support

Broad tree-sitter precision via `--install-grammars --yes` or repo-aware
`--auto-grammars` (installs grammars for the languages your repo actually
uses). Core grammars: Python, JS/TS (incl. TSX), Go, Rust, Java, C/C++, C#,
Ruby, PHP, Swift, Kotlin, Dart, Lua, Bash, Elixir, OCaml, Scala, Haskell, Zig,
Perl, F#, PowerShell. Plus 130+ extensions via regex/C extraction (every
language gets the same structural extraction + cross-file resolution, no
per-language setup) — with a data-driven tree-sitter resolver that uses real
AST depth for ANY grammar you install, falling back to regex otherwise.

## Optional C engine (scale)

Build once: `cc -O3 -o codeloom_core codeloom_core.c`, then `--index --engine c`
uses a compiled C scanner for the high-volume file walk + symbol + call + import
extraction. Measured on the Linux kernel (~28M LOC, 64,814 code files): full
knowledge graph + symbol index in **~91s** — 3.2M symbols, 408k edges. The
pure-Python default stays zero-dependency; `--engine c` is the opt-in
accelerator for huge monorepos.
