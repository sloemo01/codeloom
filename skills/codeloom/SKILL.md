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

## Honest scope (small repos)

On small repos (<~200 files), codeloom's value is **memory / safety /
evidence** (`--verify-edit`, `--checkpoint`, `--impact`, `--memory`), not raw
speed — a plain `grep` is faster for a single search. Use the MCP server
in-session for ms-latency searches (its resident index answers in milliseconds;
the CLI pays parse cost per invocation).

## Prerequisites

- Python 3.8+ (stdlib only — no pip deps).
- Repo files: `codeloom.py`, `codeloom-mcp.py`, `tests.py`, `demo.tape`,
  `README.md`, `LAUNCH.md`, `scripts/memory_extract.py`.
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

# --- Retrieval & evidence (v0.76+) ---
# One-call cited answer with honest confidence (the "just ask" entry point)
python3 codeloom.py --answer "how does login work" /path/to/repo

# Batch triage: N symbols in one call
python3 codeloom.py --context-card Engine LoginRoute /path/to/repo

# Decision lookup with evidence stamps
python3 codeloom.py --why "why retry(3)" /path/to/repo

# Natural-language flow discovery
python3 codeloom.py --find "where login is handled" /path/to/repo

# Structural AST search with metavariables ($VAR / $$$REST)
python3 codeloom.py --pattern '$F($$$ARGS)' /path/to/repo

# --- Safety layer (v0.78): run these around edits ---
# Post-edit integrity oracle: GO/CHECK/STOP after you (or an agent) edited files
python3 codeloom.py --verify-edit /path/to/repo
python3 codeloom.py --verify-edit --severity strict /path/to/repo   # STOP exits 1

# Blindspot: warn when editing files never read (uses --mark-seen hot set)
python3 codeloom.py --blindspot /path/to/repo

# Change-risk for the last commit (score 0-100 + drivers)
python3 codeloom.py --risk /path/to/repo
python3 codeloom.py --risk HEAD~3..HEAD /path/to/repo

# --- Health & signal ---
python3 codeloom.py --health /path/to/repo        # 0-10 per file
python3 codeloom.py --churn /path/to/repo         # most-edited files
python3 codeloom.py --timeline /path/to/repo      # architecture evolution
python3 codeloom.py --bug-predict /path/to/repo   # likely-to-break files
python3 codeloom.py --langs                       # supported languages

# --- Savings & proof ---
python3 codeloom.py --savings-report /path/to/repo   # local token ledger (no telemetry)
python3 codeloom.py --eval bench --root /path/to/repo  # reproducible benchmark suite

# --- Hooks ---
python3 codeloom.py --install-hook /path/to/repo   # warn-only pre-commit
python3 codeloom.py --uninstall-hook /path/to/repo
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
| `--install-agent AGENT` | print MCP config for 17 agents (claude/cursor/codex/gemini/opencode/cline/openhands/devin/hermes/aider/roo/windsurf/amazon-q/jetbrains/junie/kimi/qwen) |
| `--detect-agent` | detect which coding agent's config dir is present |
| `--cost` | append token-cost estimate |
| `--task "text"` | rank modules relevant to a task |
| `--impact X` | predict blast radius of changing X |
| `--check-edit X` | preflight: is it safe to edit X? (terminal GO/CHECK/STOP) |
| `--check-delete X` | preflight: is it safe to delete X? (terminal GO/STOP) |
| `--plan "X"` | prioritized reading plan for a task |
| `--pack "X"` | single-shot context file (reading order + impact + symbols) |
| `--resume` | restore structural context after a compaction (compact snapshot) |
| `--checkpoint "note"` | snapshot in-progress work (git diff + status note) to survive compaction |
| `--checkpoint-restore` | read the last checkpoint back to resume in-progress work |
| `--cross` | cross-file call graph (AST-resolved) |
| `--search X` | search the symbol index (definitions + snippet) |
| `--embed-search "q"` | fuzzy semantic search (subword-hash embedding, zero-dep — catches typos) |
| `--usages X` | find where a symbol is used (call sites + snippet) |
| `--grep X` | search file contents for a snippet (ranked + context) |
| `--grep-symbolic QUERY` | grep with symbolic awareness: match symbols/identifiers, not just text |
| `--lessons TEXT` | alias for `--lesson` (README documents `--lessons`) |
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
| `--index --engine c` | use the compiled C accelerator (auto-builds from codeloom_core.c on first use) |
| `--auto-grammars` | scan repo + install grammars for its languages (repo-aware AST depth) |
| `--ask "task"` | one-shot complete task brief: loom + blast radius + files-to-touch |
| `--rename OLD NEW` | what a rename touches: definitions, files, dependents, edges |
| `--serve ROOT` | C-resident index server: sub-ms lookups (no Python per query) |
| `--watch-core` | native C file watcher (kqueue/inotify): print changed code files live |
| `--precision SYMBOL` | graph precision: call edges with confidence + class relationships |
| `--routes` | extract HTTP routes: METHOD path → handler (framework-aware) |
| `--channels` | pub-sub / event channel map (EMITS → LISTENS_ON) |
| `--export FILE` | export a portable graph snapshot (symbols + edges + routes + channels) |
| `--query "callers X"` | fast structural query against the persisted graph (callers/callees/hubs/routes) |
| `--files GLOB` | find files by name/glob: `--files engine` or `--files "*.py"` |
| `--watch` | incremental daemon-less refresh (re-index only changed files) |
| `--index-status` | show persistent index status/freshness |
| `--framework` | detect the web/app framework + surface routes/models/config/conventions |
| `--session` | log this invocation to the local session log (JSONL) |
| `--session-report` | summarize the local session log (calls, tokens, cost) |
| `--json` | machine-readable JSON |
| `--write FILE` | write map to FILE |
| `--no-outline` | skip per-file one-liners (faster) |
| `--max-files N` | cap traversal (default 20000) |
| `--memory-add` (v0.79) | write a typed memory object to `.codeloom-memory/memory.jsonl` (`--type decision\|bug\|question\|architecture\|api\|constraint\|lesson\|todo\|warning\|goal\|hypothesis`, default goal, + `--title`/`--body`/`--symbols`/`--priority`); importance scored by formula |
| `--memory SYMBOL` (v0.79) | graph-linked retrieval: typed entries for the symbol + graph-neighbor reachable entries; `--target-root ROOT` builds the graph against another root, `--include-archive` also searches archived `memory-*.jsonl` |
| `--memory-stats` (v0.79) | typed-memory stats: per-type counts, tiers, archive/rotation info |
| `--goal TEXT` | record the session goal (shown by `--working-state` and `--resume`; appends a typed `goal` entry to `memory.jsonl`) |
| `--adr-status STATUS` | status for `--adr` (default Accepted) |
| `--remember NOTE` | smart-dispatch: if NOTE names a repo symbol or a pinned memory entry → graph retrieval; otherwise legacy append to `--section` markdown (default DECISIONS) |
| `--memory-prune` | dry-run report of old `.codeloom-memory/archive` entries; `--older-than DAYS` (default 90), `--delete` actually deletes |
| `--engine {py,c,rust}` | scanning engine: py (pure-Python, default), c (compiled codeloom_core), rust (compiled codeloom_core_rs, multi-threaded) |

