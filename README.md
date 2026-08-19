<h1 align="center">codemap</h1>
<p align="center">
  <b>Give your AI coding agent a map of the repo in one second.</b><br/>
  Single-file · zero-dependency · no daemon · 100% local · <b>import graph + call graph + MCP</b>
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

```bash
# 1. Grab the one file (no pip, no deps)
curl -O https://raw.githubusercontent.com/sloemo01/codemap/main/codemap.py
chmod +x codemap.py

# 2. Map any repo
./codemap /path/to/repo

# 3. Tell your agent to read it first
echo "Before editing anything, run: codemap" >> AGENTS.md
```

That's it. Under a second, zero setup, works offline.

## Demo

![codemap demo](demo.gif)

## What it gives your agent

| Command | What it answers |
|---|---|
| `codemap` | "What's in this repo, and where?" — tree + one-liners + entry points |
| `codemap --graph` | "What touches what?" — full import dependency graph |
| `codemap --graph --focus X` | "What does X need, and what breaks if I change it?" |
| `codemap --calls` | "What calls what?" — function-level execution flow |
| `codemap --calls --focus X` | "What does this one module's code actually do?" |

## Usage

```bash
codemap                      # map current dir -> stdout
codemap /path/to/repo        # map a specific repo
codemap --write MAP.md       # also write to MAP.md
codemap --json               # machine-readable JSON for tooling
codemap --graph              # Python import dependency graph
codemap --graph --focus X    # deps + dependents of module X
codemap --calls              # function-level call graph (Python)
codemap --calls --focus X    # calls inside one module
codemap --no-outline         # skip per-file one-liners (faster)
codemap --max-files 2000     # cap traversal (default 5000)
```

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

## MCP server (agents call codemap natively)

`codemap-mcp.py` is a **zero-dependency MCP server** (stdlib JSON-RPC over
stdio — no `mcp` package, no daemon). Register it with any MCP-capable agent:

```json
// Claude Code / Cursor / Codex MCP config
{
  "codemap": {
    "command": "python3",
    "args": ["/path/to/codemap-mcp.py"]
  }
}
```

Exposes four tools: `codemap_map`, `codemap_graph`, `codemap_focus`,
`codemap_calls`. Your agent can now build a mental model of any repo and trace
execution flow natively — no install, no index.

## How it works

codemap is a single Python file using only the standard library:

- **Tree + outlines** — walks the repo, respects `.gitignore`, and reads the
  top-level declarations of each file (classes, functions) across 18 languages.
- **Import graph** — parses Python with the built-in `ast` module to resolve
  absolute, relative, and source-root-relative imports into a dependency graph.
- **Call graph** — traces which functions call which, filtering to only
  repo-defined functions so you see real execution flow, not builtin noise.
- **MCP server** — a minimal JSON-RPC 2.0 stdio transport, so any MCP client
  can call codemap without installing the `mcp` package.

No indexing daemon, no background process, no network. It reads your files,
computes the structure, prints it, and exits.

## Why it's different

| | codemap | heavyweight tools (semble, codebase-memory-mcp, etc.) |
|---|---|---|
| Install | one file, copy it | `pip install` + deps |
| Setup | none | indexing daemon, MCP server, build step |
| Runs on | stdlib only | heavy runtime |
| Import graph | **yes — `--graph`, <1s** | yes, but after indexing |
| Function call graph | **yes — `--calls`** | partial |
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
- [x] Function-level call graph (`--calls`)
- [x] Zero-dependency MCP server (`codemap-mcp.py`)
- [ ] Incremental mode (only re-emit changed modules)
- [ ] Multi-language call graph (beyond Python)

## Contributing

PRs welcome. The whole thing is one file — easy to read, easy to test. Run the
suite with `python3 tests.py`. Ideas that fit the ethos: keep it zero-dependency,
keep it fast, keep it one file.

## License

MIT — do whatever you want with it.

---

*Built for the people who'd rather their AI agent actually ship code than spend 15 minutes reading a 40k-LoC repo.*
