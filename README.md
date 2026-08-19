<h1 align="center">codemap</h1>
<p align="center">
  <b>Give your AI coding agent a map of the repo in one second.</b><br/>
  Single-file · zero-dependency · no daemon · 100% local · <b>import graph included</b>
</p>

<p align="center">
  <a href="#demo"><img src="https://img.shields.io/badge/see%20it%20run-Demo-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codemap/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.8+-blue"/></a>
</p>

---

Every AI coding agent (Claude Code, Cursor, Codex, Gemini) has the same problem:
before it can *do* anything, it has to figure out what your codebase even *is*.
So it greps, it reads whole files, it burns 40,000+ tokens just to build context —
and then it forgets what it learned.

**codemap fixes that.** One command → a compact "table of contents" of your repo
(folder tree + per-module one-liners + entry points) that your agent reads in a
second to build a mental model *before* touching anything. No install. No daemon.
No GPU. Runs 100% on your machine.

## Demo

![codemap demo](demo.gif)

Drop the output into your `AGENTS.md`, or point your agent at it:

```markdown
<!-- AGENTS.md -->
Before editing anything, run `codemap` and read the output.
```

## Install

**No install required.** It's one file.

```bash
# Drop it in your repo or ~/bin:
curl -O https://raw.githubusercontent.com/sloemo01/codemap/main/codemap.py
chmod +x codemap.py
# or just run it:
python3 codemap.py
```

## Usage

```bash
codemap                      # map current dir -> stdout
codemap /path/to/repo        # map a specific repo
codemap --write MAP.md       # also write to MAP.md
codemap --json               # machine-readable JSON for tooling
codemap --graph              # Python import dependency graph
codemap --graph --focus X    # deps + dependents of module X
codemap --no-outline         # skip per-file one-liners (faster)
codemap --max-files 2000     # cap traversal (default 5000)
```

## Structural intelligence (`--graph` & `--focus`)

The map is the beginning. codemap also builds a **real import dependency graph**
with Python's built-in `ast` module — no daemon, no index, no deps. Tell your
agent "what touches what" in under a second:

```bash
# Full import graph (385 modules, 1126 edges in <1s on browser-use)
codemap --graph .

# Focus on ONE module: what it needs, and what needs it
codemap --graph --focus browser_use/agent .
# focus: browser_use.agent
#   depends_on: browser_use.actor.*, browser_use.browser.*, ... (123 modules)
#   depended_on_by: tests.ci.test_beta_agent
```

`--focus` accepts a file path, a package directory, or a dotted module name
(`browser_use/agent`, `browser_use/agent/service.py`, `agent.service`). It
answers the two questions agents burn the most tokens on: *"what does this
code need?"* and *"what else breaks if I change it?"*

## Why it's different

| | codemap | heavyweight tools (semble, codebase-memory-mcp, etc.) |
|---|---|---|
| Install | one file, copy it | `pip install` + deps |
| Setup | none | indexing daemon, MCP server, build step |
| Runs on | stdlib only | heavy runtime |
| Import graph | **yes — `--graph`, <1s** | yes, but after indexing |
| Offline | yes | varies |
| Speed | < 1s | indexing can take minutes |
| Cost | 0 tokens to query | still uses tokens to *query* it |

The heavyweight tools are great — but they're *overkill* when all you need is
a map. codemap is the 80/20: the fastest possible structural context.

## Roadmap

- [x] Multi-language outline extraction (Python, JS/TS, Go, Rust, Java, Ruby, PHP, C/C++, C#, Swift, Kotlin, shell, Lua, Elixir, Dart, OCaml, Scala, R)
- [x] `.gitignore`-aware traversal
- [x] JSON output for tooling
- [x] Import dependency graph + `--focus` (Python, via `ast`)
- [ ] MCP server wrapper (so agents can call `codemap` natively)
- [ ] Incremental mode (only re-emit changed modules)
- [ ] Function-level call graph (beyond module-level imports)

## License

MIT — do whatever you want with it.

---

*Built for the people who'd rather their AI agent actually ship code than spend 15 minutes reading a 40k-LoC repo.*