**Answers, evidence & safety (v0.76-v0.78 surface):**
| Flag | Purpose |
|---|---|
| `--answer QUESTION` | one-call cited answer with honest confidence — the "just ask" retrieval entry point |
| `--context-card S1 S2 ...` | batch triage card: N symbols' signatures + docs + relevance in one call |
| `--why QUERY` | decision lookup with evidence stamps `[exact]`/`[fuzzy]`/`[unverified]` |
| `--verify-edit [ROOT]` | **post-edit integrity oracle**: re-parse changed files, GO/CHECK/STOP verdict on dangling imports/cycles; cycles are diff-introduced only — a pre-existing cycle in the full-HEAD baseline (e.g. `a <-> b` committed long ago) never triggers STOP; `--severity warn` (exit 0 on STOP) or `--severity strict` (exit 1) |
| `--blindspot` | compare the `--mark-seen` hot set against impact-derived read set: STOP-tier "editing a file you never read" warning; `--no-blindspot` opts out |
| `--risk [REVSPEC]` | change-risk report for a commit/range (default `HEAD~1..HEAD`): score 0-100 + named drivers |
| `--savings-report` | token/seconds savings vs grep+read baseline, from the `--session` log; `--since DAYS` filters, `--repo PATH` aggregates per repo |
| `--eval KIND` | run `benchmarks/eval_runner.py` (`--root PATH`): kinds `token|compaction|sealed|bench` — the reproducible benchmark suite |
| `--install-hook` | install `.git/hooks/pre-commit` running `scripts/pre-commit-hook.sh` (warn-only, never blocks) |
| `--uninstall-hook` | remove the codeloom-managed pre-commit hook |
| `--health` | code-health screen: 0–10 per file, deterministic detectors (complexity, duplication, churn, ownership) |
| `--churn` | git churn: most-edited files (instability signal for refactor targeting) |
| `--timeline` | repository timeline: replay architecture evolution via git history |
| `--dedup` | session dedupe: skip already-read files, show only the new |
| `--pattern PATTERN` | structural AST search: `$VAR` captures, `$$$REST` captures (metavariable pattern matching) |
| `--hybrid-search QUERY` | combined semantic + symbolic search (index-first with fuzzy fallback) |
| `--langs` | list supported languages/extensions (broad regex + tree-sitter dispatched) |
| `--lsp` | show LSP bridge status (optional semantic enrichment when an LSP is installed) |
| `--lsp-symbol SYMBOL` | resolve a symbol's real definition via an installed LSP server |
| `--graph-html` | write a local zoomable HTML graph view (browser-based exploration) |
| `--find QUERY` | natural-language flow discovery: `--find "where login is handled"` |
| `--plugin-sdk` | show the plugin SDK surface for framework-aware extensions |
| `--build-core` | build the optional C accelerator (`codeloom_core.c` → compiled) |
| `--parallel` | parallelize file parsing for heavy ops (large-repo speedup) |
| `--bug-predict` | bug prediction: files likely to break (churn + complexity heuristic) |
| `--watch-merge` | read watcher JSON lines from stdin, apply changed files into the index |

**Optional precision backends** (auto-enabled when present, zero-dep otherwise):
- `tree-sitter` + grammars → precise multi-language AST parsing
- `CODELOOM_EMBED_BASE_URL`/`CODELOOM_EMBED_API_KEY` or `sentence-transformers` → semantic task scoring
- `--trace` → runtime call edges (static blind spots)

