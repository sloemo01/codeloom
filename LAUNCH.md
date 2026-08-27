# Launch material — codeloom v0.79 "Memory OS"

**Headline — codeloom is now a Memory OS.** Typed memory objects
(`memory.jsonl`), an importance engine, and `--memory <symbol>`: graph-linked
retrieval that answers "what the graph knows about X" instead of an
embedding-search hope. Deterministic extraction, zero LLM, zero deps.

Ready-to-post copy for the launch. The GIF is the hook — link the raw GitHub
URL so it renders inline on both platforms. All numbers below are measured and
reproducible ([`benchmarks/README.md`](benchmarks/README.md)).

- Demo GIF: `https://raw.githubusercontent.com/sloemo01/codeloom/main/demo.gif`
- Repo: `https://github.com/sloemo01/codeloom`
- Latest release: `https://github.com/sloemo01/codeloom/releases/tag/v0.79.1`

---

## Show HN post

**Title (option A — the differentiator):** Show HN: codeloom — measured: your agent's context survives compaction (2 calls to resume vs 33 to re-derive)

**Title (option B — the classic):** Show HN: codeloom — your agent's codebase context survives compaction, in one stdlib file

**Body:**

Every coding agent (Claude Code, Cursor, Codex, Gemini) has the same problem: before it can *do* anything, it has to figure out what your codebase even *is*. So it greps, reads whole files, burns 40k+ tokens building context — then a context compaction wipes all of it, and it re-derives everything from scratch. Over and over.

I kept hitting this, so I built codeloom: one file, zero dependencies, no daemon, 100% local. It gives an agent a real structural model of a repo in under a second — and, unlike the search tools, it tells the agent *what's relevant to the task*, *what breaks if it changes something*, and *exactly which bytes to read*.

