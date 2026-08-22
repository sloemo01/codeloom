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
| `--memory SYMBOL` (v0.79) | graph-linked retrieval: typed entries for the symbol + graph-neighbor reachable entries |
| `--memory-stats` (v0.79) | typed-memory stats: per-type counts, tiers, archive/rotation info |
| `--remember NOTE` | smart-dispatch: if NOTE names a repo symbol or a pinned memory entry → graph retrieval; otherwise legacy append to `--section` markdown (default DECISIONS) |
| `--memory-prune` | dry-run report of old `.codeloom-memory/archive` entries; `--older-than DAYS` (default 90), `--delete` actually deletes |
| `--engine {py,c,rust}` | scanning engine: py (pure-Python, default), c (compiled codeloom_core), rust (compiled codeloom_core_rs, multi-threaded) |

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
3. Tools exposed: `codeloom_map`, `codeloom_graph`, `codeloom_focus`,
   `codeloom_calls`, `codeloom_diff`, `codeloom_impact`, `codeloom_task`,
   `codeloom_plan`, `codeloom_pack`, `codeloom_cross`, `codeloom_search`,
   `codeloom_usages`, `codeloom_grep`, `codeloom_read`, `codeloom_explain`,
   `codeloom_similar`, `codeloom_deadcode`, `codeloom_get_symbol`,
   `codeloom_snippet`, `codeloom_incremental`, `codeloom_verify`,
   `codeloom_trace`, `codeloom_ask` (single natural-language entry point
   that routes deterministically — the agent never picks among tools), and
   (v0.79) `codeloom_memory_add`, `codeloom_remember`, `codeloom_memory_stats`,
   `codeloom_memory_prune`, `codeloom_query_memory` (82 tools total).

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
Expect `OK` (101 tests, incl. the 7 `TestMemoryOS` cases). Add tests for any
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

- `python3 tests.py` → `OK` (101 tests).
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
