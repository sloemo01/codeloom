<h1 align="center">codeloom</h1>

<p align="center">
  <b>Give your AI coding agent a map of the repo in one second — and memory that survives compaction.</b><br/>
  One file · zero dependencies · no daemon · 100% local · MIT
</p>

<p align="center">
  <a href="https://github.com/sloemo01/codeloom/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.8%2B-blue"/></a>
  <a href="https://github.com/sloemo01/codeloom/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/CI-passing-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom#readme"><img src="https://img.shields.io/badge/deps-zero-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom/stargazers"><img src="https://img.shields.io/github/stars/sloemo01/codeloom"/></a>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#what-it-gives-your-agent">Features</a> ·
  <a href="#mcp-server-75-tools--1-router">MCP</a> ·
  <a href="#pr-review-bot">PR Bot</a> ·
  <a href="#why-its-different">vs competitors</a> ·
  <a href="#documentation">Docs</a>
</p>

---

## The problem: agents don't just burn tokens — they forget

Every AI coding agent has the same problem: before it can *do* anything, it must
figure out what your codebase even *is*. So it greps, reads whole files, and
burns 40,000+ tokens building context. Then a context compaction wipes all of
it — and it re-derives everything from scratch. Over and over.

**codeloom fixes both halves of that.**

1. **The map** — one command produces a compact structural model of your repo
   (folder tree + module one-liners + entry points + import graph + call graph)
   that an agent reads in a second.
2. **The memory** — `--decide`, `--checkpoint`, `--resume` record the agent's
   decision stream so `--resume` restores *both* the structural context *and*
   what the agent already tried, decided, and rejected — after any compaction.

No install. No daemon. No GPU. No telemetry. Runs 100% on your machine.

## Quickstart

```bash
# Option A: copy the one file (no pip, no deps)
curl -O https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py

# Option B: pip
pip install codeloom

# Map any repo (<1s to first result)
python3 codeloom.py /path/to/repo > AGENTS.md

# Tell your agent: "read AGENTS.md first"
```

Wire it into your agent natively (17 supported):

```bash
python3 codeloom.py --install-agent claude-code   # or cursor, codex, gemini-cli,
                                                  # opencode, cline, aider, ...
```

## What it gives your agent

### Task-shaped tools (the moat)

| Command | What the agent gets |
|---|---|
| `--pack "TASK"` | **One-shot brief**: reading order + impact + relevant symbols, pre-computed |
| `--answer "Q"` | Cited answer with calibrated confidence |
| `--context-card S1 S2` | Batch triage card for N symbols in one call |
| `--why QUERY` | Decision lookup stamped `[exact]`/`[fuzzy]`/`[unverified]` |
| `--plan TASK` | Agent-native prioritized reading plan |

### Working memory across compaction (nobody else ships this)

```bash
codeloom --decide "use retry(3) not retry(∞) — unbounded hangs agents"
codeloom --checkpoint --task "fix login bug"     # save working state
codeloom --resume                                 # restore after compaction
```

Also: `--remember`, `--seen`, `--working-state`, `--lessons`, `--supersede`,
`--adr`, `--query-memory`.

### Structural intelligence

| Command | Result |
|---|---|
| `--graph` | Full import graph (385 modules, 1126 edges in <1s) |
| `--cross` | Cross-file call graph, AST-resolved |
| `--search` / `--usages` / `--grep` / `--read` | Symbol index, call sites, snippets, token-efficient source |
| `--get-symbol X` | Summary-first retrieval (~95–99% token savings) |
| `--impact M` / `--refactor` / `--rename` | Blast radius prediction |
| `--similar` / `--deadcode` / `--explain` | Refactoring intelligence, zero LLM |
| `--trace` | Runtime call edges static analysis can't see |
| `--routes` / `--channels` | HTTP routes, pub-sub event channels |
| `--pattern '$F($$$ARGS)'` | **Structural AST search** with metavariable capture |

### Speed & quality

| Command | Result |
|---|---|
| `--health` | Code-health screen: 0–10 per file, **0.2s**, deterministic detectors |
| `--risk HEAD~1..HEAD` | Change-risk score 0–100 + named drivers for any commit range |
| `--embed-search Q` | Semantic search offline — subword-hash, zero deps (ggml opt-in) |
| `--watch` → `--watch-merge` | Live freshness: native watcher pipes into the persistent index |
| `--engine c` | Auto-building C core: ~91s Linux-kernel full graph |
| `--verify FILE` | SHA-256 checksum verification |

**50 tree-sitter languages dispatched · 46 fixture-proven** (golden-file parity
tests gate CI on every grammar) · **100+ extensions via regex fallback**.

## MCP server (78 tools + 1 router)

```json
{"command": "python3", "args": ["-m", "codeloom_mcp"]}
```

Or auto-wire any of 17 agents: `codeloom --install-agent <name>`.

