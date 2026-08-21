<h1 align="center">codeloom</h1>
<p align="center">
  <b>Give your AI coding agent a map of the repo in one second.</b><br/>
  Single-file · zero-dependency · no daemon · 100% local · <b>import graph + call graph + MCP + skill + optional C engine</b>
</p>

<p align="center">
  <a href="#demo"><img src="https://img.shields.io/badge/see%20it%20run-Demo-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.8+-blue"/></a>
  <a href="https://github.com/sloemo01/codeloom#readme"><img src="https://img.shields.io/badge/deps-zero-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom/stargazers"><img src="https://img.shields.io/github/stars/sloemo01/codeloom"/></a>
</p>

<p align="center">
  <b>17 coding agents supported</b>
</p>
<p align="center">
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Claude_Code-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Codex-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Cursor-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Gemini_CLI-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/OpenCode-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Cline-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/OpenHands-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Devin-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Hermes_Agent-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Aider-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Roo_Code-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Windsurf-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Amazon_Q-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/JetBrains-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Junie-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Kimi_CLI-supported-blueviolet"/></a>
  <a href="#supported-agents"><img src="https://img.shields.io/badge/Qwen_Code-supported-blueviolet"/></a>
</p>

<p align="center">
  <b>17 agents</b> · <b>130+ languages</b> · <b>61 MCP tools</b> · <b>91s Linux kernel</b>
</p>

---

Every AI coding agent (Claude Code, Cursor, Codex, Gemini) has the same problem:
before it can *do* anything, it has to figure out what your codebase even *is*.
So it greps, reads whole files, burns 40,000+ tokens just to build context — and
then it forgets what it learned.

The expensive part isn't the tokens — **it's the amnesia.** Every context
compaction wipes the structural map the agent spent thousands of tokens
building: what's the engine, what calls what, where the entry points live,
what breaks if you touch X. The agent has to re-derive all of it from scratch,
over and over.

**codeloom fixes that.** One command → a compact structural model of your repo
(folder tree + per-module one-liners + entry points + import graph + call graph)
that your agent reads in a second to build a mental model *before* touching
anything. And `codeloom --resume` emits a single compact snapshot that **restores
that structural context after a compaction in one shot** — the agent never has
to re-learn the codebase. No install. No daemon. No GPU. Runs 100% on your machine.

**Zero-install, zero-telemetry, offline.** codeloom is a single stdlib file you
copy in — no `pip install`, no model downloads, no license validation, no
telemetry that phones home. It reads your files, computes the structure, prints
it, and exits. The heavyweight tools (jcodemunch, codegraph, codebase-memory-mcp)
can't say that: they ship telemetry, model downloads, and license checks.
codeloom has none of it.

**Git-diffable.** `codeloom --write MAP.md` produces a reviewable text artifact
you commit and diff in PRs — the map changes visibly when the code changes.
jcodemunch's index is a binary blob you can't review or diff. codeloom's map is
a file your team can read, review, and version.

## Agent reasoning, not just retrieval

The search tools (jcodemunch, codegraph, codebase-memory-mcp) answer one question:
*"Where is this symbol?"* codeloom answers the questions that actually matter
when an agent is working:

- **`--task "fix the login bug"`** → *"which files matter for THIS task?"* (ranked)
- **`--impact auth/login.py`** → *"what breaks if I change this?"* (blast radius)
- **`--plan "add retry"`** → *"read these files, in this order"* (prioritized)
- **`--pack "task"`** → *"here's the whole context, in one file"* (single-shot)

This is the moat. jcodemunch is retrieval-only — it can't prioritize, so it
can't do this. codeloom turns a query tool into a **context engine**:

```bash
# One command → the complete context for a task, pre-computed
codeloom --pack "fix the login bug" .
#   ## Reading order (most relevant first)
#   1. src/auth/login.py  (score 8, 3 keyword hits, 12 dependents)
#   2. src/auth/session.py (score 6, 2 keyword hits, 8 dependents)
#   ## Impact (what breaks if you change each)
#   ## Symbols in the relevant modules
```

An agent pastes that one file once and has everything it needs — **zero
per-query retrieval during the session.** That's the workflow jcodemunch can't
serve: it's built for repeated queries on a persistent index; codeloom is built
to load the whole context once and work offline.

## Quickstart

**macOS / Linux:**
```bash
# 1. Grab the one file (no pip, no deps)
curl -O https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py
chmod +x codeloom.py

# 2. Map any repo
./codeloom /path/to/repo

# 3. Tell your agent to read it first
echo "Before editing anything, run: codeloom" >> AGENTS.md
```

**Windows (PowerShell):**
```powershell
# 1. Grab the one file
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py" -OutFile codeloom.py

# 2. Map any repo
python codeloom.py C:\path\to\repo

# 3. Tell your agent to read it first
Add-Content AGENTS.md "Before editing anything, run: codeloom"
```

That's it. Under a second, zero setup, works offline. Cross-platform — macOS, Linux, Windows (Python 3.8+).

## Demo

![codeloom demo](demo.gif)

## What it gives your agent

