# Launch material

Ready-to-post copy for codeloom. The GIF is the hook — link the raw GitHub URL
so it renders inline on both platforms.

- Demo GIF: `https://raw.githubusercontent.com/sloemo01/codeloom/main/demo.gif`
- Repo: `https://github.com/sloemo01/codeloom`
- Latest release: `https://github.com/sloemo01/codeloom/releases/tag/v0.52.0`

---

## Show HN post

**Title:** Show HN: codeloom — your agent's codebase context survives compaction, in one stdlib file

**Body:**

Every coding agent (Claude Code, Cursor, Codex, Gemini) has the same problem: before it can do anything, it has to figure out what your codebase even *is*. So it greps, reads whole files, burns 40k+ tokens building context — then forgets what it learned.

I kept hitting this, so I built codeloom: one file, zero dependencies, no daemon, 100% local. It gives an agent a real structural model of a repo in under a second — and, unlike the search tools, it tells the agent *what's relevant to the task*, *what breaks if it changes something*, and *exactly which bytes to read*.

![codeloom demo](https://raw.githubusercontent.com/sloemo01/codeloom/main/demo.gif)

**The one thing that matters: `--pack` is a code-embedded task brief, not a ranked list.**

```bash
codeloom --pack "fix the login bug" .
```

returns a self-contained, ~1.6k-token brief with the **actual `login()` source embedded** (byte-precise, capped ~40 lines), the call path, the impact list, and what's safe to touch. An agent pastes it once and works — **zero retrieval calls on the core path**. That's the difference between "where does 'login' appear" (the search tools) and "what code actually runs when a login happens" (codeloom).

What it does:
- **Code-embedded task brief** — `--pack` embeds the real code, not names. Measured on fastapi: `fix the login bug` → ~1,655 tokens, 10 code blocks embedded, only oversized symbols point to `--full`.
- **Edit-relevance ranking** — `--task`/`--plan`/`--pack` rank by anchor → call-path walk, not keyword overlap. `session.py` ranks above `constants.py` for "fix the login bug" because it's on the login call path.
- **99%+ token savings** — `--get-symbol` is summary-first by default (signature + docstring + call graph, not full source). Measured on browser-use: `Agent` (huge class) 3,689→10 tokens (99.7%), `click` 6,954→16 (99.8%). `--full` opt-in for the implementation.
- **Zero-install, zero-telemetry, offline** — one stdlib file, no `pip install`, no model downloads, no license validation, no telemetry that phones home. The heavyweight tools can't say that.
- **Git-diffable** — `codeloom --write MAP.md` produces a reviewable text artifact you commit and diff in PRs. jcodemunch's index is a binary blob.
- **CI action** — `codeloom --install-agents .` writes AGENTS.md + a GitHub Action that runs `--pack` on every PR and posts the brief as a comment. One line to add.
- **MCP server** — zero-dep, **54 tools**, resident in-memory knowledge graph + `--watch` incremental refresh (daemon-speed, no daemon).
- **AST depth, repo-aware** — `--install-grammars --yes` for core languages, or `--auto-grammars` scans the repo and installs grammars for the languages it actually uses (no per-language setup). 130+ extensions via regex/C extraction.
- **Optional C engine, Linux-kernel scale** — build `codeloom_core.c` once, `--index --engine c` indexes the **full Linux kernel (64k files, 3.2M symbols, 408k edges) in ~91s**. Pure-Python stays zero-dep; the C accelerator is opt-in for huge monorepos.

The whole thing is Python stdlib only. No `pip install`, no indexing daemon, no GPU. Copy one file into your repo, point your agent at it, done.

**Why not just use the existing tools (codebase-memory-mcp, codegraph, jcodemunch)?** They're great at retrieval — but they're search engines, not task-orientation engines. They answer "where is this symbol?" codeloom answers "what code actually runs for this task?" — and embeds it, with the blast radius and a files-to-touch checklist (`--ask`). Plus it's the fastest possible structural context, in one file, in under a second, always fresh (no stale index) — and it indexes the Linux kernel in ~91s (vs their ~3 min).

Drop the output into your `AGENTS.md` and every future session auto-loads the map before touching anything.

Repo: https://github.com/sloemo01/codeloom

Would love feedback — especially on the edit-relevance ranking and the code-embedded brief.

---

## X thread

**1/6** Every AI coding agent wastes tokens figuring out what your codebase even *is* before it can do anything. grep, read whole files, 40k tokens of context — then it forgets.

So I built codeloom: a map of your repo for agents. One file, zero deps, no daemon, 100% local. Under a second.

**2/6** The pitch in one line: the search tools answer "where is this symbol?" codeloom answers "what code actually runs for this task?" — and embeds it.

`codeloom --pack "fix the login bug"` returns a ~1.6k-token brief with the actual `login()` source embedded, the call path, the impact list, and what's safe to touch. An agent pastes it once and works — zero retrieval on the core path.

**3/6** The ranking is edit-relevance, not keyword overlap:
• anchor the task ("login") → walk the call graph both directions
• `session.py` ranks above `constants.py` for "fix the login bug" because it's on the login call path
• that's "what code runs" vs "where does the word appear"

**4/6** Zero-install, zero-telemetry, offline — one stdlib file, no pip, no model downloads, no license checks, no telemetry that phones home. The heavyweight tools can't say that.

**5/6** Git-diffable + CI action:
• `codeloom --write MAP.md` → a reviewable text artifact you commit and diff in PRs (their index is a binary blob)
• `codeloom --install-agents .` → writes AGENTS.md + a GitHub Action that posts the `--pack` brief on every PR. One line.

**6/6** Repo: https://github.com/sloemo01/codeloom — MIT, one file, CI-verified on Linux/macOS/Windows, v0.51.0 released. `--resume` restores your agent's structural context after a compaction. Code-embedded task brief (`--ask`/`--pack`), edit-relevance, 99% token savings, lazy per-symbol index + `--watch`/`--watch-core` (near-resident or native, no daemon), 54 MCP tools, optional C engine + `--serve` — indexes the full Linux kernel (3.2M symbols, 408k edges) in **~91s**. Go use it.

---

## Reddit post (r/ClaudeAI, r/LocalLLaMA, r/ExperiencedDevs)

**Title:** I built a tool that gives AI coding agents a code-embedded task brief — not a ranked list of files

**Body:**

Every coding agent (Claude Code, Cursor, Codex) has the same problem: before it can do anything, it has to figure out what your codebase even *is*. So it greps, reads whole files, burns 40k+ tokens building context — then forgets what it learned.

I built codeloom to fix that. One stdlib file, zero deps, no daemon, 100% local.

**The thing that matters: `--pack` is a code-embedded task brief, not a ranked list.**

```bash
codeloom --pack "fix the login bug" .
```

returns a self-contained ~1.6k-token brief with the **actual `login()` source embedded** (byte-precise, capped ~40 lines), the call path, the impact list, and what's safe to touch. An agent pastes it once and works — zero retrieval calls on the core path.

The ranking is **edit-relevance**, not keyword overlap: it anchors the task ("login"), walks the call graph both directions, and ranks the execution path. `session.py` ranks above `constants.py` for "fix the login bug" because it's on the login call path — not because it has more word matches.

Also:
- **Zero-install, zero-telemetry, offline** — one file, no pip, no model downloads, no license checks, no telemetry that phones home
- **Git-diffable** — `codeloom --write MAP.md` is a reviewable text artifact you commit and diff in PRs
- **CI action** — `codeloom --install-agents .` writes AGENTS.md + a GitHub Action that posts the `--pack` brief on every PR
- **MCP server** — zero-dep, 54 tools, resident in-memory knowledge graph + `--watch` incremental refresh (daemon-speed, no daemon)
- **AST depth, repo-aware** — `--auto-grammars` scans the repo and installs grammars for its languages; 130+ extensions via regex/C extraction

Repo: https://github.com/sloemo01/codeloom

Would love feedback — especially on the edit-relevance ranking and the code-embedded brief.