78 tools total, but the agent's effective surface is **one tool**:
`codeloom_ask` takes natural language and routes deterministically —
no tool-selection misfires. Full listing:
[`docs/mcp-listing.md`](docs/mcp-listing.md).

## PR review bot

`.github/workflows/pr-bot.yml` turns every pull request into:

1. **Inline line-pinned comments** at exact diff locations:
   - **P1** security (`eval`/`exec`, hardcoded secrets), **P2** (insecure http,
     `shell=True`), **P3** (orphan new symbols, TODO/FIXME markers)
2. **Sticky summary comment** (updated per push): risk verdict 0–100 with
   drivers, diff digest, touched-file health, adaptive review checklist,
   reviewer's starting context
3. **Risk labels**: `risk:low/medium/high/critical`, rotated automatically
4. **Handoff**: `@codex` runs its LLM pass after ours, scoped to
   semantics/logic/design (our deterministic categories are already covered)

Zero LLM cost for stage 1. Works on any GitHub repo — copy the workflow file.

## Why it's different

Full source-cited matrix: [`docs/COMPETITION.md`](docs/COMPETITION.md).
Summary against the four competitors (all verified from their repos):

| | **codeloom** | jcodemunch | codegraph | codebase-memory-mcp |
|---|---|---|---|---|
| License | **MIT** | dual-use/paid commercial | MIT | MIT |
| Install | **one stdlib file** | pip + deps | bundled binary | single binary |
| MCP surface | **78 + 1 NL router** | 90+ via 6 routers | 1 listed + 7 unlisted | 15 |
| Languages | **50 / 46 fixture-proven** | 70+ claimed | 20 byte-verified | 158 vendored |
| Compaction memory | ✅ decision ledger | ❌ | ✅ session state | ❌ |
| Pattern search | ✅ `$VAR`/`$$$REST` capture | ✅ presets/DSL | ❌ | ❌ |
| Offline semantics | ✅ zero-dep | opt-in | ❌ | ✅ bundled weights |

Where competitors are ahead, stated plainly: jcodemunch has broader safety
preflight (edit/delete-safe, SCIP compiler verification); codegraph has 67k★
community scale; codebase-memory ships 158 grammars and an arXiv-published
eval; repowise (AGPL) has defect-validated risk scoring. We claim speed +
shape + proof-per-grammar + memory depth — not their moats.

## Known limits (honest)

- Python gets the deepest analysis (stdlib `ast`); other languages get
  tree-sitter outlines + regex fallbacks.
- Health/risk are structural heuristics — **not** defect-validated against a
  labeled corpus (repowise's moat; we say so rather than overclaim).
- Live-agent token-savings benchmarks are designed-for but unproven — our
  published numbers are static replay with loss rows included
  ([`bench/RESULTS.md`](bench/RESULTS.md)).
- Neural embeddings need an optional ggml/model install; without it you get
  the zero-dep subword hash (still offline, still catches typos).

## Documentation

| Doc | Contents |
|---|---|
| [`CAPABILITIES.md`](CAPABILITIES.md) | Everything codeloom can do |
| [`USER_GUIDE.md`](USER_GUIDE.md) | Practical walkthroughs |
| [`CLI.md`](CLI.md) | Every flag explained |
| [`FEATURES.md`](FEATURES.md) | Strategic feature map |
| [`SECURITY.md`](SECURITY.md) | Trust model & verification |
| [`docs/COMPETITION.md`](docs/COMPETITION.md) | Source-cited competitor matrix |
| [`docs/mcp-listing.md`](docs/mcp-listing.md) | MCP marketplace listing copy |
| [`bench/RESULTS.md`](bench/RESULTS.md) | Replay-bench results (loss rows published) |
| [`BENCHMARKS.md`](BENCHMARKS.md) | Measured performance numbers |
| [`TECHNICAL_REPORT.md`](TECHNICAL_REPORT.md) | Architecture decisions write-up |
| [`AGENT_TRACE.md`](AGENT_TRACE.md) | Agent before/after task trace |

## Trust & verification

- **CI**: Linux/macOS/Windows × Python 3.8–3.12, 75 tests, ≥45 grammar
  fixtures gated by golden files
- **Checksums**: every release publishes the SHA-256 of `codeloom.py`;
  verify with `codeloom --verify codeloom.py`
- **Auditable**: one stdlib file — read the whole thing before running it

## Contributing

PRs welcome. Run tests with `python3 tests.py`. Ethos: zero-dependency, fast,
one file, honest claims.

## Agent skill

A ready-to-load skill for using and maintaining codeloom ships at
[`skills/codeloom/SKILL.md`](skills/codeloom/SKILL.md) — every flag, MCP
wiring, the test suite, re-recording the demo GIF, and how to extend the tool.
Install into your agent's skill directory (e.g.
`~/.hermes/skills/software-development/codeloom/`).

## License

MIT — do whatever you want with it.

---

*Built for people who'd rather their AI agent ship code than spend 15 minutes re-reading a 40k-LOC repo after every compaction.*
