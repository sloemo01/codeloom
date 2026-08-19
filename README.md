<h1 align="center">codemap</h1>
<p align="center">
  <b>Give your AI coding agent a map of the repo in one second.</b><br/>
  Single-file · zero-dependency · no daemon · 100% local · <b>import graph + call graph + MCP + skill</b>
</p>

<p align="center">
  <a href="#demo"><img src="https://img.shields.io/badge/see%20it%20run-Demo-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codemap/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.8+-blue"/></a>
  <a href="https://github.com/sloemo01/codemap/stargazers"><img src="https://img.shields.io/github/stars/sloemo01/codemap"/></a>
</p>

---

Every AI coding agent (Claude Code, Cursor, Codex, Gemini) has the same problem:
before it can *do* anything, it has to figure out what your codebase even *is*.
So it greps, reads whole files, burns 40,000+ tokens just to build context — and
then it forgets what it learned.

**codemap fixes that.** One command → a compact structural model of your repo
(folder tree + per-module one-liners + entry points + import graph + call graph)
that your agent reads in a second to build a mental model *before* touching
anything. No install. No daemon. No GPU. Runs 100% on your machine.

## Quickstart

**macOS / Linux:**
```bash
# 1. Grab the one file (no pip, no deps)
curl -O https://raw.githubusercontent.com/sloemo01/codemap/main/codemap.py
chmod +x codemap.py

# 2. Map any repo
./codemap /path/to/repo

# 3. Tell your agent to read it first
echo "Before editing anything, run: codemap" >> AGENTS.md
```

**Windows (PowerShell):**
```powershell
# 1. Grab the one file
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/sloemo01/codemap/main/codemap.py" -OutFile codemap.py

# 2. Map any repo
python codemap.py C:\path\to\repo

# 3. Tell your agent to read it first
Add-Content AGENTS.md "Before editing anything, run: codemap"
```

That's it. Under a second, zero setup, works offline. Cross-platform — macOS, Linux, Windows (Python 3.8+).

## Demo

![codemap demo](demo.gif)

## What it gives your agent

| Command | What it answers |
|---|---|
| `codemap` | "What's in this repo, and where?" — tree + one-liners + entry points |
| `codemap --graph` | "What touches what?" — full import dependency graph |
| `codemap --graph --focus X` | "What does X need, and what breaks if I change it?" |
| `codemap --calls` | "What calls what?" — function-level execution flow (multi-language) |
| `codemap --calls --focus X` | "What does this one module's code actually do?" |
| `codemap --diff` | "What changed, and what's relevant to my current task?" |
| `codemap --install-agents` | "Make every future agent session auto-load the map" |
| `codemap --cost` | "How many tokens is this saving me?" |
| `codemap --task "X"` | "Which files matter for task X?" |
| `codemap --impact X` | "What breaks if I change X?" |
| `codemap --plan "X"` | "Read these files, in this order, to do task X" |
| `codemap --cross` | "What calls what, across files?" (resolved call graph) |
| `codemap --search X` | "Where is symbol X defined?" (symbol index + snippet) |
| `codemap --usages X` | "Where is symbol X used?" (call sites + snippet) |
| `codemap --grep X` | "Where does this code pattern appear?" (snippet search) |
| `codemap --read X` | "Show me the exact source of symbol X" (token-efficient) |
| `codemap --explain X` | "What does symbol X do?" (plain-English, no LLM) |
| `codemap --similar X` | "What's structurally similar to X?" (refactoring) |
| `codemap --deadcode` | "What's defined but never called?" (dead code) |
| `codemap --get-symbol X` | "Smallest snippet for X, with token count" (token-shaving) |
| `codemap --snippet P S E` | "Extract bytes S-E from file P" (byte-range) |
| `codemap --incremental` | "What changed since last run?" (hash-based cache) |
| `codemap --verify FILE` | "Is this file the official codemap?" (SHA-256) |

## Usage

