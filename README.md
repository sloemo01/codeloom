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
codemap --incremental        # show files changed since last run (hash-based cache)
codemap --verify FILE        # print SHA-256 of a file (security check)
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

Exposes thirteen tools: `codemap_map`, `codemap_graph`, `codemap_focus`,
`codemap_calls`, `codemap_diff`, `codemap_impact`, `codemap_task`,
`codemap_plan`, `codemap_cross`, `codemap_search`, `codemap_usages`,
`codemap_incremental`, `codemap_verify`. Your agent can build a mental model,
trace execution flow across files, see what changed, predict blast radius, get
a task-oriented reading plan, search any symbol, find where it's used, and
verify a download — natively, no install, no index.

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

## Known limits (honest)

codemap trades precision for its single-file/zero-dep design. These are
deliberate, documented tradeoffs — not bugs:

- **Regex multi-language analysis is best-effort.** The non-Python call graph
  and import detection use regex, not tree-sitter. It will miss some language
  idioms (dynamic dispatch, higher-order calls) and can mis-attribute calls.
  Python analysis uses precise `ast` and is much more accurate.
- **Static analysis misses runtime wiring.** Dynamic imports, monkeypatching,
  and import-time tricks aren't visible to any static analyzer — including
  tree-sitter tools. codemap is for *structure*, not guaranteed correctness.
- **Import resolution is heuristic.** It handles common layouts, namespace
  packages, and source-root-relative imports, but unusual `sys.path` setups
  may mis-resolve. The suffix-match fallback covers most cases.
- **Task scoring is a heuristic.** Token overlap + graph centrality is fast and
  effective, but won't match semantic-embedding relevance for nuanced tasks.
- **No persistent index.** codemap is always-fresh (reads files live) rather
  than maintaining a background index. `--incremental` mitigates repeated-run
  cost, but it's not a daemon-backed knowledge graph.

If you need tree-sitter precision, a persistent knowledge graph, or snippet-level
code search, the heavyweight tools (semble, codebase-memory-mcp) are genuinely
better at those. codemap wins on **speed, zero-setup, freshness, and
task-awareness** — the 80% case for everyday agent use.

## Why it's different

| | codemap | heavyweight tools (semble, codebase-memory-mcp, etc.) |
|---|---|---|
| Install | one file, copy it | `pip install` + deps |
| Setup | none | indexing daemon, MCP server, build step |
| Runs on | stdlib only | heavy runtime |
| Import graph | **yes — `--graph`, <1s** | yes, but after indexing |
| Function call graph | **yes — `--calls`, multi-lang** | partial |
| **Cross-file call graph** | **yes — `--cross`, AST-resolved** | yes (tree-sitter) |
| **Symbol index / search** | **yes — `--search`** | yes |
| Git-aware `--diff` | **yes — always fresh** | no |
| **Task relevance (`--task`)** | **yes** | **no** |
| **Change impact (`--impact`)** | **yes** | **no** |
| **Reading plan (`--plan`)** | **yes** | **no** |
| `--install-agents` | **yes — one command** | manual setup |
| MCP server | **yes — zero-dep `codemap-mcp.py`** | yes |
| Offline | yes | varies |
| Speed | < 1s | indexing can take minutes |
| Cost | 0 tokens to query | still uses tokens to *query* it |

The heavyweight tools are great — but they're *overkill* when all you need is
a map. codemap is the 80/20: the fastest possible structural context.

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
- [x] Incremental mode (`--incremental`, hash-based cache)
- [x] Multi-language import graph (JS/TS, Go, Rust, Java, C/C++, C#, Ruby, PHP, Swift, Kotlin, Dart, Lua)
- [x] Full `.gitignore` support (negation, anchoring, `**`, dir-only)
- [x] Symlink loop protection + Windows-safe path handling
- [x] Security check (`--verify`, SHA-256)
- [ ] Persistent knowledge-graph index (daemon-backed, for monorepos)

## Benchmarks

Honest, reproducible numbers vs the competitors live in
[`benchmarks/`](benchmarks/README.md) — including the caveat that codemap is a
complement, not a replacement, for the heavy tools.

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
