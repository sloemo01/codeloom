# CodeLoom — Feature Map (The AI Operating System for Repositories)

The strategic reframe: **codeloom is not "another MCP code-graph." It is the
fastest context engine for coding agents** — one intelligent entry point that
internally decides what to read and returns layered context, backed by
repository memory and git intelligence.

This map tracks every feature from the strategic vision, its status, and the
design. Statuses: ✅ shipped · 🚧 in progress · ⬜ planned.

---

## The Keystone

### 1. Intent Engine — `loom_context(task)` ⭐⭐⭐⭐⭐ — 🚧
**Status: partial — `codeloom_ask` routes; `loom_context` orchestrates layered context.**

Instead of exposing 78 tools, expose **ONE**: `loom_context(task)`. Given "fix
the auth bug", CodeLoom internally decides search → graph → embeddings → git →
tests → docs, then returns **layered context**, not one tool's output.

```
user "fix the auth bug"
  → Overview (map, entry points)
  → Important files (edit-relevance)
  → Relevant symbols (get-symbol)
  → Exact code (pack/read, capped)
  → Git context (churn, recent commits on those files)
  → Memory (architecture notes, past decisions on auth)
```

### 2. Layered Context ✅
Don't always send full functions. Return a pyramid:
```
Overview → Important files → Relevant symbols → Exact code
```
Agents waste tokens reading unnecessary code; layered context lets them
descend only as deep as they need. (This is what `--pack` + `--resume` start
to do; `loom_context` unifies it.)

---

## Repository Memory

### 3. Repository Memory 🚧 (building)
Persistent store of: architecture decisions, patterns, common fixes, preferred
style, naming, review comments, past conversations. When asked "how do we build
APIs?" return current code + architecture notes + recent commits + docs +
related discussions — not just code.

- **Design:** `.codeloom-memory/` with `ARCHITECTURE.md`, `DECISIONS.md`,
  `PATTERNS.md`, `CONVENTIONS.md`. Auto-extracted from docs/AGENTS.md/README at
  index time; appended via `codeloom --remember "decision: use X"`.

### 4. Live File Watching 🚧
Every save → incremental AST update → incremental graph update → incremental
embeddings. No full reindex.
- **Status:** ✅ `--watch` does incremental symbol refresh. Embedding increment
  is ⬜ (depends on optional embedding backend).

### 5. Git Intelligence ⭐⭐⭐⭐⭐ 🚧
Nobody does this well. Use git history as first-class data:
- Why was this changed? (commit messages on a file)
- Who introduced this bug? (blame)
- Most unstable file / most edited function (churn)
- Hot paths, ownership graph
- **Status:** `--diff` (git diff structure) ✅. Churn/blame/ownership 🚧.

---

## Graph & Search

### 6. Architecture Detection ✅
Auto-detect MVC / Clean Architecture / DDD / Hexagonal / Microservices /
Feature-first / Layered / Monolith, then expose it to the agent.
(`--architecture` ✅: detects pattern + scope from repo layout + top-level
structure.)

### 7. Dependency Heatmap ✅
Find: god classes, circular imports, dead packages, unused modules, high
coupling, hidden dependencies. (`--heatmap` ✅: god/hub classes + circular
imports + unused modules.)

### 8. Semantic + Graph Search ✅
Combine BM25 + embeddings + AST + graph traversal + git history, scored
together. (`--hybrid-search` ✅: BM25 + symbol kind/size + git churn scored
together; embedding signal ⬜ via optional backend; full graph-traversal signal ⬜.)

### 9. Agent Session Memory ✅
Remember already-read files, explored symbols, cached answers, previous
searches — avoid rereading. (`--session` logs ✅; `--seen` reports ✅; `--dedup`
skips already-read files ✅; `dedupe_symbols()` suppresses seen symbols inside a
single pack/loom response ✅.)

### 10. Code Compression ✅
Instead of full `function...`, return: signature, params, returns, deps,
callers, risk. Only send source if needed. (This is the adaptive `--get-symbol`
+ summary-first design, already shipped.)

---

## Intelligence & DX

### 11. Auto Documentation ✅
Generate README, architecture docs, API docs, DB docs, sequence diagrams,
dependency diagrams — continuously. (`--docs readme|arch` ✅ generates README
or ARCHITECTURE docs from structure; API/DB/diagrams ⬜.)

### 12. Refactor Engine ✅
Ask "refactor auth" → returns files, dependencies, tests affected, risk, order.
(`--refactor X` ✅ orchestrates impact + preflight into files/deps/risk/order.)

### 13. Multi-Repository Knowledge ✅
One graph across frontend / backend / SDK / CLI / docs — cross-repo navigation.
(`--cross-repo A B ...` builds a combined graph across multiple roots with
per-repo modules + service-to-service edges ✅.)