```bash
codemap                      # map current dir -> stdout
codemap /path/to/repo        # map a specific repo
codemap --write MAP.md       # also write to MAP.md
codemap --json               # machine-readable JSON for tooling
codemap --graph              # Python import dependency graph
codemap --graph --focus X    # deps + dependents of module X
codemap --calls              # function-level call graph (multi-language)
codemap --calls --focus X    # calls inside one module
codemap --diff               # structure of files changed vs HEAD (git)
codemap --install-agents     # write/update AGENTS.md with a codemap block
codemap --cost               # append token-cost estimate to output
codemap --impact X           # predict blast radius of changing module X
codemap --task "text"        # rank modules relevant to a task
codemap --plan "text"        # prioritized reading plan for a task
codemap --cross              # cross-file call graph (resolved across modules)
codemap --search SYMBOL      # search the symbol index (definitions + snippet)
codemap --usages SYMBOL      # find where a symbol is used (call sites + snippet)
codemap --grep QUERY         # search file contents for a snippet (ranked + context)
codemap --read SYMBOL        # extract exact source of a function/class/method
codemap --explain SYMBOL     # plain-English explanation of a symbol (no LLM)
codemap --similar SYMBOL     # find structurally similar functions/classes
codemap --deadcode           # find functions defined but never called
codemap --get-symbol X       # token-counted symbol snippet (summary-first by default)
codemap --get-symbol X --full # full source (opt-in)
codemap --snippet P S E      # extract bytes S-E from file P
codemap --incremental        # show files changed since last run (hash-based cache)
codemap --verify FILE        # print SHA-256 of a file (security check)
codemap --trace CMD          # run a command, record runtime call edges (needs --force)
codemap --force              # acknowledge --trace executes code (isolation warning)
codemap --install-grammars   # install tree-sitter grammars (opt-in precision)
codemap --index              # build + save a persistent byte-offset index (scale)
codemap --index-status       # show persistent index status/freshness
codemap --no-outline         # skip per-file one-liners (faster)
codemap --max-files 2000     # cap traversal (default 5000)
```

## Deep structural intelligence (`--cross`, `--search`)

The old weakness — "shallow/structural only, no cross-file call graphs, no
semantic index" — is gone. codemap now does **deep AST analysis** and has a
**real symbol index**, still in one stdlib file:

```bash
# Cross-file call graph: resolve A.main() -> B.engine.run() across modules
codemap --cross .
#   src.main.main() -> src.core.engine.Engine
#   src.core.engine.run() -> src.utils.retry.retry

# Search the symbol index (functions, classes, methods + where they're defined)
codemap --search Engine .
#   Engine  [class]  src.core.engine:4
#     class Engine:
#     def __init__(self):

# Find where a symbol is USED (call sites, not just the definition)
codemap --usages retry .
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
codemap --grep "retry" .
#   src.core.engine:8
#     def run(self, fn):
#         retry(fn)
#   src.main:7
#     eng = Engine()
#     eng.run(retry)
```

Ranking: exact-word matches first, then substring, then case-insensitive.
This closes the snippet-search gap — the one thing semble does that codemap
previously didn't.

## Token-efficient read (`--read`)

Read the exact source of a function, class, or method without burning tokens on
the whole file — jcodemunch's core value, integrated with codemap's
task-orientation:

```bash
codemap --read Engine .
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

Beyond retrieval, codemap helps the agent *understand* and *refactor*:

```bash
# Plain-English explanation of a symbol (AST + call graph, no LLM needed)
codemap --explain Engine .
# Summary: class Engine:
# Calls (0):
# Called by (0):

# Find structurally similar functions/classes (same signature shape)
codemap --similar run .
#   browser_use.actor.element.click  (3 params)
#   browser_use.actor.mouse.move    (3 params)

# Find dead code (defined but never called)
codemap --deadcode .
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
codemap --get-symbol Agent .
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

| query | baseline | codemap (summary) | savings |
|---|---|---|---|
| `Agent` (huge class) | 3,689 | 10 | **99.7%** |
| `click` (function) | 6,954 | 16 | **99.8%** |
| `extract` (function) | 2,558 | 31 | **98.8%** |

Summary-first retrieval turns the huge-symbol case (which used to *lose* 983%
tokens) into a 99%+ win. That's the honest 95%+ claim — measured, not marketed.

## Task-aware intelligence (`--task`, `--impact`, `--plan`)

The competitors are all *retrieval* tools — they help an agent **find** things.
codemap is a *reasoning* tool: it connects structure to the **actual task** the
agent is working on. No competitor does this.

