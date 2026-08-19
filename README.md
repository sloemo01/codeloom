<h1 align="center">codemap</h1>
<p align="center">
  <b>Give your AI coding agent a map of the repo in one second.</b><br/>
  Single-file · zero-dependency · no daemon · 100% local
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

```bash
# In any repo:
$ codemap
# codemap — /you/project
# 468 files
#
# ## Entry points
#   README.md
#   src/cli.py
#   pyproject.toml
#
# ## Structure
# ├── src/
# │   ├── cli.py
# │   │   def main():
# │   │   class Parser:
# │   ├── core/
# │   │   ├── engine.py
# │   │   │   class Engine:
# │   │   │   async def run(
# │   │   └── models.py
# │   │       class User:
# │   │       class Config:
# └── tests/
#     └── test_engine.py
```

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
codemap --no-outline         # skip per-file one-liners (faster)
codemap --max-files 2000     # cap traversal (default 5000)
```

## Why it's different

| | codemap | heavyweight tools (semble, codebase-memory-mcp, etc.) |
|---|---|---|
| Install | one file, copy it | `pip install` + deps |
| Setup | none | indexing daemon, MCP server, build step |
| Runs on | stdlib only | heavy runtime |
| Offline | yes | varies |
| Speed | < 1s | indexing can take minutes |
| Cost | 0 tokens | still uses tokens to *query* it |

The heavyweight tools are great — but they're *overkill* when all you need is
a map. codemap is the 80/20: the fastest possible structural context.

## Roadmap

- [x] Multi-language outline extraction (Python, JS/TS, Go, Rust, Java, Ruby, PHP, C/C++, C#, Swift, Kotlin, shell, Lua, Elixir, Dart, OCaml, Scala, R)
- [x] `.gitignore`-aware traversal
- [x] JSON output for tooling
- [ ] MCP server wrapper (so agents can call `codemap` natively)
- [ ] Incremental mode (only re-emit changed modules)
- [ ] `--deps` flag to surface import/call relationships

## License

MIT — do whatever you want with it.

---

*Built for the people who'd rather their AI agent actually ship code than spend 15 minutes reading a 40k-LoC repo.*