### 14. Agent Benchmark Mode ✅
Measure tool calls, tokens, latency, cache hits, context size. Publish
benchmarks. (`benchmarks/` does token efficiency on the jcodemunch corpus;
`--session-report` measures tokens/calls/cost live.)

### 15. Plugin SDK ⭐⭐⭐⭐⭐ ✅
Let others build Next.js / React / Laravel / Django / Rust / Unity / Godot
plugins — framework-aware extraction. (`--plugin-sdk` ✅ documents the hook
surface; concrete per-framework plugins ⬜.)

### 16. Language Server Integration ✅
Use LSP / TS server / Rust Analyzer / Pyright / Go LS where it beats static
parsing — don't reinvent everything. (`--lsp` ✅ detects installed servers for
optional semantic enrichment; codeloom stays zero-dep — LSP is never required.)

### 17. Background Worker 🚧
Separate parser / embeddings / graph / watcher / MCP / search — multi-threaded.
(`--parallel` gives multiprocessing for index/graph ✅; a persistent worker is
intentionally not a "daemon" by design — the no-daemon moat.)

### 18. Rust Core ⬜
If/when raw parsing speed matters more than the single-file stdlib moat,
port the indexing/search engine to Rust. **Tradeoff:** breaks the one-file
zero-dep identity — a deliberate decision, not automatic.

---

## Explainability & UX

### 19. Visual Graph ✅
Web UI: functions, imports, packages, calls, dependencies, architecture —
zoomable. (`--graph-html` ✅ writes a local self-contained zoomable HTML graph
of imports/calls; built-in, no daemon — open in a browser.)

### 20. Explain Mode ✅
Ask "explain authentication" → flow diagram + relevant files + call chain +
DB + middleware + routes, automatically. (`--explain-topic X` ✅ returns
relevant files + call flow for a domain; `--explain` ✅ per-symbol.)

---

## Killer Features (Nobody Has)

### 21. Execution Graph 🚧
Combine static analysis + runtime traces + test coverage. (`--trace` ✅ gives
runtime edges; merging with static call graph + coverage ⬜.)

### 22. AI Context Cache 🚧
Cache previous context requests so repeated prompts return almost instantly.
(`--resume` + persistent index + `--watch` = the seed; keyed cache ⬜.)

### 23. Repository Timeline ✅
Replay architecture evolution over time (via git history + memory).
(`--timeline` ✅ replays commits; architecture-level diff ⬜.)

### 24. Bug Prediction ✅
Identify files likely to break by churn + complexity + coupling.
(`--bug-predict` ✅ scores files by churn + coupling + complexity.)

### 25. Natural-Language API ✅
"Find where login starts", "Show every payment flow", "Explain how uploads
work" — the `loom_context` intent engine is the foundation.
(`--find` ✅ does flow discovery for a query; `--loom` ✅ layered context.)

### 26. Context Diff ✅
Compare two branches at the architecture level, not just line-by-line.
(`--context-diff BASE HEAD` ✅ maps changed files to modules + counts
architecture-level change.)

### 27. Knowledge Packs ✅
Export compact, reusable project summaries for agents. (`--resume` + `--pack`
are exactly this; `--pack` exports a task brief, `--resume` exports the map.)

### 28. Compaction Survival ✅
The agent's work survives a context wipe. (`--resume` restores the map,
`--remember` persists conclusions, `--seen` tracks explored files,
`--checkpoint` snapshots in-progress work, `--adr` records decisions — all as
files in the repo, so a wiped agent resumes mid-work, not from zero.)

### 29. Single-Entry-Point Router ✅
`codeloom_ask` routes deterministically across the entire tool surface — the
agent's effective surface is **1 tool** (~50 tokens) instead of 78 schemas
(~3,000 tokens). Token efficiency + precision + full coverage.

### 30. Integrated C Engine + LSP ✅
`codeloom_core.c` auto-builds on first use (no manual step, no download); LSP
auto-detects + auto-starts an installed server for real cross-file resolution.

---

## Priority order (what to build first)

1. **Intent Engine (`loom_context`)** — the keystone, makes CodeLoom a context engine
2. **Layered memory** — architecture/patterns/decisions persistence
3. **Git intelligence** — churn, ownership, blame, hot paths
4. **Hybrid search** — AST + graph + embeddings + git scored together
5. **Session memory** — avoid re-reading what was already seen
6. **Cross-repository** — one graph across services
7. **Plugin SDK** — framework-aware extraction for other ecosystems
8. **Visual graph** — local, built-in, zoomable
9. **Bug prediction + repository timeline** — the "nobody has" differentiators
