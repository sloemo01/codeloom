# codeloom capabilities

Everything codeloom can do, in one place. Zero-dependency, single file, no daemon.

## Core map

| Command | What it does |
|---|---|
| `codeloom` | Map the repo: tree + per-module one-liners + entry points |
| `codeloom --json` | Machine-readable JSON map for tooling |
| `codeloom --write MAP.md` | Write the map to a file |
| `codeloom --no-outline` | Skip per-file one-liners (faster) |
| `codeloom --max-files N` | Cap traversal (default 5000) |

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
| `codeloom --install-grammars --yes` | Install tree-sitter grammars (25 languages) |
| `codeloom --install-agents` | Write AGENTS.md block |

## MCP server (24 tools)

`codeloom-mcp.py` is a zero-dependency MCP server (stdlib JSON-RPC over stdio).
Tools: `codeloom_map`, `codeloom_graph`, `codeloom_focus`, `codeloom_calls`,
`codeloom_diff`, `codeloom_impact`, `codeloom_task`, `codeloom_plan`,
`codeloom_pack`, `codeloom_cross`, `codeloom_search`, `codeloom_usages`,
`codeloom_grep`, `codeloom_read`, `codeloom_explain`, `codeloom_similar`,
`codeloom_deadcode`, `codeloom_get_symbol`, `codeloom_snippet`,
`codeloom_incremental`, `codeloom_verify`, `codeloom_trace`,
`codeloom_ask` (single natural-language entry point), `codeloom_framework`.

## Language support

25 languages via `--install-grammars --yes`: Python, JS/TS, Go, Rust, Java,
C/C++, C#, Ruby, PHP, Swift, Kotlin, Dart, Lua, bash, Elixir, OCaml, Scala,
Haskell, Zig, Perl, F#, PowerShell. Plus regex fallback for 18+ more.
