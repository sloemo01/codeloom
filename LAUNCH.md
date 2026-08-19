# Launch material

Ready-to-post copy for codemap. The GIF is the hook — link the raw GitHub URL
so it renders inline on both platforms.

- Demo GIF: `https://raw.githubusercontent.com/sloemo01/codemap/main/demo.gif`
- Repo: `https://github.com/sloemo01/codemap`

---

## Show HN post

**Title:** Show HN: codemap — a map of your codebase for AI agents, in one stdlib file

**Body:**

Every coding agent (Claude Code, Cursor, Codex, Gemini) has the same problem: before it can do anything, it has to figure out what your codebase even *is*. So it greps, reads whole files, burns 40k+ tokens building context — then forgets what it learned.

I kept hitting this, so I built codemap: one file, zero dependencies, no daemon, 100% local. It gives an agent a real structural model of a repo in under a second.

![codemap demo](https://raw.githubusercontent.com/sloemo01/codemap/main/demo.gif)

What it does:
- **Map** — folder tree + per-module one-liners + entry points
- **Import graph** — `codemap --graph` → 385 modules, 1126 edges on browser-use in <1s
- **Focus** — `codemap --graph --focus X` → what a module needs + what breaks if you change it
- **Call graph** — `codemap --calls` → function-level execution flow (builtins filtered out)
- **MCP server** — `codemap-mcp.py` is a zero-dep MCP server, so agents call it natively

The whole thing is Python stdlib only. No `pip install`, no indexing daemon, no GPU. Copy one file into your repo, point your agent at it, done.

Why not just use the existing tools (semble, codebase-memory-mcp, etc.)? They're great — but they all make you install a daemon and wait for an index. codemap is the 80/20: the fastest possible structural context, in one file, in under a second.

Drop the output into your `AGENTS.md` and every future session auto-loads the map before touching anything.

Repo: https://github.com/sloemo01/codemap

Would love feedback — especially on the call-graph resolution and what else agents actually need to stop burning tokens.

---

## X thread

**1/6** Every AI coding agent wastes tokens figuring out what your codebase even *is* before it can do anything. grep, read whole files, 40k tokens of context — then it forgets.

So I built codemap: a map of your repo for agents. One file, zero deps, no daemon, 100% local. Under a second.

**2/6** The pitch in one line: every structural-intelligence tool for agents makes you install a daemon. codemap gives you the map, the import graph, the call graph, and an MCP server — in one stdlib file, in under a second.

**3/6** What it does:
• Map — tree + per-module one-liners + entry points
• `--graph` — import graph (385 modules, 1126 edges on browser-use, <1s)
• `--focus` — what a module needs + what breaks if you change it
• `--calls` — function-level execution flow

**4/6** The MCP server is zero-dependency too — pure stdlib JSON-RPC over stdio. Register it in Claude Code / Cursor / Codex and your agent calls codemap natively. No `pip install`, no index.

**5/6** Drop the output into your `AGENTS.md` and every future session auto-loads the map before touching anything. Set and forget.

**6/6** Repo: https://github.com/sloemo01/codemap — MIT, one file, go use it. Feedback welcome, especially on the call-graph resolution.
