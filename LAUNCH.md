# Launch material

Ready-to-post copy for codemap. The GIF is the hook — link the raw GitHub URL
so it renders inline on both platforms.

- Demo GIF: `https://raw.githubusercontent.com/sloemo01/codemap/main/demo.gif`
- Repo: `https://github.com/sloemo01/codemap`
- Latest release: `https://github.com/sloemo01/codemap/releases/tag/v0.18.0`

---

## Show HN post

**Title:** Show HN: codemap — 99% token savings on code retrieval, in one stdlib file

**Body:**

Every coding agent (Claude Code, Cursor, Codex, Gemini) has the same problem: before it can do anything, it has to figure out what your codebase even *is*. So it greps, reads whole files, burns 40k+ tokens building context — then forgets what it learned.

I kept hitting this, so I built codemap: one file, zero dependencies, no daemon, 100% local. It gives an agent a real structural model of a repo in under a second — and, unlike the search tools, it tells the agent *what's relevant to the task*, *what breaks if it changes something*, and *exactly which bytes to read*.

![codemap demo](https://raw.githubusercontent.com/sloemo01/codemap/main/demo.gif)

What it does:
- **99%+ token savings** — `--get-symbol` is summary-first by default (signature + docstring + call graph, not full source). Measured on browser-use: `Agent` (huge class) 3,689→10 tokens (99.7%), `click` 6,954→16 (99.8%), `extract` 2,558→31 (98.8%). `--full` opt-in for the implementation.
- **Task-aware** — `codemap --task "fix the login bug"` → ranked reading list; `--impact X` → what breaks if you change X; `--plan "task"` → prioritized read order
- **Cross-file call graph** — `codemap --cross` → real execution flow across modules (AST-resolved)
- **Search** — `--search` (symbol index + snippet), `--usages` (call sites), `--grep` (snippet search), `--read` (exact symbol source, multi-language)
- **Understanding** — `--explain` (plain-English, no LLM), `--similar` (refactoring candidates), `--deadcode` (unused symbols)
- **Change-aware** — `--diff` (structure of changed files), `--incremental` (hash-based cache)
- **Runtime truth** — `--trace CMD` (captures dynamic imports/monkeypatching static analysis misses)
- **MCP server** — `codemap-mcp.py` is a zero-dep MCP server with 21 tools, so agents call it natively
- **No daemon** — the MCP server keeps an in-memory index (incremental, always fresh), so you get daemon-speed queries without a background process, staleness, or anything to crash
- **Scale** — `codemap --index` builds a persistent on-disk byte-offset index (469 files, 4538 symbols in ~1s), so large-repo queries load in milliseconds
- **Correctness** — nested `.gitignore` merging, cache invalidation on `.gitignore` change, workspace-root import resolution (pyproject/package.json/go.mod), `--trace` isolation warning

The whole thing is Python stdlib only. No `pip install`, no indexing daemon, no GPU. Copy one file into your repo, point your agent at it, done.

**Why 99% and not "95%"?** Because summary-first retrieval makes *every* retrieval cheap — including huge symbols that full-source tools (jcodemunch included) can't handle. The benchmark is honest and reproducible: `benchmarks/run.py --tokens`.

Why not just use the existing tools (semble, codebase-memory-mcp, jcodemunch)? They're great at retrieval — but they're search engines, not task-orientation engines. codemap does retrieval *and* tells the agent what matters, what breaks, and what to read first. Plus it's the fastest possible structural context, in one file, in under a second, always fresh (no stale index).

Drop the output into your `AGENTS.md` and every future session auto-loads the map before touching anything.

Repo: https://github.com/sloemo01/codemap

Would love feedback — especially on the task-relevance ranking and the summary-first retrieval.

---

## X thread

**1/6** Every AI coding agent wastes tokens figuring out what your codebase even *is* before it can do anything. grep, read whole files, 40k tokens of context — then it forgets.

So I built codemap: a map of your repo for agents. One file, zero deps, no daemon, 100% local. Under a second.

**2/6** The pitch in one line: every structural-intelligence tool for agents makes you install a daemon. codemap gives you the map, the import graph, the cross-file call graph, search, and an MCP server — in one stdlib file, in under a second.

**3/6** 99%+ token savings on retrieval — the part that saves your context window:
• `--get-symbol X` → summary-first (signature + docstring + call graph), not full source
• Measured: `Agent` (huge class) 3,689→10 tokens (99.7%), `click` 6,954→16 (99.8%)
• `--full` opt-in when you need the implementation

**4/6** The part the search tools don't do — task-orientation:
• `--task "fix the login bug"` → ranked reading list
• `--impact X` → what breaks if you change X
• `--plan "task"` → read these files, in this order
• `--cross` → the real call path across files

**5/6** The MCP server is zero-dependency too — pure stdlib JSON-RPC over stdio, 21 tools, with an in-memory index that's always fresh. Register it in Claude Code / Cursor / Codex and your agent calls codemap natively. No `pip install`, no daemon, no stale index.

**6/6** Repo: https://github.com/sloemo01/codemap — MIT, one file, CI-verified on Linux/macOS/Windows, v0.18.0 released. 99% token savings, persistent index for scale, nested .gitignore, cache invalidation, workspace roots, trace safety — all handled. Go use it. Feedback welcome, especially on the task-relevance ranking and summary-first retrieval.