| Command | What it answers |
|---|---|
| `codeloom` | "What's in this repo, and where?" — tree + one-liners + entry points |
| `codeloom --graph` | "What touches what?" — full import dependency graph |
| `codeloom --graph --focus X` | "What does X need, and what breaks if I change it?" |
| `codeloom --calls` | "What calls what?" — function-level execution flow (multi-language) |
| `codeloom --calls --focus X` | "What does this one module's code actually do?" |
| `codeloom --diff` | "What changed, and what's relevant to my current task?" |
| `codeloom --install-agents` | "Make every future agent session auto-load the map" |
| `codeloom --cost` | "How many tokens is this saving me?" |
| `codeloom --task "X"` | "Which files matter for task X?" |
| `codeloom --impact X` | "What breaks if I change X?" |
| `codeloom --plan "X"` | "Read these files, in this order, to do task X" |
| `codeloom --pack "X"` | "Give me the whole context for task X, in one file" |
| `codeloom --resume` | "Restore my structural map after a compaction" (compact snapshot) |
| `codeloom --checkpoint "note"` | Snapshot in-progress work (git diff + status note) to survive compaction |
| `codeloom --checkpoint-restore` | Read the last checkpoint back to resume in-progress work |
| `codeloom --loom "task"` | Intent engine: layered context for a task (overview→files→code→git→memory) |
| `codeloom --remember "note" --section X` | Append to persistent repository memory |
| `codeloom --adr "title" --context "..." --decision "..."` | Write a structured Architectural Decision Record |
| `codeloom --adr-list` | List saved Architectural Decision Records |
| `codeloom --churn` | Git intelligence: most-edited files (instability signal) |
| `codeloom --hybrid-search "q"` | Hybrid search: BM25 + structure + git scored; adds local embeddings if present (opt-in) |
| `codeloom --seen` | Session memory: already-read files/symbols (skip re-reading) |
| `codeloom --cross-repo A B ...` | One graph across multiple repos (frontend+backend+SDK) |
| `codeloom --architecture` | Detect architectural pattern (MVC/layered/DDD/monolith) |
| `codeloom --heatmap` | Dependency heatmap: god classes, circular imports, unused |
| `codeloom --explain-topic X` | Explain a domain end-to-end (files + call flow) |
| `codeloom --routes` | Extract HTTP routes: METHOD path → handler (framework-aware) |
| `codeloom --channels` | Pub-sub/event channel map (EMITS → LISTENS_ON) |
| `codeloom --export FILE` | Export a portable graph snapshot (symbols + edges + routes + channels) |
| `codeloom --docs readme\|arch` | Generate a README or ARCHITECTURE doc |
| `codeloom --refactor X` | Refactor engine: files, deps, risk, order for symbol X |
| `codeloom --rename OLD NEW` | What a rename touches: definitions, files, dependents, edges |
| `codeloom --ask "task"` | One-shot complete task brief: loom + blast radius + files-to-touch |
| `codeloom --bug-predict` | Bug prediction: files likely to break (churn+coupling+complexity) |
| `codeloom --timeline` | Repository timeline: architecture evolution via git |
| `codeloom --dedup` | Session dedupe: skip already-read files (show new delta) |
| `codeloom --plugin-sdk` | Show the plugin SDK (framework-aware extraction hooks) |
| `codeloom --lsp` | Show LSP bridge status (optional semantic enrichment) |
| `codeloom --graph-html` | Write a local zoomable HTML graph view |
| `codeloom --find "q"` | Natural-language flow discovery ("find where login starts") |
| `codeloom --files "*.py"` | Find files by name/glob ("engine" or "*.py") |
| `codeloom --context-diff BASE HEAD` | Branch-to-branch architecture-level diff |
| `codeloom --install-agent claude` | Print MCP config for 17 agents (claude/cursor/codex/gemini/opencode/cline/openhands/devin/hermes/aider/roo/windsurf/amazon-q/jetbrains/junie/kimi/qwen) |
| `codeloom --detect-agent` | Detect which installed agent's config dir is present |
| `codeloom --precision X` | Graph precision: call edges with confidence + class relationships |
| `codeloom --cross` | "What calls what, across files?" (resolved call graph) |
| `codeloom --search X` | "Where is symbol X defined?" (symbol index + snippet) |
| `codeloom --usages X` | "Where is symbol X used?" (call sites + snippet) |
| `codeloom --grep X` | "Where does this code pattern appear?" (snippet search) |
| `codeloom --read X` | "Show me the exact source of symbol X" (token-efficient) |
| `codeloom --explain X` | "What does symbol X do?" (plain-English, no LLM) |
| `codeloom --similar X` | "What's structurally similar to X?" (refactoring) |
| `codeloom --deadcode` | "What's defined but never called?" (dead code) |
| `codeloom --get-symbol X` | "Smallest snippet for X, with token count" (token-shaving) |
| `codeloom --snippet P S E` | "Extract bytes S-E from file P" (byte-range) |
| `codeloom --incremental` | "What changed since last run?" (hash-based cache) |
| `codeloom --verify FILE` | "Is this file the official codeloom?" (SHA-256) |

## Usage

```bash
codeloom                      # map current dir -> stdout
codeloom /path/to/repo        # map a specific repo
codeloom --write MAP.md       # also write to MAP.md
codeloom --json               # machine-readable JSON for tooling
codeloom --graph              # Python import dependency graph
codeloom --graph --focus X    # deps + dependents of module X
codeloom --calls              # function-level call graph (multi-language)
codeloom --calls --focus X    # calls inside one module
codeloom --diff               # structure of files changed vs HEAD (git)
codeloom --install-agents     # write/update AGENTS.md with a codeloom block
codeloom --cost               # append token-cost estimate to output
codeloom --impact X           # predict blast radius of changing module X
codeloom --task "text"        # rank modules relevant to a task
codeloom --plan "text"        # prioritized reading plan for a task
codeloom --cross              # cross-file call graph (resolved across modules)
codeloom --search SYMBOL      # search the symbol index (definitions + snippet)
codeloom --usages SYMBOL      # find where a symbol is used (call sites + snippet)
codeloom --grep QUERY         # search file contents for a snippet (ranked + context)
codeloom --read SYMBOL        # extract exact source of a function/class/method
codeloom --explain SYMBOL     # plain-English explanation of a symbol (no LLM)
codeloom --similar SYMBOL     # find structurally similar functions/classes
codeloom --deadcode           # find functions defined but never called
codeloom --get-symbol X       # token-counted symbol snippet (summary-first by default)
codeloom --get-symbol X --full # full source (opt-in)
codeloom --snippet P S E      # extract bytes S-E from file P
codeloom --incremental        # show files changed since last run (hash-based cache)
codeloom --verify FILE        # print SHA-256 of a file (security check)
codeloom --trace CMD          # run a command, record runtime call edges (needs --force)
codeloom --force              # acknowledge --trace executes code (isolation warning)
codeloom --install-grammars   # install tree-sitter grammars (opt-in precision)
codeloom --index              # build + save a persistent byte-offset index (scale)
codeloom --index-status       # show persistent index status/freshness
codeloom --no-outline         # skip per-file one-liners (faster)
codeloom --max-files 20000    # cap traversal (default 20000; raise for 10M+ LOC monorepos)
```

