# codeloom CLI reference

Every command, flag, and argument. Run `codeloom --help` for the live list.

## Usage

```
codeloom [OPTIONS] [ROOT]
```

`ROOT` defaults to the current directory.

## Core

| Flag | Description |
|---|---|
| `(none)` | Map the repo: tree + one-liners + entry points |
| `--json` | Machine-readable JSON map |
| `--write FILE` | Write the map to FILE |
| `--no-outline` | Skip per-file one-liners (faster) |
| `--max-files N` | Cap traversal (default 5000) |
| `--version` | Print version |

## Structural

| Flag | Description |
|---|---|
| `--graph` | Import dependency graph |
| `--graph --focus X` | Deps + dependents of module X |
| `--calls` | Function-level call graph |
| `--calls --focus X` | Calls inside one module |
| `--cross` | Cross-file call graph (AST-resolved) |
| `--framework` | Detect framework + routes/models/config/conventions |

## Search & retrieval

| Flag | Description |
|---|---|
| `--search X` | Symbol index search |
| `--usages X` | Where a symbol is used |
| `--grep QUERY` | Snippet search |
| `--read X` | Exact source of a symbol |
| `--get-symbol X` | Summary-first retrieval |
| `--get-symbol X --full` | Full source + call context |
| `--snippet P S E` | Byte-range extraction |

## Task-orientation

| Flag | Description |
|---|---|
| `--task "text"` | Rank modules relevant to a task |
| `--impact X` | Blast radius of changing X |
| `--plan "text"` | Prioritized reading plan |
| `--pack "text"` | Single-shot context file |
| `--resume` | Restore structural context after a compaction (compact snapshot) |

## Understanding

| Flag | Description |
|---|---|
| `--explain X` | Plain-English explanation (no LLM) |
| `--similar X` | Structurally similar symbols |
| `--deadcode` | Functions defined but never called |

## Change-aware & runtime

| Flag | Description |
|---|---|
| `--diff` | Structure of files changed vs git HEAD |
| `--incremental` | Files changed since last run |
| `--trace CMD --force` | Runtime call edges |

## Scale & trust

| Flag | Description |
|---|---|
| `--index` | build + save a persistent knowledge graph (symbols + call/import edges) |
| `--watch` | incremental daemon-less refresh: re-index only changed files |
| `--index-status` | Index freshness |
| `--verify FILE` | SHA-256 checksum |
| `--install-grammars --yes` | Install tree-sitter grammars (25 languages) |
| `--install-agents` | Write AGENTS.md block |
| `--force` | Acknowledge `--trace` executes code |