```bash
# Rank modules relevant to a task (token overlap + graph centrality)
codemap --task "fix the login bug" .
# 1. src/auth/login.py  (score 8, 3 keyword hits, 12 dependents)
# 2. src/auth/session.py (score 6, 2 keyword hits, 8 dependents)

# Predict the blast radius of changing a module
codemap --impact core/engine.py .
# risk: high
# ## Direct dependents (34) — most likely to break
#   src.main, tests.test_engine, ...

# Agent-native prioritized reading plan
codemap --plan "add retry to engine" .
# Read these files, in this order, to understand the task:
# 1. src/core/engine.py  (why: 2 keyword matches, 2 modules depend on it)
# 2. src/utils/retry.py  (why: 1 keyword match, 3 modules depend on it)
```

`--impact` answers *"what breaks if I change this?"* before the agent edits.
`--task` and `--plan` turn codemap from a passive map into a **task-orientation
engine** — the agent gets a prioritized reading list instead of a 468-file tree.

## Structural intelligence (`--graph`, `--focus`, `--calls`)

The map is the beginning. codemap also builds a **real import dependency graph**
and a **function-level call graph** with Python's built-in `ast` module — no
daemon, no index, no deps. Tell your agent "what touches what" in under a second:

```bash
# Full import graph (385 modules, 1126 edges in <1s on browser-use)
codemap --graph .

# Focus on ONE module: what it needs, and what needs it
codemap --graph --focus browser_use/agent .
# focus: browser_use.agent
#   depends_on: browser_use.actor.*, browser_use.browser.*, ... (123 modules)
#   depended_on_by: tests.ci.test_beta_agent

# Function-level call graph (only calls to repo-defined functions)
codemap --calls --focus browser_use/agent/service .
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
codemap --diff .
# codemap --diff — 2 changed file(s)
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
codemap --install-agents .
# created /path/to/AGENTS.md
```

Writes (or updates) an `AGENTS.md` with a codemap instruction block, so every
future agent session auto-loads the map before touching anything. Set and forget.

## Token-cost reporting (`--cost`)

```bash
codemap --cost .
# ## Cost
#   ~308 tokens (~1234 bytes) — vs ~40k+ tokens for grep+read on a large repo
```

Quantify the win. Numbers beat claims.

## Incremental mode (`--incremental`)

Repeated runs on large repos shouldn't re-parse everything. `--incremental`
uses a hash-based cache (`.codemap-cache.json`) — no daemon, no index — and
reports only the files that changed since the last run:

```bash
codemap --incremental .
# codemap --incremental — 1 changed file(s) since last run
# ## Changed files
#   src/utils/retry.py
```

## Security check (`--verify`)

Running a single-file script downloaded from the web carries risk. `--verify`
prints the SHA-256 of a file so you can confirm a downloaded copy is the
official codemap and not tampered with:

```bash
codemap --verify codemap.py
# sha256: bd4b63de974d2b1411d11847ff6d541a60f9252482da030e5bdcfd4f1c1262ef
```

## Why no daemon (the MCP server is better)

A daemon keeps the index in RAM for fast repeated queries — but it goes stale,
needs a separate process to manage, and uses resources even when idle. codemap
doesn't need one:

- **The MCP server is the daemon.** When an agent registers `codemap-mcp.py`,
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

For large repos, `codemap --index` builds a **persistent on-disk byte-offset
index** once, then `--get-symbol`/`--search` load it in milliseconds instead of
re-parsing everything:

```bash
codemap --index .            # build + save the index (469 files, 4538 symbols in ~1s)
codemap --get-symbol Agent . # loads from the index in ~0.4s
codemap --index-status .     # is the index fresh?
```

The index survives across invocations (no daemon, no background process) and is
incrementally refreshed via content hashes. This is the scale win for large
repos — the persistent-index benefit without the operational surface.

## MCP server (agents call codemap natively)

`codemap-mcp.py` is a **zero-dependency MCP server** (stdlib JSON-RPC over
stdio — no `mcp` package, no daemon). Register it with any MCP-capable agent:

```json
// Claude Code / Cursor / Codex MCP config
// macOS/Linux: "command": "python3"   Windows: "command": "python"
{
  "codemap": {
    "command": "python3",
    "args": ["/path/to/codemap-mcp.py"]
  }
}
```