`--focus` accepts a file path, package dir, or dotted module name
(`browser_use/agent`, `browser_use/agent/service.py`, `agent.service`).

## Use cases (what each command is FOR — pick by the agent's goal)

This is the decision guide: given what you're trying to do, which flag serves it.

### Understand a codebase (onboarding, unfamiliar repo)
- `codeloom` — the 30-second map: tree + one-liners + entry points. **Start here**
  on any repo you've never seen.
- `codeloom --graph` — import dependency graph: "what touches what."
- `codeloom --calls` / `--cross` — execution flow: "what calls what, across files."
- `codeloom --framework` — what web framework, its routes, models, config, conventions.
- `codeloom --architecture` / `--heatmap` — big-picture pattern and dependency smells.
- `codeloom --explain-topic X` / `--loom X` / `--ask X` — understand one domain end-to-end.

### Find code (retrieval)
- `codeloom --search X` — "where is symbol X defined?"
- `codeloom --usages X` — "where is X called/used?"
- `codeloom --grep "text"` — "where does this exact code/string appear?" (ranked, snippet)
- `codeloom --files "engine"` / `--files "*.py"` — "find the file by name/glob"
- `codeloom --read X` — "show me X's exact source, token-efficiently"
- `codeloom --get-symbol X` — "smallest snippet for X + its token cost" (summary-first)
- `codeloom --snippet P S E` — "extract bytes S–E from file P"

### Understand one symbol
- `codeloom --explain X` — plain-English role of X (AST + call graph, no LLM)
- `codeloom --similar X` — refactoring candidates with the same shape
- `codeloom --precision X` — call edges with confidence + class relationships
- `codeloom --deadcode` — "what's defined but never called?"

### Plan a change (before editing — the safety layer)
- `codeloom --task "desc"` — rank files relevant to the task
- `codeloom --impact X` — blast radius: "what breaks if I change X?"
- `codeloom --plan "task"` — prioritized reading order for the task
- `codeloom --pack "task"` / `--ask "task"` — the complete one-shot task brief
  (relevant files + embedded code + impact + files-to-touch)
- `codeloom --check-edit X` / `--check-delete X` — GO/CHECK/STOP safety verdict
- `codeloom --refactor X` — refactor engine: files, deps, risk, safe order
- `codeloom --rename OLD NEW` — what a rename touches (blast radius for renames)

### Verify an edit (after editing — the loop-closure layer, v0.78)
- `codeloom --verify-edit` — **after** you (or an agent) changed files: re-parses
  and returns GO/CHECK/STOP on dangling imports/new cycles. Cycle detection
  compares the FULL-HEAD graph against the post-edit graph and fires only on
  **diff-introduced** cycle edges — a cyclic repo's pre-existing baseline never
  triggers STOP, so it is safe on codebases that were already cyclic. Run it
  before telling the agent "edit is fine" — `--severity strict` makes STOP exit
  1 (for CI/gates).
- `codeloom --blindspot` — "am I about to edit a file I never read?" (uses the
  `--mark-seen` hot set); `--no-blindspot` opts out.
- `codeloom --risk HEAD~1..HEAD` — "how risky is my last change?" (0-100 + drivers)
- `codeloom --install-hook` — make the pre-commit warn automatically (never blocks)
- `codeloom --savings-report` — "what did this session cost in tokens?" (local ledger)

### One-call retrieval (evidence-first answers)
- `codeloom --answer "Q"` — cited answer with honest confidence — first choice
  when the agent needs to answer a question about the codebase
- `codeloom --context-card A B C` — batch triage: many symbols in one call
- `codeloom --why "X"` — "why was this decision made?" (evidence-stamped)
- `codeloom --find "login flow"` — natural-language flow discovery
- `codeloom --pattern '$F($$$ARGS)'` — structural AST search (metavariables)
- `codeloom --hybrid-search Q` — semantic + symbolic combined
- `codeloom --embed-search "q"` — fuzzy semantic search (typo-tolerant)

### Health / signal (refactor targeting)
- `codeloom --health` — 0-10 per-file code health screen
- `codeloom --churn` — most-edited files (instability signal)
- `codeloom --timeline` — architecture evolution over git history
- `codeloom --bug-predict` — files likely to break next
- `codeloom --dedup` — "what's new since my last run?" (skip already-read)

### Proof / reproducibility (maintenance)
- `codeloom --eval bench --root <repo>` — run the whole measured benchmark suite
  (token efficiency, compaction recovery, sealed retrieval, memory eval)
- `python3 benchmarks/dogfood_bench.py --repo <r> --task '<t>'` — head-to-head
  plain grep+read vs codeloom on the same repo/task (zero-dep, zero-LLM):
  calls / tokens-in / tokens-out / wall / completeness, with honest loss rows —
  if codeloom loses a metric it says so (e.g. flask: +14.5% tokens, 2.6x wall
  vs plain, but wins on evidence: impact, memory/checkpoint survival)
- `codeloom --verify FILE` — SHA-256 checksum (download trust)