![codeloom demo](https://raw.githubusercontent.com/sloemo01/codeloom/main/demo.gif)

**The measured numbers (all reproducible, loss rows published):**

- **Graph-linked memory retrieval (v0.79).** Memory objects are typed, stored in `memory.jsonl`, and linked to the call graph — `--memory <symbol>` returns what the graph knows about that symbol, not an embedding-search hope. Extraction is deterministic (`scripts/memory_extract.py` mines git history), `--memory-stats` and the `memory_eval` benchmark ship with it, and none of it runs an LLM.

- **Compaction recovery: 2 calls / ~985 tokens vs 33 calls / ~21.6k tokens.** After a compaction, `--resume` restores both the structural map and the decision ledger. Nobody else publishes this number — the 30k★ field leader has zero mentions of compaction in its README. ([bench](https://github.com/sloemo01/codeloom/blob/main/benchmarks/README.md))
- **Symbol retrieval: 43–54× fewer tokens than code-review-graph** on the same fastapi symbols, same tokenizer (9–10 vs 428–485). Measured live against their MCP server.
- **Setup-to-first-answer: 0.13s warm** (after `--index`). Theirs: pip install (75 packages) + graph build (42MB) + daemon + an embeddings extra (~2GB) for semantic search. Ours: copy one stdlib file.
- **Sealed retrieval run (no LLM):** 10 calls / 731 tokens vs bare grep-and-read 29 calls / ~5.6–6.7k tokens (bare hit-rate run-variant 1–3/10; codeloom's 4/10 deterministic).
- **Dogfood honesty (2026-08-23, pallets/flask, 83 files):** same-session head-to-head vs grep+read, codeloom used **MORE** total tokens (+14.5%) and wall time (+2.6x) — the 98.8%/43–54× claims apply to big-repo chains-of-calls vs grep+read baselines, not small-repo single-agent sessions; it won on evidence (`--impact` 5 direct + 33 transitive dependents in 0.23s, `--task` ranking, `--checkpoint-restore` diff, `--memory` retrieval). Reproduce: `benchmarks/dogfood_bench.py`.

**The one thing that matters: `--pack` is a code-embedded task brief, not a ranked list.**

```bash
codeloom --pack "fix the login bug" .
```

returns a self-contained, ~1.5k-token brief with the **actual `login()` source embedded** (byte-precise, capped ~40 lines), the call path, the impact list, and what's safe to touch. An agent pastes it once and works — **zero retrieval calls on the core path**. That's the difference between "where does 'login' appear" (the search tools) and "what code actually runs when a login happens" (codeloom).

What it does:
- **Code-embedded task brief** — `--pack` embeds the real code, not names. Measured on fastapi: `fix the login bug` → ~1,541 tokens, 10 code blocks embedded, only oversized symbols point to `--full`.
- **Edit-relevance ranking** — `--task`/`--plan`/`--pack` rank by anchor → call-path walk, not keyword overlap. `session.py` ranks above `constants.py` for "fix the login bug" because it's on the login call path.
- **99%+ token savings** — `--get-symbol` is summary-first by default (signature + docstring + call graph, not full source). Measured on fastapi: `Agent` 3,997→30 tokens (99.2%), `click` 3,997→30 (99.2%) — and **98.8% overall** across the 15 task-runs on express/fastapi/gin (same repos and tokenizer as jcodemunch's own benchmark). `--full` opt-in for the implementation.
- **Zero-install, zero-telemetry, offline** — one stdlib file, no `pip install`, no model downloads, no license validation, no telemetry that phones home. The heavyweight tools can't say that.
- **Git-diffable** — `codeloom --write MAP.md` produces a reviewable text artifact you commit and diff in PRs. jcodemunch's index is a binary blob.
- **CI action** — `codeloom --install-agents .` writes AGENTS.md + a GitHub Action that runs `--pack` on every PR and posts the brief as a comment. One line to add.
- **MCP server** — zero-dep, **82 tools**, resident in-memory knowledge graph + `--watch` incremental refresh (daemon-speed, no daemon).
- **Memory OS (v0.79)** — typed memory objects in `memory.jsonl`, an importance engine, and `--memory <symbol>` retrieval linked straight into the call graph. Extraction is deterministic (`scripts/memory_extract.py` mines git history), so memory is reproducible — zero-LLM, zero-dep, and it never phones home.
- **AST depth, repo-aware** — `--install-grammars --yes` for core languages, or `--auto-grammars` scans the repo and installs grammars for the languages it actually uses (no per-language setup). 130+ extensions via regex/C extraction.
- **Optional C engine, Linux-kernel scale** — build `codeloom_core.c` once, `--index --engine c` indexes the **full Linux kernel graph (C engine: 64,814 files, 3.2M symbols, 408k edges) in ~89–113s**. Pure-Python stays zero-dep; the C accelerator is opt-in for huge monorepos. (The Rust walk+map — 67,306 files / 5.66M symbols — is ~11–13s.)

The whole thing is Python stdlib only. No `pip install`, no indexing daemon, no GPU. Copy one file into your repo, point your agent at it, done.

And now it remembers. v0.79 ships Memory OS: typed memory objects (`memory.jsonl`) with graph-linked retrieval — `--memory <symbol>` returns what the graph knows about that symbol, with deterministic extraction from git history (`scripts/memory_extract.py`). Zero-LLM, zero-dep: the memory is reproducible, not vibes.

**Why not just use the existing tools?** They're great at retrieval — but they're search engines, not task-orientation engines. They answer "where is this symbol?" codeloom answers "what code actually runs for this task?" — and embeds it, with the blast radius and a files-to-touch checklist (`--ask`). Plus it's the fastest possible structural context, in one file, in under a second, always fresh (no stale index) — and it indexes the Linux kernel full graph (C engine) in ~89–113s (vs their ~3 min).

Repo: https://github.com/sloemo01/codeloom

Would love feedback — especially on the edit-relevance ranking and the compaction-recovery benchmark.

---

## X thread

**1/6** Every AI coding agent wastes tokens figuring out what your codebase even *is* before it can do anything. grep, read whole files, 40k tokens of context — then it forgets.

So I built codeloom: a map of your repo for agents. One file, zero deps, no daemon, 100% local. Under a second.

**2/7** The measured differentiator: after a context compaction, a bare agent re-derives with **33 calls / ~21.6k tokens**. `codeloom --resume` restores the map *and* the decision ledger in **2 calls / ~985 tokens**. The 30k★ field leader has zero mentions of compaction anywhere.

**3/7** Your agent forgets nothing the graph can link. v0.79 is a Memory OS: typed memory objects in `memory.jsonl` linked to the call graph — `--memory <symbol>` returns what the graph knows about X, not an embedding-search hope. Deterministic extraction from git history, zero LLM.

**4/7** The pitch in one line: the search tools answer "where is this symbol?" codeloom answers "what code actually runs for this task?" — and embeds it.

`codeloom --pack "fix the login bug"` returns a ~1.5k-token brief with the actual `login()` source embedded, the call path, the impact list, and what's safe to touch. An agent pastes it once and works — zero retrieval on the core path.

**5/7** Measured head-to-head vs code-review-graph (same repo, same symbols, same tokenizer): 9–10 tokens vs 428–485. Setup: one stdlib file vs 75 pip packages + a daemon. Semantic search: zero-dep offline vs a ~2GB embeddings extra.

**6/7** Zero-install, zero-telemetry, offline — one stdlib file, no pip, no model downloads, no license checks, no telemetry that phones home. The heavyweight tools can't say that.

**7/7** Repo: https://github.com/sloemo01/codeloom — MIT, one file, CI-verified on Linux/macOS/Windows, v0.79.0 "Memory OS" released. `--resume` restores your agent's structural context after compaction; `--pack`/`--answer`/`--impact` give the code, the call path, and the blast radius. Measured compaction recovery 2 calls vs 33, retrieval 10 calls vs 29. Go use it.

---

## X thread

**1/6** Every AI coding agent wastes tokens figuring out what your codebase even *is* before it can do anything. grep, read whole files, 40k tokens of context — then it forgets.

So I built codeloom: a map of your repo for agents. One file, zero deps, no daemon, 100% local. Under a second.

**2/7** The pitch in one line: the search tools answer "where is this symbol?" codeloom answers "what code actually runs for this task?" — and embeds it.

`codeloom --pack "fix the login bug"` returns a ~1.5k-token brief with the actual `login()` source embedded, the call path, the impact list, and what's safe to touch. An agent pastes it once and works — zero retrieval on the core path.

**3/7** Your agent forgets nothing the graph can link. v0.79 is a Memory OS: typed memory objects in `memory.jsonl` linked to the call graph — `--memory <symbol>` returns what the graph knows about X, not an embedding-search hope. Deterministic extraction from git history, zero LLM.

**4/7** The ranking is edit-relevance, not keyword overlap:
• anchor the task ("login") → walk the call graph both directions
• `session.py` ranks above `constants.py` for "fix the login bug" because it's on the login call path
• that's "what code runs" vs "where does the word appear"

**5/7** Zero-install, zero-telemetry, offline — one stdlib file, no pip, no model downloads, no license checks, no telemetry that phones home. The heavyweight tools can't say that.

**6/7** Git-diffable + CI action:
• `codeloom --write MAP.md` → a reviewable text artifact you commit and diff in PRs (their index is a binary blob)
• `codeloom --install-agents .` → writes AGENTS.md + a GitHub Action that posts the `--pack` brief on every PR. One line.

**7/7** Repo: https://github.com/sloemo01/codeloom — MIT, one file, CI-verified on Linux/macOS/Windows, v0.79.0 "Memory OS" released. `--resume` restores your agent's structural context after a compaction; `--working-state` + `--cognitive-load` restore its decisions, lessons, open items and hot set. Code-embedded task brief (`--ask`/`--pack`), 99% token savings, lazy per-symbol index + `--watch`/`--watch-core` (near-resident or native, no daemon), 82 MCP tools, integrated C engine + `--serve` — indexes the full Linux kernel graph (C engine) in **~89–113s**. Go use it.

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

returns a self-contained ~1.5k-token brief with the **actual `login()` source embedded** (byte-precise, capped ~40 lines), the call path, the impact list, and what's safe to touch. An agent pastes it once and works — zero retrieval calls on the core path.

The ranking is **edit-relevance**, not keyword overlap: it anchors the task ("login"), walks the call graph both directions, and ranks the execution path. `session.py` ranks above `constants.py` for "fix the login bug" because it's on the login call path — not because it has more word matches.

**Memory, not a markdown journal.** Most "memory" solutions are markdown journals — the agent writes notes into a file and hopes a later search finds them. codeloom's memory (v0.79) is typed, structured (`memory.jsonl`), and *linked into the call graph*: `--memory <symbol>` returns what the graph knows about that symbol, so retrieval follows the same structure the code does — deterministic extraction from git history, no LLM in the loop, nothing to hope for. It's the difference between a pile of notes and a memory with a graph attached.

Also:
- **Zero-install, zero-telemetry, offline** — one file, no pip, no model downloads, no license checks, no telemetry that phones home
- **Git-diffable** — `codeloom --write MAP.md` is a reviewable text artifact you commit and diff in PRs
- **CI action** — `codeloom --install-agents .` writes AGENTS.md + a GitHub Action that posts the `--pack` brief on every PR
- **MCP server** — zero-dep, 82 tools, resident in-memory knowledge graph + `--watch` incremental refresh (daemon-speed, no daemon)
- **Memory OS (v0.79)** — typed memory in `memory.jsonl`, importance engine, `--memory <symbol>` graph-linked retrieval, `--memory-stats` + `memory_eval` benchmark; deterministic extraction from git history, zero-LLM, zero-dep
- **AST depth, repo-aware** — `--auto-grammars` scans the repo and installs grammars for its languages; 130+ extensions via regex/C extraction
- **Compaction recovery, measured** — after a compaction: 2 calls / ~985 tokens to restore (`--resume`) vs 33 calls / ~21.6k tokens to re-derive (bare). All numbers reproducible: github.com/sloemo01/codeloom/blob/main/benchmarks/README.md

Repo: https://github.com/sloemo01/codeloom

Would love feedback — especially on the edit-relevance ranking and the compaction-recovery benchmark.