Exposes twenty-one tools: `codemap_map`, `codemap_graph`, `codemap_focus`,
`codemap_calls`, `codemap_diff`, `codemap_impact`, `codemap_task`,
`codemap_plan`, `codemap_cross`, `codemap_search`, `codemap_usages`,
`codemap_grep`, `codemap_read`, `codemap_explain`, `codemap_similar`,
`codemap_deadcode`, `codemap_get_symbol`, `codemap_snippet`,
`codemap_incremental`, `codemap_verify`, `codemap_trace`. Your agent can build
a mental model, trace execution flow across files, see what changed, predict
blast radius, get a task-oriented reading plan, search any symbol, find where
it's used, grep for snippets, read exact symbol source, explain a symbol, find
refactoring candidates, detect dead code, retrieve token-counted snippets,
extract byte ranges, verify a download, and capture runtime call edges —
natively, no install, no index.

## How it works

codemap is a single Python file using only the standard library:

- **Tree + outlines** — walks the repo, respects `.gitignore` (full pattern
  support: negation, anchoring, `**`, dir-only), guards against symlink loops,
  and reads the top-level declarations of each file across 18 languages.
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
- **Incremental cache** — a hash-based `.codemap-cache.json` (no daemon) so
  repeated runs only re-parse changed files.
- **MCP server** — a minimal JSON-RPC 2.0 stdio transport, so any MCP client
  can call codemap without installing the `mcp` package.

No indexing daemon, no background process, no network. It reads your files,
computes the structure, prints it, and exits.

## Known limits (honest) — and how each is removed

codemap trades precision for its single-file/zero-dep design. But every limit
now has an **optional progressive-enhancement backend** that removes it when the
richer tool is present — the zero-dep core always works, and the precision
ceiling rises when you opt in:

- **Regex multi-language analysis is best-effort.** *Removed by:* `codemap
  --install-grammars` (or `pip install tree-sitter tree-sitter-python ...`).
  codemap then uses real AST parsing for those languages instead of regex.
  Verified: `export function helper` (missed by regex) is caught by tree-sitter.
- **Static analysis misses runtime wiring.** *Removed by:* `codemap --trace CMD
  --force` runs a command (e.g. your test suite) under `sys.settrace` and
  records the ACTUAL call edges — capturing dynamic imports and monkeypatching
  that no static analyzer (including tree-sitter) can see. `--force` is required
  because `--trace` executes code; run it in an isolated sandbox/CI job.
- **Task scoring is a heuristic.** *Removed by:* set `CODEmap_EMBED_BASE_URL` +
  `CODEmap_EMBED_API_KEY` (OpenAI-compatible) or install `sentence-transformers`.
  codemap then uses real semantic embeddings for `--task` relevance instead of
  token overlap.
- **Import resolution is heuristic.** Handles common layouts, namespace packages,
  source-root-relative imports, and multi-root workspaces (via `pyproject.toml` /
  `package.json` / `go.mod`); unusual `sys.path` setups may still mis-resolve.
- **No persistent index.** codemap is always-fresh (reads files live) rather
  than maintaining a background index. `--incremental` mitigates repeated-run
  cost, but it's not a daemon-backed knowledge graph.

## Correctness

- **Nested `.gitignore`** — codemap merges subdirectory `.gitignore` files as it
  walks (gitignore semantics), so a nested ignore is honored.
- **Cache invalidation** — the incremental cache tracks `.gitignore` hashes; if
  a `.gitignore` changes, the cached file list is invalidated and re-walked.
- **Workspace roots** — imports resolve against detected package roots
  (`pyproject.toml` / `package.json` / `go.mod`), handling multi-root monorepos.
- **`--trace` safety** — requires `--force` (it executes code); run in an
  isolated sandbox/CI job.

If you need tree-sitter precision, a persistent knowledge graph, or snippet-level
code search, the heavyweight tools (semble, codebase-memory-mcp) are genuinely
better at those. codemap wins on **speed, zero-setup, freshness, and
task-awareness** — the 80% case for everyday agent use.

## Why it's different