### Web / backend architecture
- `codeloom --routes` — "what HTTP endpoints exist, METHOD path → handler"
- `codeloom --channels` — "what pub-sub/event channels, EMITS → LISTENS_ON"
- `codeloom --cross-repo A B` — one graph across services (frontend + backend + SDK)
- `codeloom --context-diff BASE HEAD` — branch-to-branch architecture diff

### Session / persistence / compaction
- `codeloom --write MAP.md` — commit the map as a reviewable artifact
- `codeloom --remember "note" --section X` — persist a conclusion (survives compaction); if "note" names a repo symbol, it smart-dispatches to graph-linked retrieval instead
- `codeloom --adr "title" --context "..." --decision "..."` — write a structured Architectural Decision Record (the human "why")
- `codeloom --adr-list` — list saved ADRs
| `--decide "title" --reason "..."` | record a decision (accepted/rejected) — survives compaction |
| `--reject "title" --reason "..."` | record a rejected decision |
| `--status STATUS` | status for `--decide` (accepted/rejected/open) |
| `--hypothesis "title"` | record an open hypothesis |
| `--mark-seen f sym` | mark files/symbols as deeply understood (hot set) |
| `--working-state` | layered working-state packet: goal, decisions, actions, open items, hot set |
| `--list-decisions` / `--list-open` | list recorded decisions / open items |
- `codeloom --lesson "tried X, failed because Y"` — record a trap (never re-explore dead ends)
- `codeloom --supersede OLD NEW` — mark a decision as superseded
- `codeloom --query-memory "auth"` — what do we already know about X
- `codeloom --memory-add --type bug --title "..." --symbols AuthService` (v0.79) — write a typed memory object to `memory.jsonl`
- `codeloom --memory Engine` (v0.79) — typed + graph-neighbor memory retrieval
- `codeloom --memory-stats` (v0.79) — per-type memory counts/tiers
- `codeloom --cognitive-load "topic"` — cognitive-load-aware decomposition
- `codeloom --seen` — "what have I already explored?" (avoid re-reading)
- `codeloom --resume` — restore the structural map after a compaction
- `codeloom --session` / `--session-report` — log & summarize what was done
- `codeloom --incremental` — "what changed since my last run?"

### Scale / freshness / speed
- `codeloom --index` — build a persistent graph once, then query in ms
- `codeloom --index --engine c` / `--watch-core` / `--serve` — C accelerator,
  native watcher, sub-ms resident server (huge monorepos)
- `codeloom --index-status` — is the index fresh?
- `codeloom --watch` — daemon-less incremental refresh

### Maintenance / trust
- `codeloom --install-agents` — write AGENTS.md + CI action
- `codeloom --install-agent AGENT` — print MCP config for 17 agents
- `codeloom --detect-agent` — which agent is already configured
- `codeloom --install-grammars --yes` / `--auto-grammars` — tree-sitter AST depth
- `codeloom --trace CMD --force` — capture runtime call edges static misses
- `codeloom --verify FILE` — confirm a download is the official checksum
- `codeloom --export FILE` — write a shareable graph snapshot teammates can clone
- `codeloom --json` — machine-readable output for tooling
- `codeloom --docs readme|arch` — generate a README or ARCHITECTURE doc

## Procedure

### 0. Compaction-survival habit (do this in EVERY session)
An agent's repo knowledge and its **work-in-progress understanding** survive a
context compaction **only if they were saved to disk**. Make this a default
habit, not an optional step. The two memory layers:

- **Session journal** (`--decide`/`--reject`/`--hypothesis`/`--mark-seen`/
  `--working-state`) — narrative of what the agent did/decided this session.
- **Persistent memory** (`--remember`/`--adr`) — long-term decisions/patterns.

1. **On first contact with a repo:** run `codeloom` to build the map, then
   `codeloom --write MAP.md` so the structural overview is a committed file.
2. **Record decisions explicitly:** `codeloom --decide "<title>" --reason "..."`
   when you choose an approach; `codeloom --reject "<title>" --reason "..."`
   when you abandon one. This prevents re-trying failed ideas after a wipe.
3. **Record hypotheses** that are still open: `codeloom --hypothesis "<guess>"`.
4. **As you read/understand files, mark them** as deeply understood:
   `codeloom --mark-seen file.py symbol::name` (adds them to the hot set).
5. **Before a compaction or at the end of a work chunk, snapshot progress:**
   `codeloom --checkpoint "<what you're doing / decided>"` writes the uncommitted
   diff + status note. Restore with `--checkpoint-restore`.
6. **Record architectural decisions as structured ADRs:** `codeloom --adr "<title>"
   --context "..." --decision "..."` captures the human "why".
