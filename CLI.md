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
| `--max-files N` | Cap traversal (default 20000) |
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
| `--resume` | restore structural context after a compaction (compact snapshot) |
| `--checkpoint "note"` | snapshot in-progress work (git diff + status note) to survive compaction |
| `--checkpoint-restore` | read the last checkpoint back to resume in-progress work |

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
| `codeloom --index --engine c` | use the compiled C accelerator (build: `cc -O3 -o codeloom_core codeloom_core.c`) — Linux kernel ~91s |
| `codeloom --watch-core ROOT` | Native C file watcher (kqueue/inotify): print changed code files live |
| `codeloom --serve ROOT` | C-resident index server: sub-ms symbol lookups (no Python per query) |
| `--watch` | incremental daemon-less refresh: re-index only changed files |
| `--index-status` | Index freshness |
| `--verify FILE` | SHA-256 checksum |
| `--install-grammars --yes` | Install tree-sitter grammars (opt-in precision) |
| `--auto-grammars` | Scan the repo + install grammars for its languages (beats the manual per-language step) |
| `--install-agents` | Write AGENTS.md block |
| `--force` | Acknowledge `--trace` executes code |

## Context engine & intelligence

| Flag | Description |
|---|---|
| `--loom "task"` | Intent engine: layered context (overview→files→code→git→memory) |
| `--remember "note" --section X` | Append to persistent repository memory |
| `--churn` | Git intelligence: most-edited files (instability signal) |
| `--hybrid-search "q"` | Hybrid search (BM25 + structure + git scored) |
| `--seen` / `--dedup` | Session memory: already-read files/symbols |
| `--cross-repo A B ...` | One graph across multiple repos |
| `--architecture` | Detect architectural pattern |
| `--heatmap` | Dependency heatmap (god classes, circular imports, unused) |
| `--explain-topic X` | Explain a domain end-to-end |
| `--routes` | Extract HTTP routes: METHOD path → handler (framework-aware) |
| `--channels` | Pub-sub/event channel map (EMITS → LISTENS_ON) |
| `--export FILE` | Export a portable graph snapshot |
| `--docs readme\|arch` | Generate a README or ARCHITECTURE doc |
| `codeloom --refactor X` | Refactor engine: files, deps, risk, order for symbol X |
| `codeloom --rename OLD NEW` | What a rename touches: definitions, files, dependents, edges |
| `codeloom --ask "task"` | One-shot complete task brief: loom + blast radius + files-to-touch checklist |
| `--bug-predict` | Files likely to break (churn+coupling+complexity) |
| `--timeline` | Repository timeline via git |
| `--find "q"` | Natural-language flow discovery |
| `--files "glob"` | Find files by name/glob ("engine" or "*.py") |
| `--context-diff BASE HEAD` | Branch-to-branch architecture diff |
| `--plugin-sdk` / `--lsp` | Plugin surface / LSP bridge status |
| `--graph-html` | Write local zoomable HTML graph view |
| `codeloom --check-edit X` / `--check-delete X` | Preflight refactoring safety verdicts |
| `codeloom --install-agent claude` | Print MCP config for 17 agents (claude/cursor/codex/gemini/opencode/cline/openhands/devin/hermes/aider/roo/windsurf/amazon-q/jetbrains/junie/kimi/qwen) |
| `codeloom --detect-agent` | Detect which coding agent's config dir is present |
| `codeloom --langs` | List supported languages/extensions (130+ regex + tree-sitter set) |
| `codeloom --build-core` | Build the optional C accelerator (cc -O3 codeloom_core.c) if absent |
| `codeloom --precision X` | Graph precision: call edges with confidence + class relationships |