| | codemap | jcodemunch | semble / codebase-memory-mcp |
|---|---|---|---|
| Install | one file, copy it | `pip install` + MCP + deps | `pip install` + deps |
| Setup | none | index once | indexing daemon |
| Runs on | stdlib only | tree-sitter + index | heavy runtime |
| Time to first result | < 1s | after indexing | after indexing |
| Always fresh | yes (reads live) | re-index | re-index |
| Import graph | **yes — `--graph`** | partial | yes |
| Cross-file call graph | **yes — `--cross`** | yes (`get_call_hierarchy`) | yes |
| Symbol index / search | **yes — `--search`** | yes (`search_symbols`) | yes |
| Token-shaving retrieval | **yes — `--get-symbol`** | yes (`get_symbol_source`) | partial |
| Byte-range snippets | **yes — `--snippet`** | yes | partial |
| **Task relevance (`--task`)** | **yes** | **no** | **no** |
| **Reading plan (`--plan`)** | **yes** | **no** | **no** |
| **Explain (`--explain`)** | **yes (no LLM)** | **no** | **no** |
| **Similar (`--similar`)** | **yes** | **no** | **no** |
| **Dead code (`--deadcode`)** | **yes** | **no** | **no** |
| **Runtime trace (`--trace`)** | **yes** | **no** | **no** |
| Git-aware `--diff` | **yes — always fresh** | partial (`get_changed_symbols`) | no |
| MCP server | **yes — zero-dep, 21 tools** | yes | yes |
| Offline | yes | yes | varies |

The heavyweight tools are great at retrieval — but they're *search engines*,
not *task-orientation engines*. codemap does retrieval (token-shaving, byte
offsets) *and* tells the agent what matters, what breaks, and what to read
first — the reasoning the search tools don't do. And it's the fastest possible
structural context, in one file, in under a second, always fresh.

## For humans too

codemap isn't just for agents. Onboarding to a new codebase, auditing a repo
you inherited, or just reminding yourself where things live — `codemap` gives
you the same instant structural overview a senior dev has after a week. It's
the fastest way to answer "what is this project, actually?"

## Roadmap

- [x] Multi-language outline extraction (Python, JS/TS, Go, Rust, Java, Ruby, PHP, C/C++, C#, Swift, Kotlin, shell, Lua, Elixir, Dart, OCaml, Scala, R)
- [x] `.gitignore`-aware traversal
- [x] JSON output for tooling
- [x] Import dependency graph + `--focus` (Python, via `ast`)
- [x] Function-level call graph (`--calls`, multi-language)
- [x] Zero-dependency MCP server (`codemap-mcp.py`)
- [x] Git-aware `--diff` (structure of changed files)
- [x] `--install-agents` (auto-write AGENTS.md)
- [x] Token-cost reporting (`--cost`)
- [x] Task-aware relevance (`--task`)
- [x] Change-impact prediction (`--impact`)
- [x] Agent-native reading plan (`--plan`)
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
- [ ] Persistent knowledge-graph index (daemon-backed, for monorepos)

## Benchmarks

Honest, reproducible numbers vs the competitors live in
[`benchmarks/`](benchmarks/README.md) — including the caveat that codemap is a
complement, not a replacement, for the heavy tools. Run them yourself:

```bash
python3 benchmarks/run.py --repo /path/to/repo
```

## Trust & verification

- **CI** — the test suite runs on Linux, macOS, and Windows (Python 3.8–3.12)
  via GitHub Actions (`.github/workflows/ci.yml`).
- **Checksums** — every release ships the SHA-256 of `codemap.py`; verify a
  downloaded copy with `codemap --verify codemap.py`.
- **Releases** — tagged, versioned releases with the demo GIF and checksum via
  `scripts/release.sh`.
- **One file, readable** — the whole tool is a single stdlib file you can audit
  before running. No hidden deps, no network calls, no telemetry.

## Contributing

PRs welcome. The whole thing is one file — easy to read, easy to test. Run the
suite with `python3 tests.py`. Ideas that fit the ethos: keep it zero-dependency,
keep it fast, keep it one file.

## Agent skill

A ready-to-load Hermes skill for using and maintaining codemap ships in
[`skills/codemap/SKILL.md`](skills/codemap/SKILL.md) — covers every flag, MCP
wiring, the test suite, re-recording the demo GIF, and how to extend the tool.

## License

MIT — do whatever you want with it.

---

*Built for the people who'd rather their AI agent actually ship code than spend 15 minutes reading a 40k-LoC repo.*