7. **Every invocation should log itself:** `codeloom --session` (or the MCP
   server's resident session log) so `--session-report` can summarize what was done.

**After a compaction or an "I forgot the earlier context" signal, your FIRST
action is:**

```bash
codeloom --working-state .     # layered packet: goal, decisions, actions, open items, hot set
codeloom --resume .             # structural map (tree + entry points + call graph)
codeloom --checkpoint-restore .  # in-progress work (uncommitted diff + note)
codeloom --list-open .          # open items/hypotheses
```

Treat the `--working-state` packet as the single source of truth for what you
decided and where you left off.

> Rule of thumb: if a fact about the codebase, a decision, or the task matters,
> write it to disk the moment you learn it. Do not rely on context memory to
> carry it — context gets compacted, files do not.

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
3. Tools exposed (82 total — full surface below). `codeloom_ask` is the
   single natural-language entry point that routes deterministically — the
   agent never picks among tools. The complete registry:

| `codeloom_adr` | Write a structured Architectural Decision Record (context + decision + status) to .codeloom-memory/adr/. Captures the human 'why' behind an architecture choi... |
| `codeloom_adr_list` | List all saved Architectural Decision Records. Use before writing a new ADR to avoid duplicating an existing decision. |
| `codeloom_answer` | One-call cited answer to a natural-language code question (repowise get_answer parity): hybrid search -> best match with honest confidence (high/medium/low),... |
| `codeloom_architecture` | Detect the architectural pattern (MVC / layered / Clean / DDD / Hexagonal / monolith / microservices) from the repo layout, plus the top-level structure. Ori... |
| `codeloom_ask` | Single natural-language entry point. Ask in plain English and codeloom routes deterministically to the right tool — the agent never has to pick among 82 tool... |
| `codeloom_blindspot` | Coverage audit: find the files/symbols you have NOT read yet so nothing important is missed before you act. Answers 'read coverage', 'read everything', 'what... |
| `codeloom_bug_predict` | Bug prediction: files statistically likely to break, scored by churn + coupling + complexity. Priority hotfix/refactor targets. |
| `codeloom_calls` | Function-level call graph: which functions call which, across the codebase. Only reports calls to functions defined in the repo (builtins/stdlib filtered out... |
| `codeloom_channels` | Pub-sub / event channel map: EMITS -> LISTENS_ON edges for socket.io, Node EventEmitter, Kafka/RabbitMQ-style pub-sub. Links senders to receivers across files. |
| `codeloom_check_delete` | Preflight: is it safe to delete this symbol? Returns a terminal GO/STOP verdict — GO only if nothing references the symbol, else STOP with the exact dependen... |
| `codeloom_check_edit` | Preflight: is it safe to edit this symbol? Returns a terminal GO/CHECK/STOP verdict with the exact callers that will break, so the agent stops looping and kn... |
| `codeloom_checkpoint` | Snapshot in-progress work (uncommitted git diff + a status note) to a file so it survives a context compaction. Call this before a compaction or at the end o... |
| `codeloom_checkpoint_restore` | Read the last checkpoint back so the agent can resume in-progress work (uncommitted diff + status note) after a compaction. |
| `codeloom_churn` | Git intelligence: the most-edited files (by commit count) — an instability signal. Helps decide which files are risky to change. |
| `codeloom_cognitive_load` | Cognitive-load-aware task decomposition: splits a topic into working-memory-sized steps (intrinsic load), flags noise to skip (extraneous load), and surfaces... |
| `codeloom_context` | Batch triage card for MULTIPLE symbols in ONE call (repowise get_context parity): per-target definition, same-module signatures, callers count, and governing... |
| `codeloom_context_diff` | Branch-to-branch architecture-level diff: which modules changed between two refs, not just lines. |
| `codeloom_cross` | Cross-file call graph: resolve calls to their defining module, so A.main() calling engine.run() (imported from B) yields A.main -> B.engine.run. Deep AST ana... |
| `codeloom_cross_repo` | Build a combined knowledge graph across multiple repository roots (frontend + backend + SDK + CLI + docs). Returns per-repo modules and cross-repo service-to... |
| `codeloom_deadcode` | Find functions/classes defined in the codebase but never called. Uses the call graph to detect dead code. |
| `codeloom_dedup` | Session dedupe: skip files already read this session and return only the new delta — saves tokens across repeated calls. |
| `codeloom_diff` | Show the structure of only the files changed vs git HEAD. Use when the agent is working on a specific change — tells it what's relevant to the current task, ... |
| `codeloom_docs` | Generate a README or ARCHITECTURE doc from the repo structure. kind: 'readme' (default) or 'arch'. |
| `codeloom_embed_search` | Fuzzy semantic symbol search using a zero-dependency subword-hash embedding (fastText n-gram technique, pure-Python). Finds symbols whose identifier is seman... |
| `codeloom_explain` | Generate a plain-English explanation of a symbol's role using its AST signature + call graph. Template-based, no LLM needed. Returns a summary, what it calls... |
| `codeloom_explain_topic` | Explain a topic/domain end-to-end: relevant files + call flow, instead of a single symbol. E.g. 'authentication' -> the files and how they connect. |
| `codeloom_export` | Export a portable, self-contained graph snapshot (symbols + call/import edges + routes + channels) to a single JSON file. Commit it to the repo so teammates ... |
| `codeloom_files` | Find files by name or glob, e.g. '--files engine' or '--files *.py'. Returns matching paths relative to root. For locating a file you don't know the exact pa... |
| `codeloom_find` | Natural-language flow discovery: 'find where login starts' / 'show every payment flow'. Returns the domain's entry points + call flow. |
| `codeloom_focus` | Focus on ONE module: what it depends on (depends_on) and what depends on it (depended_on_by). Answers 'what does this code need?' and 'what breaks if I chang... |
| `codeloom_framework` | Detect the web/app framework (Next.js, FastAPI, Django, Laravel, Express, etc.) and surface its structure: entry points, routes, models, config, and conventi... |
| `codeloom_get_symbol` | Token-counted symbol retrieval. By default returns a SUMMARY (signature + docstring + call graph) — the 95%+ token-savings mode. Pass full=true for the compl... |
| `codeloom_get_working_state` | Return the layered working-state packet: goal, status, key decisions, actions taken, open items/hypotheses, and hot set (already-understood files). Call this... |
| `codeloom_graph` | Build the Python import dependency graph of a codebase: which modules import which. Use to understand 'what touches what'. |
| `codeloom_graph_html` | Write a local zoomable HTML graph view of imports/calls to codeloom-graph.html. Self-contained, no daemon — open in a browser. |
| `codeloom_grep` | Search file contents for a snippet (the 'find the exact code' capability). Returns ranked matches with context lines. Use to find where a specific code patte... |
| `codeloom_grep_symbolic` | Code-only grep: matches real CODE, excluding comments and string literals (the usual false-positive sources). Results ranked by symbol relevance — hits insid... |
| `codeloom_health` | Code health screen (repowise get_health parity, speed-first): deterministic detectors — long functions, too-many-params, dead symbols, duplicate names — scor... |
| `codeloom_heatmap` | Dependency heatmap: god/hub classes (widest blast radius), circular imports, and possibly-unused modules. Refactor-risk signals. |
| `codeloom_hybrid_search` | Hybrid search: BM25 lexical score + structural signals (symbol kind, size) + git churn scored together. Ranks symbols by combined relevance, better than bare... |
| `codeloom_impact` | Predict the blast radius of changing a module: which modules depend on it (direct + transitive) and what it depends on. Answers 'what breaks if I change this... |
| `codeloom_incremental` | Show which files changed since the last run, using a hash-based cache (no daemon). Use for repeated runs on large repos — only re-parses changed files. |
| `codeloom_langs` | List supported languages/extensions: the broad 130+ regex/C structural-extraction set plus the opt-in tree-sitter precision set. |
| `codeloom_list_open_items` | List the open items/hypotheses recorded in the current session. |
| `codeloom_loom` | The intent engine. Given a task in plain English, return LAYERED context in one call: overview -> important files (edit-relevance) -> relevant code (pack) ->... |
| `codeloom_lsp` | LSP bridge status: detect installed language servers (pyright, clangd, rust-analyzer, gopls...) for optional semantic enrichment. codeloom stays zero-dep — L... |
| `codeloom_lsp_symbol` | Resolve a symbol's real definition via an installed LSP server (pyright/clangd/rust-analyzer/gopls/ts-server) — the cross-file edge static parsing can miss. ... |
| `codeloom_map` | Produce a compact 'table of contents' of a codebase: folder tree, per-module one-liners (classes/functions), and entry points. Use this FIRST to build a ment... |
| `codeloom_mark_seen` | Mark files or symbols as already deeply understood so they appear in the hot set of future working-state packets. Use after reading a file/symbol so a post-c... |
| `codeloom_memory_add` | Memory OS add: write a typed memory entry (decision, bug, lesson, constraint, architecture, api, question, todo, warning) linked to symbols. Entries feed cod... |
| `codeloom_memory_prune` | Memory OS growth bounds: report archive entries older than N days (dry-run; NEVER auto-deletes). Use with 'delete': true to actually prune the reported entri... |
| `codeloom_memory_stats` | Memory OS stats: report the repository's memory health — entry counts by type, growth bounds, archive size. Call before pruning or to understand how much mem... |
| `codeloom_pack` | Single-shot context packing: emit ONE compact file for a task with reading order + impact analysis + symbol index, all pre-computed. An agent pastes this onc... |
| `codeloom_pattern` | Structural AST pattern search (ast-grep-style, zero-dep): find every code site matching a code shape. $VAR captures one node, $$$REST captures lists. Example... |
| `codeloom_plan` | Emit a prioritized 'read these files, in this order' plan for a task. The agent-native format: tells the agent exactly what to read to understand a task befo... |
| `codeloom_plugin_sdk` | Show the plugin SDK surface: how to write a framework-aware extraction hook that extends codeloom. |
| `codeloom_precision` | Graph precision report for a symbol: call edges annotated with confidence (definite vs maybe) plus class relationships and dependents. Helps agents trust whi... |
| `codeloom_query` | Fast structural query against the persisted graph (build with --index first): callers X, callees X, dependents X, hubs, routes, symbol X. One graph query rep... |
| `codeloom_query_memory` | Search long-term memory (decisions, patterns, lessons, conventions, ADRs) for what the agent already knows about a topic. Use for 'what do we already know ab... |
| `codeloom_read` | Extract the exact source of a function, class, or method. Python uses AST; other languages use tree-sitter (when available) or brace-matching. Token-efficien... |
| `codeloom_record_decision` | Record an accepted or rejected decision with a reason into the session journal (and persistent memory). Prevents re-trying failed ideas after a compaction. U... |
| `codeloom_record_hypothesis` | Record an open hypothesis about the codebase so it survives compaction and can be verified later. Shows up in the working-state packet's open items. |
| `codeloom_record_lesson` | Record a lesson/trap: something tried and why it failed, so a wiped agent never re-explores the same dead end. Use when you abandon an approach after trying it. |
| `codeloom_refactor` | Refactor engine for a symbol: files touched, dependencies, risk assessment (preflight), and suggested safe order. |
| `codeloom_remember` | Memory OS retrieval: fetch everything the repo remembers about a symbol — linked memory notes, decisions, lessons, and the memory graph around it. Ask 'what ... |
| `codeloom_rename` | What a rename touches: every definition, every file containing the name, every dependent module/edge. Run before renaming a symbol to know the blast radius a... |
| `codeloom_resume` | Emit a compact structural snapshot (entry points + modules + hub modules + top call sites) to restore an agent's context after a compaction. Paste the output... |
| `codeloom_risk` | Pre-merge change-risk report for a commit or range (clean-room, zero LLM): scores diff size, file spread, health findings in touched files, high-fan-in symbo... |
| `codeloom_routes` | Extract HTTP routes: METHOD path -> handler across frameworks (FastAPI, Flask, Express, Django, Next.js, Starlette). Links URL patterns to the handler that s... |
| `codeloom_search` | Search the symbol index for a function, class, or method. Returns where each symbol is defined (module + line) with a context snippet. Works across Python an... |
| `codeloom_seen` | Session memory: report which files and symbols were already read this session, so the agent can skip re-reading them and save tokens. Reads the local session... |
| `codeloom_session_report` | Summarize the local session log: total calls, tokens, and estimated input cost, broken down by command. Local observability — no network, no daemon. Run code... |
| `codeloom_similar` | Find functions/classes with a structurally similar signature (same param count) for refactoring. Returns candidates across the codebase. |
| `codeloom_snippet` | Extract a byte-range snippet from a file. Returns the text + token estimate + byte count. Use for precise, token-efficient retrieval. |
| `codeloom_task` | Rank modules relevant to a task description, by token overlap + graph centrality. Use to find which files matter for a specific task before reading the whole... |
| `codeloom_timeline` | Repository timeline: replay architecture evolution via git log — who changed what, when. |
| `codeloom_trace` | Run a command (e.g. a test script) under sys.settrace and record the ACTUAL runtime call edges. Captures dynamic imports and monkeypatching that static analy... |
| `codeloom_usages` | Find where a symbol is USED (not just defined) across the codebase. Answers 'where is this function/class called?' with context snippets. |
| `codeloom_verify` | Print the SHA-256 of a file so users can verify a downloaded copy of codeloom is official and not tampered with. |
| `codeloom_verify_edit` | Edit-safety preflight: given a target file (or repo root), report whether an edit there is safe — dependents, call sites, blast radius — with a GO/STOP verdi... |
| `codeloom_watch` | Incremental daemon-less refresh: re-index only files changed since the last index, keeping the lazy per-symbol store current. Call before queries to guarante... |
| `codeloom_why` | Decision lookup with evidence stamps (repowise get_why parity): searches recorded memory/ADRs and stamps every matching line [exact]/[fuzzy]/[unverified] so ... |

### 3. Deterministic routing — how `codeloom_ask` works (the 82-behind-1 design)

`codeloom_ask` is not an LLM picking a tool. It is a **deterministic keyword
router** with fixed branch priority (verified in `codeloom-mcp.py` `_route_ask`):

1. **Edit-safety guard (first)** — "verify", "did i break", "check my edit",
   "blindspot", "read coverage" → `codeloom_verify_edit` / `codeloom_blindspot`.
   These win over every other branch on purpose: safety questions must never
   be mis-routed.
2. **Task orientation** — "what matters", "which files", "what breaks",
   "impact of", "blast radius", "read order", "pack" → task/impact/plan/pack.
3. **Symbol / retrieval** — symbol names, "what is", "where is" → search/
   read/explain/usages.
4. **Memory** — "remember this", "what do we know about" → memory write/
   retrieval (`--memory <symbol>` graph-linked).
5. **ADRs / checkpoint / resume / session** — "record decision",
   "save my progress", "what was i doing", "tokens spent".
6. **Export / cross-repo / files / langs** — "share graph", "across repos",
   "find file", "what languages".
7. **Question-shaped fallback** — "why…" → `--why` decision lookup; "who/
   what/how/where" or ≤4 words → `--answer` cited answer.
8. **Default (never an error)** — map + task relevance. Even an empty or
   ambiguous query returns USEFUL context, never nothing.

Consequences for the agent (why this matters):
- **Never pick among 82 tools** — one NL entry point, deterministic mapping.
  No LLM tool-selection step, so no tool-selection misfires (the problem
  jcodemunch hit with its 91-tool surface).
- **Fail-safe by design** — every branch returns actionable context; the
  default is the map + task ranking. A "wrong" pick is still helpful.
- **Deterministic == testable** — same query, same route, every time. The
  router is exercised by the 120-test suite and the MCP handshake.
- **Keyword order is load-bearing** — safety (verify/blindspot) and
  write-vs-read disambiguation ("remember this bug" → write, "what do we
  know about X" → retrieve) sit BEFORE the broad branches that would
  swallow them.

### 3. Memory OS workflow (v0.79)
The typed-memory layer (`memory.jsonl`) turns the markdown memory files into
structured, importance-scored, graph-linked entries:

1. **Write memory the standard ways** — `--decide`, `--lesson`, `--adr`
   keep writing their markdown files AND append a typed JSONL entry
   (backward-compatible dual write).
2. **Write arbitrary typed notes** — `--memory-add --type decision|bug|question|
   architecture|api|constraint|lesson|todo|warning|goal|hypothesis` (default
   `goal`) + `--title "..." [--body "..." --symbols A,B --priority N]`
   appends a typed entry to `.codeloom-memory/memory.jsonl`; importance comes
   from the formula (type + keywords + symbols), printed as `importance: N`.
3. **Retrieve graph-linked** — `--memory <symbol>` returns the typed
   entries pinned to that symbol PLUS entries reachable through graph
   neighbors (e.g. a module that imports it), not just exact matches.
4. **Extract from history** — `scripts/memory_extract.py [--dry-run]`
   deterministically mines `git log` (regex heuristics: bug/api/
   architecture → typed memories with confidence) and feeds the core via
   `--memory-add`; idempotent via `.codeloom-memory/extract-state.json`.
   Always run `--dry-run` first.
5. **Stats & bounds** — `--memory-stats` per-type counts/tiers; entries
   rotate losslessly into `archive/memory-<date>.jsonl` at cap
   (`CODELOOM_MEMORY_CAP_BYTES`), `--memory-prune` reports/deletes old
   archive entries (dry-run by default, `--delete` to act).

### 4. Run the test suite
```bash
python3 tests.py
```
Expect `OK` (120 tests, incl. the 7 `TestMemoryOS` cases). Add tests for any
new feature.

### 5. Re-record the demo GIF
1. Edit `demo.tape` to showcase the features you want (map, graph+focus, calls, diff).
2. Ensure the demo repo (`demo-repo/`) is a git repo with a committed baseline
   and a real change so `--diff` has output.
3. Run `vhs demo.tape` (with `~/bin` on PATH so `codeloom` resolves).
4. Verify a late frame with `vision_analyze` (extract via ffmpeg) before committing.

### 6. Extend codeloom
- New CLI flag: add to `argparse` in `main()`, implement the logic, add a test.
- New MCP tool: add to `TOOLS` list + a branch in `call_tool()` in `codeloom-mcp.py`.
- Bump `VERSION` in `codeloom.py` and `SERVER_VERSION` in `codeloom-mcp.py`.
- Update `README.md` (usage, feature table, roadmap) and `LAUNCH.md` if launch copy changes.

## Pitfalls
- **Dual-path verification is mandatory (deadcode/call-graph fixes)**: codeloom has TWO
  call-graph implementations — the regex/`_scan_calls` path and the tree-sitter
  fast-path (`_ts_call_edges`). A fix to one is NOT a fix to both. If you change
  call-edge logic (class definitions, module-level calls, attribute calls), you
  MUST verify in an environment where tree-sitter + grammars are installed
  (the fast path), because a grammar-less sandbox exercises only the regex path
  and can ship an incomplete fix. 2026-08-23: df2d3b1 fixed only the regex path;
  b4f1dc6 closed the tree-sitter gap after the regression was caught on the real
  repo. Repro check: `REGISTRY = Registry()` at module top-level must NOT flag
  `Registry` dead WITH a grammar installed, and a genuinely-dead class must
  STILL be flagged (no over-correction). Run `python3 -c "import tree_sitter"` to
  confirm which path you're testing.
- **Dead-symbol output needs manual triage before acting**: the CLI dead-code
  detector misses `self.`/`cls.` calls, qualified module references
  (`MODULE.sym()`), `export *` barrels, and interface-typed callers — so a large
  repo's dead list is mostly false positives. Verify with `rg -l \<name>` across
  the repo before claiming a symbol dead.
  MCP server keeps serving whatever code was on disk when it started — after
  a fix lands, the OLD behavior persists until restart even though
  `SERVER_VERSION` is unchanged. Detect it: `initialize` `serverInfo` now
  carries `server_file_mtime` + `server_sha256` (mtime + content hash of
  `codeloom-mcp.py` at load); `codeloom_health` prints a header
  (`server_version` / `server_file_mtime` / `index_commit`); every
  `tools/call` `_meta` carries the same identity. If these differ from the
  on-disk file, the server predates the code — **restart it (kill +
  respawn)** before re-testing.
- **Symbol-tool targets accept module paths too** (v0.79.0): the index keys
  on bare symbol names, but `codeloom_get_symbol` / `codeloom_context` /
  `codeloom_similar` / `codeloom_remember` fall back to module resolution
  when a dotted target ('src.flask.app') misses, and `codeloom_impact`
  accepts a bare symbol ('wsgi_app') resolving to its defining module —
  both marked '(resolved via module/symbol fallback)' in the output.
- **Optional engines auto-build — never a download** (`--engine c` / `--engine rust`):
  the first run compiles the committed `codeloom_core.c` (`cc -O3`, ~2s) or
  `codeloom_core_rs.rs` (`rustc -O`) locally — no network, no permission,
  no pip. If the compiler is missing, codeloom silently falls back to the
  pure-Python engine and still works. `--build-core` pre-builds explicitly.


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

- `python3 tests.py` → `OK` (120 tests).
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
- MCP server keeps an in-memory index (incremental, always fresh); `--get-symbol` uses a lazy per-symbol dbm index (near-resident single-key lookups).
- MCP smoke test returns `serverInfo` name `codeloom-mcp`; `tools/list` reports 82 tools.
- `scripts/memory_extract.py --dry-run` lists mined bug/api/architecture memories without writing.
- `demo.gif` exists and a late frame shows the intended feature output.