## Deep structural intelligence (`--cross`, `--search`)

The old weakness — "shallow/structural only, no cross-file call graphs, no
semantic index" — is gone. codeloom now does **deep AST analysis** and has a
**real symbol index**, still in one stdlib file:

```bash
# Cross-file call graph: resolve A.main() -> B.engine.run() across modules
codeloom --cross .
#   src.main.main() -> src.core.engine.Engine
#   src.core.engine.run() -> src.utils.retry.retry

# Search the symbol index (functions, classes, methods + where they're defined)
codeloom --search Engine .
#   Engine  [class]  src.core.engine:4
#     class Engine:
#     def __init__(self):

# Find where a symbol is USED (call sites, not just the definition)
codeloom --usages retry .
#   src.core.engine:8
#     def run(self, fn):
#         retry(fn)
```

`--cross` resolves calls to their defining module via Python `ast` — so an
agent sees the *real* execution flow across files, not just per-module noise.
`--search` is a true inverted index of every symbol, with module + line + a
context snippet, across Python and other languages. `--usages` answers "where
is this called?" — the question agents ask before editing — with call sites
and snippets, so the agent doesn't have to open files to see context.

## Snippet search (`--grep`)

Find the exact code, not just symbols. `--grep` searches file contents and
returns ranked matches with context lines — the "find this snippet" capability:

```bash
codeloom --grep "retry" .
#   src.core.engine:8
#     def run(self, fn):
#         retry(fn)
#   src.main:7
#     eng = Engine()
#     eng.run(retry)
```

Ranking: exact-word matches first, then substring, then case-insensitive.
This closes the snippet-search gap — the one thing codebase-memory-mcp does
that codeloom previously didn't.

## Token-efficient read (`--read`)

Read the exact source of a function, class, or method without burning tokens on
the whole file — jcodemunch's core value, integrated with codeloom's
task-orientation:

```bash
codeloom --read Engine .
# src.core.engine:4  [class]
# class Engine:
#     def __init__(self):
#         self.cfg = Config()
#     def run(self, fn):
#         retry(fn)
```

`--read` uses Python `ast` to extract the precise source lines of a symbol, so
the agent gets exactly the code it needs — not the whole file. For other
languages it uses tree-sitter (when a grammar is installed) or a brace-matching
fallback, so `--read` works across Python, JS/TS, Go, Rust, Java, C/C++, and
more. Combined with `--task` (what's relevant), `--impact` (what breaks), and
`--cross` (the call path), this is the full agent workflow: *find → read →
understand → edit*.

## Understanding & refactoring (`--explain`, `--similar`, `--deadcode`)

Beyond retrieval, codeloom helps the agent *understand* and *refactor*:

```bash
# Plain-English explanation of a symbol (AST + call graph, no LLM needed)
codeloom --explain Engine .
# Summary: class Engine:
# Calls (0):
# Called by (0):

# Find structurally similar functions/classes (same signature shape)
codeloom --similar run .
#   browser_use.actor.element.click  (3 params)
#   browser_use.actor.mouse.move    (3 params)

# Find dead code (defined but never called)
codeloom --deadcode .
#   src.core.engine.Engine
#   src.main.main
```

`--explain` gives the agent a plain-English summary of a symbol's role from its
AST + call graph — no LLM, no tokens. `--similar` finds refactoring candidates
with the same signature shape. `--deadcode` surfaces unused symbols so the agent
knows what's safe to remove.

## Token-shaving retrieval (`--get-symbol`, `--snippet`)

The "match jcodemunch's token-shaving" layer. `--get-symbol` is **summary-first
by default**: it returns the signature + docstring + call graph (not the full
source), with a token count — so even huge symbols cost ~10 tokens instead of
40k. Pass `--full` for the complete source when you need the implementation:

```bash
codeloom --get-symbol Agent .
# browser_use.agent.service:133  [class]  ~10 tokens (summary)
# Signature: Agent
# Docstring: Determine timeout based on model name
# Calls (0): none
# Called by (0): none
# Use `--get-symbol Agent --full` for the full source.
```

`--snippet` extracts an exact byte range from a file, returning the text +
token estimate + byte count. Both are the precise-retrieval primitives that
let agents budget their context window.

**The 95%+ proof** — `benchmarks/run.py --tokens` measures summary-first
retrieval vs a grep-and-read baseline:

| query | baseline | codeloom (summary) | savings |
|---|---|---|---|
| `Agent` (huge class) | 3,689 | 10 | **99.7%** |
| `click` (function) | 6,954 | 16 | **99.8%** |
| `extract` (function) | 2,558 | 31 | **98.8%** |

Summary-first retrieval turns the huge-symbol case (which used to *lose* 983%
tokens) into a 99%+ win. That's the honest 95%+ claim — measured, not marketed.
The same token-shaving edge, benchmarked on real repos (fastapi/express/gin) and
side-by-side against jcodemunch, is folded into the **unified comparison table**
in ["Why it's different"](#why-its-different). Full methodology in
[`benchmarks/`](benchmarks/README.md).

The honest caveat: codeloom returns a **summary** (signature + docstring +
call graph) by default, so it wins on token count by design — summary-first is
the point. You opt into `--full` when you need the implementation.

## Task-aware intelligence (`--task`, `--impact`, `--plan`)

The competitors are all *retrieval* tools — they help an agent **find** things.
codeloom is a *reasoning* tool: it connects structure to the **actual task** the
agent is working on. No competitor does this.

```bash
# Rank modules relevant to a task (token overlap + graph centrality)
codeloom --task "fix the login bug" .
# 1. src/auth/login.py  (score 8, 3 keyword hits, 12 dependents)
# 2. src/auth/session.py (score 6, 2 keyword hits, 8 dependents)

# Predict the blast radius of changing a module
codeloom --impact core/engine.py .
# risk: high
# ## Direct dependents (34) — most likely to break
#   src.main, tests.test_engine, ...

# Agent-native prioritized reading plan
codeloom --plan "add retry to engine" .
# Read these files, in this order, to understand the task:
# 1. src/core/engine.py  (why: 2 keyword matches, 2 modules depend on it)
# 2. src/utils/retry.py  (why: 1 keyword match, 3 modules depend on it)
```

`--impact` answers *"what breaks if I change this?"* before the agent edits.
`--task` and `--plan` turn codeloom from a passive map into a **task-orientation
engine** — the agent gets a prioritized reading list instead of a 468-file tree.

## Structural intelligence (`--graph`, `--focus`, `--calls`)

The map is the beginning. codeloom also builds a **real import dependency graph**
and a **function-level call graph** with Python's built-in `ast` module — no
daemon, no index, no deps. Tell your agent "what touches what" in under a second:

```bash
# Full import graph (385 modules, 1126 edges in <1s on browser-use)
codeloom --graph .

# Focus on ONE module: what it needs, and what needs it
codeloom --graph --focus browser_use/agent .
# focus: browser_use.agent
#   depends_on: browser_use.actor.*, browser_use.browser.*, ... (123 modules)
#   depended_on_by: tests.ci.test_beta_agent

# Function-level call graph (only calls to repo-defined functions)
codeloom --calls --focus browser_use/agent/service .
#   _execute_ai_step() -> extract_clean_markdown, get_ai_step_system_prompt, ...
#   _execute_history_step() -> _execute_ai_step, _format_element_for_error, ...
```

`--focus` accepts a file path, a package directory, or a dotted module name
(`browser_use/agent`, `browser_use/agent/service.py`, `agent.service`). It
answers the two questions agents burn the most tokens on: *"what does this
code need?"* and *"what else breaks if I change it?"*

## Git-aware `--diff` (the one competitors don't have)

Agents almost always work on a *specific change*, not a whole repo. `--diff`
shows the structure of **only the files changed vs `git HEAD`** — so the agent
knows exactly what's relevant to the current task, and it's *always fresh*
(no stale index):

```bash
codeloom --diff .
# codeloom --diff — 2 changed file(s)
# ## Changed files
#   src/core/engine.py
#   tests/test_engine.py
# ## Structure of changes
#   src/core/engine.py
#     class Engine:
#     def run(self, fn):
```

## One-command agent setup (`--install-agents`)

```bash
codeloom --install-agents .
# created /path/to/AGENTS.md
```

Writes (or updates) an `AGENTS.md` with a codeloom instruction block, so every
future agent session auto-loads the map before touching anything. Set and forget.

## Token-cost reporting (`--cost`)

```bash
codeloom --cost .
# ## Cost
#   ~308 tokens (~1234 bytes) — vs ~40k+ tokens for grep+read on a large repo
```

Quantify the win. Numbers beat claims.

## Incremental mode (`--incremental`)

Repeated runs on large repos shouldn't re-parse everything. `--incremental`
uses a hash-based cache (`.codeloom-cache.json`) — no daemon, no index — and
reports only the files that changed since the last run:

```bash
codeloom --incremental .
# codeloom --incremental — 1 changed file(s) since last run
# ## Changed files
#   src/utils/retry.py
```

## Security check (`--verify`)

Running a single-file script downloaded from the web carries risk. `--verify`
prints the SHA-256 of a file so you can confirm a downloaded copy is the
official codeloom and not tampered with:

```bash
codeloom --verify codeloom.py
# sha256: <current release checksum — see GitHub releases>
```

## Why no daemon (the MCP server is better)

A daemon keeps the index in RAM for fast repeated queries — but it goes stale,
needs a separate process to manage, and uses resources even when idle. codeloom
doesn't need one:

- **The MCP server is the daemon.** When an agent registers `codeloom-mcp.py`,
  it stays resident in the agent's process and answers repeated queries from an
  in-memory index — daemon-speed, no separate process.
- **Always fresh.** The in-memory index re-parses *only changed files* (via
  content hashes) on each query, so it never goes stale like a daemon's index.
- **Nothing to crash.** No background process, no idle RAM/CPU, no operational
  surface. Copy one file, done.
- **Zero-dep, single file.** The whole thing — CLI + MCP server + in-memory
  index — is stdlib-only.

That's the honest tradeoff: a daemon is faster for *thousands of queries per
second on a 28M-LOC monorepo*. For an AI agent working on a repo, the MCP
server delivers the same in-memory speed with none of the daemon's costs.

## Scale without a daemon (`--index`)

For large repos, `codeloom --index` builds a **persistent on-disk knowledge
graph** — a byte-offset symbol index **plus call/import edges** — once, then
`--get-symbol`/`--search`/`--cross`/`--deadcode` load it in milliseconds
instead of re-parsing everything:

```bash
codeloom --index .            # build + save the knowledge graph (symbols + call/import edges)
codeloom --get-symbol Agent . # loads from the index in ~0.1s
codeloom --deadcode .         # loads call edges from the graph — no re-parse
codeloom --index-status .     # is the index fresh?
```

The knowledge graph survives across invocations (no daemon, no background
process) and is incrementally refreshed via content hashes. This is the
multi-million-line monorepo win: the same call/import edges a daemon keeps
resident, stored on disk, so heavy ops load in milliseconds instead of
re-parsing. Measured on microsoft/vscode (12k files): `--deadcode` drops from
10.3s (serial) to **4.8s** by loading from the graph; `--get-symbol` cold-starts
at **~0.11s** (index load, no re-parse).

**Tested on the Linux kernel (~28M LOC, 95k files):** with the optional C
accelerator (`--engine c`), the **full knowledge graph + symbol index builds
the kernel in ~91s** — 64,814 code files → **3.2M symbols, 408k call/import
edges**. The C core (one `cc -O3` build, `codeloom_core.c`) does fast
multi-language file-walk + symbol + call + import extraction, sharded across
cores; the symbol index alone is ~13s. The pure-Python default stays
zero-dependency. Tree-sitter walks are iterative so deeply-nested kernel
source doesn't hit the recursion limit.

**No cold-start on repeated queries.** `--get-symbol` uses a **lazy per-symbol
index** (stdlib dbm, one keyed record per symbol) — a single lookup is a
near-resident ~ms read, not a full multi-hundred-MB dict deserialize. And
`codeloom --watch` is the **daemon-less incremental refresh**: it re-indexes
only the files that changed (via mtime/size) and updates the lazy store in
place — so an agent can call `--watch` on-demand and always hit a fresh index
without a full rebuild or a managed daemon. The MCP server keeps the graph
resident in memory for the session — so repeated `--get-symbol`/`--deadcode`
calls in one session are served from the resident graph, no re-parse, no
cold-start. With the C engine built, `codeloom --watch-core` is a **native
recursive file watcher** (kqueue/inotify) and `codeloom --serve` is a
**C-resident index server** that answers symbol lookups sub-ms with zero
Python per query. That's the daemon's "hot in memory" benefit without a separate
daemon process to run or crash.

## The three parts (CLI + MCP + skill)

For full agent use, codeloom is three pieces that work together:

| Piece | What it is | Role |
|---|---|---|
| **`codeloom.py`** (CLI) | The engine | Does the actual analysis |
| **`codeloom-mcp.py`** (MCP server) | The tool interface | Lets the agent *call* codeloom as a native tool |
| **`skills/codeloom/SKILL.md`** (skill) | The procedural knowledge | Tells the agent *how* to use codeloom — which command for which task, the workflow, the pitfalls |

The CLI is the engine; the MCP server is what makes codeloom a first-class agent
tool (native tool calls, the resident in-memory graph, the natural-language
`codeloom_ask` entry point); the skill is what turns it from "a tool the agent
*could* call" into "a tool the agent *uses correctly*" — knowing when to run
`--task` vs `--impact` vs `--pack`. All three are zero-dependency and ship in
the repo.

## CI action (one-line onboarding)

Add codeloom to a repo in one command — it writes `AGENTS.md` **and** the
`codeloom-map` GitHub Action that runs `--pack` on every PR and posts the task
brief as a comment:

```bash
codeloom --install-agents .
# writes AGENTS.md + .github/workflows/codeloom-map.yml
```

The action uses the PR title as the task, runs `codeloom --pack`, and posts the
code-embedded brief as a PR comment — so every PR gets a task-shaped map of what
it touches, automatically. jcodemunch can't ship this as a one-liner; codeloom
does.

## MCP server (agents call codeloom natively)

`codeloom-mcp.py` is a **zero-dependency MCP server** (stdlib JSON-RPC over
stdio — no `mcp` package, no daemon). Register it with any MCP-capable agent.
`codeloom --install-agent AGENT` prints the exact config for **17 agents** —
Claude Code, Codex, Cursor, Gemini CLI, OpenCode, Cline, OpenHands, Devin,
Hermes Agent, Aider, Roo Code, Windsurf, Amazon Q, JetBrains, Junie, Kimi CLI,
and Qwen Code (or auto-detect with `--detect-agent`):

```json
// Claude Code / Cursor / Codex MCP config
// macOS/Linux: "command": "python3"   Windows: "command": "python"
{
  "codeloom": {
    "command": "python3",
    "args": ["/path/to/codeloom-mcp.py"]
  }
}
```

Exposes **61 tools**: `codeloom_map`, `codeloom_graph`, `codeloom_focus`,
`codeloom_calls`, `codeloom_diff`, `codeloom_impact`, `codeloom_task`,
`codeloom_plan`, `codeloom_pack`, `codeloom_cross`, `codeloom_search`,
`codeloom_usages`, `codeloom_grep`, `codeloom_read`, `codeloom_explain`,
`codeloom_similar`, `codeloom_deadcode`, `codeloom_get_symbol`,
`codeloom_snippet`, `codeloom_incremental`, `codeloom_verify`,
`codeloom_trace`, `codeloom_ask` (single natural-language entry point),
`codeloom_framework`, `codeloom_session_report`, `codeloom_watch`,
`codeloom_loom`, `codeloom_remember`, `codeloom_churn`,
`codeloom_hybrid_search`, `codeloom_seen`, `codeloom_cross_repo`,
`codeloom_architecture`, `codeloom_heatmap`, `codeloom_explain_topic`,
`codeloom_docs`, `codeloom_refactor`, `codeloom_rename`,
`codeloom_bug_predict`, `codeloom_timeline`, `codeloom_dedup`,
`codeloom_plugin_sdk`, `codeloom_lsp`, `codeloom_lsp_symbol`,
`codeloom_langs`, `codeloom_graph_html`, `codeloom_find`,
`codeloom_context_diff`, `codeloom_check_edit`, `codeloom_check_delete`,
`codeloom_resume`, `codeloom_precision`, and more. Your agent can build
a mental model, trace execution flow across files, see what changed, predict
blast radius, get a task-oriented reading plan or one-shot task brief
(`--ask`), search any symbol, find where it's used, grep for snippets, read
exact symbol source, explain a symbol, check a rename's blast radius, find
refactoring candidates, detect dead code, retrieve token-counted snippets,
extract byte ranges, verify a download, capture runtime call edges, detect the
framework, and summarize session cost — natively, no install, no index.

## Supported agents

codeloom drops into **17 coding agents** with the exact MCP config each expects.
`codeloom --install-agent AGENT` prints it; `codeloom --detect-agent` finds
which one is already configured on your machine:

| Agent | Install | Config format |
|---|---|---|
| Claude Code | `codeloom --install-agent claude` | `mcpServers` JSON |
| Codex | `codeloom --install-agent codex` | `mcpServers` JSON |
| Cursor | `codeloom --install-agent cursor` | `mcpServers` JSON |
| Gemini CLI | `codeloom --install-agent gemini` | `mcpServers` JSON |
| OpenCode | `codeloom --install-agent opencode` | bare `{codeloom:{…}}` |
| Cline | `codeloom --install-agent cline` | `mcpServers` JSON |
| OpenHands | `codeloom --install-agent openhands` | TOML `[mcp_servers.codeloom]` |
| Devin | `codeloom --install-agent devin` | TOML `[mcp_servers.codeloom]` |
| Hermes Agent | `codeloom --install-agent hermes` | TOML `[mcp_servers.codeloom]` |
| Aider | `codeloom --install-agent aider` | `mcpServers` JSON |
| Roo Code | `codeloom --install-agent roo` | `mcpServers` JSON |
| Windsurf | `codeloom --install-agent windsurf` | `mcpServers` JSON |
| Amazon Q Developer | `codeloom --install-agent amazon-q` | `mcpServers` JSON |
| JetBrains IDEs | `codeloom --install-agent jetbrains` | `mcpServers` JSON |
| Junie | `codeloom --install-agent junie` | `mcpServers` JSON |
| Kimi CLI | `codeloom --install-agent kimi` | `mcpServers` JSON |
| Qwen Code | `codeloom --install-agent qwen` | `mcpServers` JSON |

## How it works

codeloom is **three parts working together**, all zero-dependency:

| Part | Language | Role |
|---|---|---|
| `codeloom.py` | **Python** (stdlib) | The engine — walks, parses, graphs, and the CLI |
| `codeloom-mcp.py` | **Python** (stdlib) | The MCP server your agent calls as native tools |
| `codeloom_core.c` | **C** (optional) | The compiled accelerator — faster scan, native watcher, resident server |

**Python (stdlib only).** The core CLI and engine use only the standard library:

- **Tree + outlines** — walks the repo, respects `.gitignore` (full pattern
  support: negation, anchoring, `**`, dir-only), guards against symlink loops,
  and reads the top-level declarations of each file across 130+ extensions.
- **Import graph** — parses Python with the built-in `ast` module to resolve
  absolute, relative, importer-relative, and source-root-relative imports into
  a dependency graph. Non-Python languages (JS/TS, Go, Rust, Java, C/C++, C#,
  Ruby, PHP, Swift, Kotlin, Dart, Lua) use best-effort regex import detection.
- **Call graph** — traces which functions call which, filtering to only
  repo-defined functions so you see real execution flow, not builtin noise.
- **Cross-file call graph** — resolves calls to their defining module via
  `ast` + import maps, so `A.main() -> B.engine.run()` works across files.
- **Symbol index** — a true inverted index of every function, class, and method
  with module + line + context snippet, searchable via `--search` and `--usages`.
- **Incremental cache** — a hash-based `.codeloom-cache.json` (no daemon) so
  repeated runs only re-parse changed files.
- **MCP server** — a minimal JSON-RPC 2.0 stdio transport, so any MCP client
  can call codeloom without installing the `mcp` package.

**Shell (macOS / Linux / Windows).** The CLI is a plain command you run from any
shell — `codeloom .`, `codeloom --ask "task" .`, `codeloom --export map.json .`.
No daemon, no background process, no network. It reads your files, computes the
structure, prints it, and exits.

**C (integrated accelerator).** For huge monorepos, `codeloom_core.c` is a
compile-once accelerator that **auto-builds on first use** — `_find_core()`
compiles it from the committed source (`cc -O3 -o codeloom_core
codeloom_core.c`) the first time you use `--engine c`, no manual step, no
download. It does a faster multi-language file-walk + symbol + call + import
scan (sharded across cores), a **native recursive file watcher**
(`--watch-core`, kqueue/inotify), and a **C-resident index server** (`--serve`)
that answers symbol lookups sub-ms with zero Python per query. The default
pure-Python path stays zero-dependency; the C core is strictly optional and
compiles from the auditable committed source.

No indexing daemon, no background process, no network. It reads your files,
computes the structure, prints it, and exits.

## Known limits (honest) — and how each is removed

codeloom trades precision for its single-file/zero-dep design. But every limit
now has an **optional progressive-enhancement backend** that removes it when the
richer tool is present — the zero-dep core always works, and the precision
ceiling rises when you opt in:

- **Regex multi-language analysis is best-effort.** *Improved:* codeloom now uses
  a **string/comment-aware scanner** that strips strings and comments before
  matching, so it doesn't false-positive on `helper()` inside a string or
  comment. *Removed by:* `codeloom --install-grammars --yes` (or `pip install
  tree-sitter tree-sitter-python ...`). codeloom then uses real AST parsing for
  the **tree-sitter language set** — a data-driven resolver dynamically imports
  ANY installed grammar (core + long-tail) for real AST depth, auto-installing
  per-language on demand (`CODELOOM_AUTO_INSTALL_GRAMMARS=1`), falling back to
  regex when a grammar isn't installed. Plus a **130+ extension regex/C
  extraction** for recognition everywhere. TypeScript/JavaScript (incl.
  `.tsx/.jsx/.mjs`), Rust (struct/enum/trait/impl), Svelte, Vue, Astro, Scala,
  Erlang, Solidity, Terraform/HCL, Nix, COBOL, VB.NET, Pascal, ArkTS, Metal,
  Lua/Luau, R, Fortran, Ada, Verilog/VHDL, SystemVerilog, Objective-C, Perl,
  Bash/Zsh, PowerShell, Julia, Tcl, CoffeeScript, and more. `codeloom --langs`
  lists them all.
- **Static analysis misses runtime wiring.** *Removed by:* `codeloom --trace CMD
  --force` runs a command (e.g. your test suite) under `sys.settrace` and
  records the ACTUAL call edges — capturing dynamic imports and monkeypatching
  that no static analyzer (including tree-sitter) can see. `--force` is required
  because `--trace` executes code; run it in an isolated sandbox/CI job.
- **Task scoring is a heuristic.** *Removed by:* set `CODELOOM_EMBED_BASE_URL` +
  `CODELOOM_EMBED_API_KEY` (OpenAI-compatible) or install `sentence-transformers`.
  codeloom then uses real semantic embeddings for `--task` relevance instead of
  token overlap.
- **Import resolution is heuristic.** Handles common layouts, namespace packages,
  source-root-relative imports, and multi-root workspaces (via `pyproject.toml` /
  `package.json` / `go.mod`); unusual `sys.path` setups may still mis-resolve.
- **Knowledge graph is on-disk, not resident.** `--index` builds a persistent
  knowledge graph (symbols + call/import edges) that heavy ops load from — but
  it's loaded per-invocation, not kept resident by a daemon. The MCP server
  keeps the graph resident in memory for the session, giving you daemon-speed
  repeated queries without a background process. For a truly enormous
  multi-million-line repo where you query constantly, a background daemon that
  keeps the graph hot is one option — codeloom's no-daemon model trades a small
  cold-start cost for zero operational surface.

## Correctness

- **Nested `.gitignore`** — codeloom merges subdirectory `.gitignore` files as it
  walks (gitignore semantics), so a nested ignore is honored.
- **Cache invalidation** — the incremental cache tracks `.gitignore` hashes; if
  a `.gitignore` changes, the cached file list is invalidated and re-walked.
- **Workspace roots** — imports resolve against detected package roots
  (`pyproject.toml` / `package.json` / `go.mod`), handling multi-root monorepos.
- **`--trace` safety** — requires `--force` (it executes code); run in an
  isolated sandbox/CI job.

codeloom covers the full stack in one file: tree-sitter precision (via
`--install-grammars` / `--auto-grammars`), a persistent knowledge graph
(`--index`), and snippet-level retrieval (`--search`/`--get-symbol`). On top
of that it does what the search tools don't — task-orientation and the
code-embedded `--ask`/`--pack` brief — with **speed, zero-setup, freshness,
and offline operation** as the default.

## Why it's different

Honest comparison against the field. codeloom wins decisively on the axis
that matters for everyday agent work: **task-shaped context, zero install,
compaction survival, and Linux-kernel-class index speed** — while keeping a
single zero-dependency file.

| | codeloom | jcodemunch | codegraph | codebase-memory-mcp |
|---|---|---|---|---|
| Install | **one file, copy it** | `pip install` + MCP + deps | compiled Rust binary | `pip install` + deps |
| Setup | none | index once | watch/daemon | indexing daemon |
| Runs on | **stdlib only** (C core opt-in) | tree-sitter + index | Rust + SQLite + FTS5 | compiled C engine |
| Time to first result | **< 1s** | after indexing | after indexing | after indexing |
| Always fresh | **yes (reads live)** | re-index | native watcher | re-index |
| **Linux kernel index** | **~91s** (`--engine c`) | — | Rust tree-sitter build | ~180s (3 min) |
| **Task context (`--ask`)** | **yes — complete brief** | no | no | no |
| **Compaction survival (`--resume`)** | **yes** | no | no | no |
| **Code-embedded `--pack`** | **yes** | partial | no | partial |
| **Reading plan (`--plan`)** | **yes** | no | no | no |
| **Blast radius (`--impact`/`--refactor`/`--rename`)** | **yes** | partial | yes | yes |
| **Token-shaving (`--get-symbol` summary-first, 99% savings)** | **yes** | partial (full source) | no | partial |
| **Byte-range snippets (`--snippet`)** | **yes** | yes | no | partial |
| Import graph | **yes — `--graph`** | partial | yes | yes |
| Cross-file call graph | **yes — `--cross`** | yes | yes | yes |
| Symbol index / search | **yes — `--search`** | yes | yes | yes |
| **Hybrid search (semantic opt-in)** | **yes** | no | no | yes |
| **Graph precision (`--precision`)** | **yes** | no | no | no |
| **Repo memory (`--remember`/`--loom`)** | **yes** | no | no | no |
| Native file watcher | **yes — `--watch-core`** | no | yes | — |
| Sub-ms resident lookups | **yes — `--serve`** | yes | yes | yes |
| **Runtime call edges (`--trace`)** | **yes** | no | no | no |
| Offline | **yes** | yes | yes | yes |
| **MCP tools** | **61** | 91 actions (6 routers) | ~10 | varies |
| Zero-dependency single file | **yes** | no | no | no |

The heavyweight tools are great at retrieval — but they're *search engines*,
not *task-orientation engines*. codeloom does retrieval (token-shaving, byte
offsets) *and* tells the agent what matters, what breaks, and what to read
first — the reasoning the search tools don't do. And it's the fastest possible
structural context, in one file, in under a second, always fresh.

**On tool count (verified by driving the actual jcodemunch server):** jcodemunch
exposes **6 MCP tools** (`set_tool_tier`, `announce_model`, `jcodemunch_guide`,
`order`, `menu`, `route`) that route to **91 internal actions** (`index_repo`,
`search_symbols`, `get_symbol_source`, etc.) — and its own users report it as
"30 tools" over the 50-tool limit (#297). codeloom has **61 MCP tools** and,
critically, **1 natural-language entry point** (`codeloom_ask`) that routes
deterministically — so the agent's effective surface is **1 tool**, not
6-over-91 or 30. That's the routing complexity that causes jcodemunch's 30%
misfire, and it's the thing codeloom's design eliminates.

## What jcodemunch users asked for — and codeloom delivers

From jcodemunch's own issue tracker, the pain points users raised, and how
codeloom's design answers them:

| User pain (jcodemunch issue) | codeloom |
|---|---|
| **Too many tools** (#297: 109 tools across jMunch, over the 50 limit) | **61 tools + 1 entry point** (`codeloom_ask`) |
| **Token overhead on grep tasks** (#142: 1.31x more tokens, 2.43x cache reads) | **summary-first `--get-symbol` + `--pack`** (token-minimal by design) |
| **Framework-aware intelligence** (#201: Laravel/Next.js conventions) | **`--framework`** (detects framework + routes/models/config/conventions) |
| **Install friction** (#308, #273: PyPI unavailable, hook support) | **one file, copy it** + `pip install codeloom` |
| **Indexing hangs / daemon CPU** (#375, #275, #68, #356) | **no daemon**, `--index` builds in ~1s |
| **Config bugs** (#300, #416: config ignored) | **zero config** — no config file to break |

**The honest scope note:** codeloom is a *code* intelligence tool. It matches
jcodemunch (code) and beats it on these pain points. jdocmunch (documentation)
and jdatamunch (data/schema) are separate domains codeloom doesn't target —
claiming to replace all three would be an overclaim.

## For humans too

codeloom isn't just for agents. Onboarding to a new codebase, auditing a repo
you inherited, or just reminding yourself where things live — `codeloom` gives
you the same instant structural overview a senior dev has after a week. It's
the fastest way to answer "what is this project, actually?"

## Roadmap

- [x] Multi-language outline extraction (Python, JS/TS, Go, Rust, Java, Ruby, PHP, C/C++, C#, Swift, Kotlin, shell, Lua, Elixir, Dart, OCaml, Scala, R)
- [x] `.gitignore`-aware traversal
- [x] JSON output for tooling
- [x] Import dependency graph + `--focus` (Python, via `ast`)
- [x] Function-level call graph (`--calls`, multi-language)
- [x] Zero-dependency MCP server (`codeloom-mcp.py`)
- [x] Git-aware `--diff` (structure of changed files)
- [x] `--install-agents` (auto-write AGENTS.md)
- [x] Token-cost reporting (`--cost`)
- [x] Task-aware relevance (`--task`)
- [x] Change-impact prediction (`--impact`)
- [x] Agent-native reading plan (`--plan`)
- [x] Single-shot context packing (`--pack`, the moat feature)
- [x] Cross-file call graph (`--cross`, AST-resolved)
- [x] Symbol index + search (`--search`, with snippets)
- [x] Usage search (`--usages`, call sites + snippets)
- [x] Snippet search (`--grep`, ranked + context)
- [x] Token-efficient read (`--read`, exact symbol source via AST)
- [x] Symbol explanation (`--explain`, AST + call graph, no LLM)
- [x] Similar-symbol search (`--similar`, refactoring)
- [x] Dead-code detection (`--deadcode`)
- [x] Token-counted symbol retrieval (`--get-symbol`, byte offsets + tokens)
- [x] Summary-first retrieval (`--get-symbol` default, 95%+ token savings)
- [x] String/comment-aware scanner (precise zero-dep multi-language parsing)
- [x] Byte-range snippet extraction (`--snippet`)
- [x] Token-consumption benchmark (`benchmarks/run.py --tokens`)
- [x] In-memory index in MCP server (incremental, always fresh — no daemon)
- [x] Nested `.gitignore` merging + cache invalidation on `.gitignore` change
- [x] Workspace-root import resolution (pyproject/package.json/go.mod)
- [x] `--trace` isolation warning (`--force` required)
- [x] `--install-grammars` (one-command opt-in tree-sitter installer)
- [x] Persistent on-disk index (`--index`, scale without a daemon)
- [x] Persistent parsed cache (repeated runs skip re-parsing)
- [x] Benchmark harness + CI + release script (trust/credibility)
- [x] Incremental mode (`--incremental`, hash-based cache)
- [x] Multi-language import graph (JS/TS, Go, Rust, Java, C/C++, C#, Ruby, PHP, Swift, Kotlin, Dart, Lua)
- [x] Full `.gitignore` support (negation, anchoring, `**`, dir-only)
- [x] Symlink loop protection + Windows-safe path handling
- [x] Security check (`--verify`, SHA-256)
- [x] Optional tree-sitter backend (multi-language precision)
- [x] Optional embedding backend (task-scoring precision)
- [x] Runtime trace mode (`--trace`, static blind spots)
- [x] Persistent knowledge-graph index (symbols + call/import edges)
- [x] Framework-aware HTTP routes (`--routes`: METHOD path → handler)
- [x] Pub-sub / event channel map (`--channels`: EMITS → LISTENS_ON)
- [x] Portable graph snapshot export (`--export`: commit-and-share)
- [x] Repo-aware AST depth (`--auto-grammars`: installs grammars for your repo's languages)
- [x] `--files` find-by-name/glob + `--grep` searches docs (self-maintaining)
- [x] MCP config for 17 coding agents (`--install-agent` + `--detect-agent`)

## Benchmarks

Honest, reproducible numbers live in
[`benchmarks/`](benchmarks/README.md) — including the load-once benchmark that
shows the code-embedded `--pack` brief. Run them yourself:

```bash
python3 benchmarks/run.py --repo /path/to/repo
python3 benchmarks/load_once.py --repo /path/to/repo --task "fix the login bug"
```

## Documentation

- **[`CAPABILITIES.md`](CAPABILITIES.md)** — everything codeloom can do, in one place
- **[`FEATURES.md`](FEATURES.md)** — the strategic feature map (intent engine, memory, git intelligence, all 27 features)
- **[`USER_GUIDE.md`](USER_GUIDE.md)** — practical walkthrough for agent work
- **[`CLI.md`](CLI.md)** — every command, flag, and argument
- **[`SECURITY.md`](SECURITY.md)** — the trust model, verification, and what codeloom does NOT do
- **[`benchmarks/`](benchmarks/README.md)** — honest, reproducible numbers vs the competitors

## Trust & verification

- **CI** — the test suite runs on Linux, macOS, and Windows (Python 3.8–3.12)
  via GitHub Actions (`.github/workflows/ci.yml`).
- **Checksums** — every release ships the SHA-256 of `codeloom.py`; verify a
  downloaded copy with `codeloom --verify codeloom.py`.
- **Releases** — tagged, versioned releases with the demo GIF and checksum via
  `scripts/release.sh`.
- **One file, readable** — the whole tool is a single stdlib file you can audit
  before running. No hidden deps, no network calls, no telemetry.

## Contributing

PRs welcome. The whole thing is one file — easy to read, easy to test. Run the
suite with `python3 tests.py`. Ideas that fit the ethos: keep it zero-dependency,
keep it fast, keep it one file.

## Agent skill

A ready-to-load skill for using and maintaining codeloom ships in
[`skills/codeloom/SKILL.md`](skills/codeloom/SKILL.md) — covers every flag, MCP
wiring, the test suite, re-recording the demo GIF, and how to extend the tool.

The skill is the third part of the full agent experience (CLI + MCP + skill):
it tells the agent *how* to use codeloom correctly — which command for which
task, the workflow, and the pitfalls — so it uses `--task`/`--impact`/`--pack`
at the right times instead of guessing. Install it into your agent's skill
directory (e.g. `~/.hermes/skills/software-development/codeloom/`) to make
codeloom a first-class tool your agent uses well.

## License

MIT — do whatever you want with it.

---

*Built for the people who'd rather their AI agent actually ship code than spend 15 minutes reading a 40k-LoC repo.*
