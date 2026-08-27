<div align="center">

# codeloom

**Give your AI coding agent a map of the repo in about a second — and memory that survives compaction.**

`one file` · `zero dependencies` · `no daemon` · `100% local` · `MIT`

[![License](https://img.shields.io/badge/license-MIT-2da44e)](#license)
[![Python](https://img.shields.io/badge/python-3.8%2B-2da44e)](https://www.python.org/downloads/)
[![CI](https://img.shields.io/badge/CI-passing-2da44e)](https://github.com/sloemo01/codeloom/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-128-2da44e)](https://github.com/sloemo01/codeloom/blob/main/tests.py)
[![Deps](https://img.shields.io/badge/deps-zero-2da44e)](https://github.com/sloemo01/codeloom#readme)
[![MCP tools](https://img.shields.io/badge/MCP-82%20tools%20%2B%201%20router-2da44e)](#mcp-server-82-tools--1-router)
[![Stars](https://img.shields.io/github/stars/sloemo01/codeloom)](https://github.com/sloemo01/codeloom/stargazers)

[Quickstart](#quickstart) · [Features](#what-it-gives-your-agent) · [Memory OS](#memory-os--the-graph-remembers-for-you) · [MCP](#mcp-server-82-tools--1-router) · [PR bot](#pr-review-bot) · [Why it's different](#why-its-different) · [Evidence](#evidence-verification)

</div>

<div align="center">

![codeloom demo](demo.gif)

*A real run: one command, one second, a full structural map your agent can act on.*

</div>

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
3. **Memory OS (v0.79)** — typed, importance-scored memory objects written to
   `memory.jsonl` and **linked to the code graph itself**: retrieve what the
   repo learned about a symbol *and* its graph neighbors in one call.

No install. No daemon. No GPU. No telemetry. Runs 100% on your machine.

## What's new in v0.79.4 (speed release)

Big-repo cold-scan speedups, measured on home-assistant/core (18,486 files,
same machine, same repo):

| Command | Before | After | Why |
|---|---|---|---|
| `--graph` | 2m31s | **26s** | parallel import extraction (was serial) |
| `--impact` | 2m39s | **28s** | parallel graph + O(1) suffix import resolution |
| `--calls --focus zha` | 46s | **35s** | parallel call-edge scan |
| `--verify-edit` | 8min | **75s** | O(1) suffix resolution (the quadratic wall) |
| `--index --engine c` | 12min | **4m30s** | parallel precise symbol pass (was serial re-AST) |

- **`_resolve_import` is no longer quadratic** — step 4 scanned *every*
  module name for *every* unresolved import (~2.5B comparisons on HA-core).
  A per-map suffix index (`_suffix_index`, cached by identity) makes suffix
  resolution O(1); results are byte-identical (133 tests pass).
- **`--graph`/`--calls`/`--impact` are now parallel by default** on large
  repos (the `--parallel` flag previously existed but was never wired into
  these dispatches). `--parallel` stays as an explicit override for
  `--index`/`--cross`/`--deadcode`.
- **C engine's precise pass is parallel** — it re-ASTs every `.py` for
  byte-accurate spans; that was serial, so `--engine c` did C-scan + full
  serial parse (why it ran *slower* than py). Now 3× faster and close to
  parity with the py engine.

## What's new in v0.79.3 (query-fix release)

- **`--query callers/callees` return real answers** — the knowledge graph
  stores call edges as `{module: {caller_func: set(callees)}}`, but
  `render_query` read it as `{module: set(callees)}`: `callers X` tested
  `X in cs` against the module's function-KEYS, so it only ever matched the
  module *defining* X (on HA-core: 20 real callers, 1 bogus result), and
  `callees X` looked up X as a module key. Callers now scan every module's
  callee sets; callees accept either a module or a function name.
- Regression test: `--query callers` on a 3-module fixture returns the
  cross-module caller.

## What's new in v0.79.2 (C-engine parity release)

Found by running codeloom against home-assistant/core (18,484 Python files,
287 MB — the opposite of a small repo):

- **C engine now extracts Python + C import edges** — the C core's import
  matcher only handled quoted sources (`import 'x'`, `require('x')`), so
  Python's `import x.y` / `from x import y` (unquoted) and C `#include`
  lines were silently dropped: `--index --engine c` produced ~35% fewer
  edges than the py engine and `--query dependents/hubs` came back empty.
  Python `from`/`import` parsing and `#include` handling are fixed and
  covered by tests.
- **Stale accelerator binaries are rebuilt, never silently served** — the
  shipped `codeloom_core` had lagged `codeloom_core.c` by four commits; the
  finders only auto-built when the binary was *missing*. Now a binary older
  than its committed source is rebuilt on demand (`--engine c` /
  `--watch-core` / `--serve`) and `--build-core` rebuilds stale cores.
- Verified: 132 tests pass, including 3 new C-engine regression tests.

## What's new in v0.79.1 (hardening release)

A robustness + security pass — no new commands, everything below is
verified by regression tests (`python3 tests.py`, 128 tests):

- **MCP server survives garbage input** — handler exceptions answer `-32603`
  (was: process death), malformed JSON answers `-32700` and keeps serving,
  JSON-RPC batches handled, bad arg types (`max_files:"abc"`, `repos:[123]`)
  return clean errors instead of killing the server
- **`--json` on every command** — all 51 dispatch branches now emit
  machine-readable JSON with `--json` (previously ~30 silently ignored it)
- **Security**: file walker skips symlinks resolving outside the repo root;
  crafted index entries pointing through symlinks are dropped (realpath
  checks on the JSON index, lazy dbm store, and symbol re-reads); module
  names are JS-escaped in `--graph-html` (hostile filenames can't execute);
  the pre-commit hook's script path is properly quoted
- **Honest ledgers** — `--session` logs real wall time + real output bytes
  (was: hardcoded `0.0s`); `--diff` on a non-git directory says so instead
  of fabricating; `--snippet` is byte-accurate on multi-byte files and
  rejects invalid ranges; file hashing is full-file SHA-256 (the old 8KB
  sample could miss same-length edits past byte 8192)
- **Dead code removed** — 5 never-called functions deleted
  (`_scan_calls`, `lazy_index_has`, `dedupe_symbols`,
  `_embeddings_available`, `memory_enforce_caps` — caps were already
  enforced inline per write)

## Quickstart

```bash
# Option A: copy the one file (no pip, no deps)
curl -O https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py

# Option B: pip
pip install codeloom

# Map any repo (<1s to first result — verify: `time python3 codeloom.py <repo> > /dev/null`)
python3 codeloom.py /path/to/repo > AGENTS.md

# Tell your agent: "read AGENTS.md first"
```

Wire it into your agent natively (17 supported):

```bash
python3 codeloom.py --install-agent claude-code   # or cursor, codex, gemini-cli,
                                                  # opencode, cline, aider, ...
```

## What it gives your agent

| Command | What the agent gets |
|---|---|
| `--pack "TASK"` | **One-shot brief**: reading order + impact + relevant symbols, pre-computed |
| `--answer "Q"` | Cited answer with calibrated confidence |
| `--context-card S1 S2` | Batch triage card for N symbols in one call |
| `--why QUERY` | Decision lookup stamped `[exact]`/`[fuzzy]`/`[unverified]` |
| `--plan TASK` | Agent-native prioritized reading plan |

### Working memory across compaction

*No competitor README we examined mentions compaction.*

```bash
codeloom --decide "use retry(3) not retry(∞) — unbounded hangs agents"
codeloom --checkpoint --task "fix login bug"     # save working state
codeloom --resume                                 # restore after compaction
```

*Measured: `--resume` restores the structural map + decision ledger in 2 calls / ~985 tokens vs 33 calls / 21,636 tokens for the bare grep-and-read re-derive (95.4% fewer) — verify: `python3 benchmarks/compaction_recovery.py --repo /tmp/bench-fastapi`*

Also: `--remember`, `--seen`, `--working-state`, `--lessons`, `--supersede`,
`--adr`, `--query-memory`.

**Growth bounds (v0.78).** Memory can't stack forever: each ledger
file caps at **200KB**, then rotates **losslessly and deterministically** to
`.codeloom-memory/archive/` (rotation is byte-exact — nothing is dropped).
codeloom **never auto-deletes** memory; the only way to shrink it is
`--memory-prune` (reports by default — see what would be removed, then
apply with `--memory-prune --delete`) — the
agent's explicit choice, never the tool's.

## Memory OS — the graph remembers for you

Shipped in **v0.79**, Memory OS turns the repo's memory from a plain ledger
into a **typed, importance-scored, graph-linked memory layer** — our main
addition in this release.

### Typed memory objects

Every memory is a typed record appended to `.codeloom-memory/memory.jsonl`
with a fixed schema:

```json
{"type": "decision",            // decision|bug|lesson|architecture|api|constraint|
                               // question|todo|warning|goal|hypothesis
 "id": "decision-042",          // deterministic "<type>-NNN" per-type counter
 "title": "use retry(3) not retry(∞)",
 "body": "unbounded hangs agents; 3 keeps backoff bounded",
 "reason": "",
 "affected_symbols": ["retry", "agent_loop"],
 "importance": 65,
 "confidence": 0.9,             // per-type: decision 0.9, bug 0.8, question 0.4 …
 "tier": "active",              // hot|active|archive
 "timestamp": "2026-08-22T12:00:00Z",
 "created": "memory"}           // decide|lesson|adr|goal|hypothesis|checkpoint|extract|memory
```

Typed objects beat markdown walls because retrieval can be precise: filter
by `type`, rank by `importance`, resolve by `id`, and trace exactly which
symbols a memory touches.

### Importance scoring

Every memory gets a 0–100 importance score — deterministic, no LLM:

```
importance = base(10)
           + keywords     (+30 if title/body contains always|never|must|critical|
                            important|security|do not|dont)
           + type_weight  (bug +20, architecture/constraint/warning +15,
                           decision +10, todo/goal/hypothesis/lesson/question/api +5)
           + graph_centrality (0 affected symbols → +0, 1–2 → +5, 3+ → +10)
           + recency     (write-time +10; read-time +10 within 7 days, +5 within 30)
           , capped at 100
```

`--memory-stats` reports the distribution (counts per type/tier, total and
archive bytes, top linked symbols) so you can see what the repo knows and
what it cares about.

### Graph-linked retrieval: `--memory <symbol>`

`--memory <symbol>` doesn't just match text — it returns **entries that
mention the symbol *plus* entries attached to its graph neighbors**
(dependents, dependencies, callers), scored by importance. Ask about
`validate()` and you get the memory attached to `login()` that calls it.
That's the difference between a journal and a memory that's wired into the
codebase's shape.

### The auto-extractor

`scripts/memory_extract.py` (stdlib-only, no LLM, no network) mines **git
history** into typed memories: deterministic regex heuristics classify
commits (`bug` → confidence 0.7, `api` → 0.6, `architecture` → 0.55;
`regression`/`critical` bumps bug confidence, capped at 0.95) and derive
`affected_symbols` from the files each commit touched. Idempotent via
`.codeloom-memory/extract-state.json`; `--dry-run` previews what would be
extracted without touching anything.

```bash
python3 scripts/memory_extract.py --repo .            # extract new memories
python3 scripts/memory_extract.py --all               # re-attempt everything
python3 scripts/memory_extract.py --dry-run           # preview only
```

The old CLI surface still works as the manual layer: `--memory-add`
(typed memory objects) / `--decide`/`--remember` (free-text note), `--adr`,
`--query-memory`.

### Structural intelligence

| Command | Result |
|---|---|
| `--graph` | Full import graph (measured on this repo: 385 modules, 1126 edges in <1s — verify: `python3 codeloom.py --graph <repo>`) |
| `--cross` | Cross-file call graph, AST-resolved |
| `--search` / `--usages` / `--grep` / `--read` | Symbol index, call sites, snippets, token-efficient source |
| `--get-symbol X` | Summary-first retrieval (measured 97.8–99.2% token savings vs grep-and-read, 15 task-runs — verify: `python3 benchmarks/token_efficiency.py`) |
| `--impact M` / `--refactor` / `--rename` | Blast radius prediction |
| `--similar` / `--deadcode` / `--explain` | Refactoring intelligence, zero LLM |
| `--trace` | Runtime call edges static analysis can't see |
| `--routes` / `--channels` | HTTP routes, pub-sub event channels |
| `--pattern '$F($$$ARGS)'` | **Structural AST search** with metavariable capture |

### Speed & quality

| Command | Result |
|---|---|
| `--health` | Code-health screen: 0–10 per file, **0.2s** (measured on this repo; verify: `time python3 codeloom.py --health <repo>`), deterministic detectors |
| `--risk HEAD~1..HEAD` | Change-risk score 0–100 + named drivers for any commit range |
| `--embed-search Q` | Semantic search offline — subword-hash, zero deps (ggml opt-in) |
| `--watch` → `--watch-merge` | Live freshness: native watcher pipes into the persistent index |
| `--engine c` | Auto-building C core: Linux-kernel full graph (C engine) ~89-113s ([bench](benchmarks/hardware-scaling.md)) |
| `--verify FILE` | SHA-256 checksum verification |
| `--verify-edit` (v0.78) | **Post-edit integrity oracle** — GO/CHECK/STOP verdict after an edit; cycle detection diffs against the **full HEAD graph**, so pre-existing cycles never trigger STOP — only cycles the diff actually introduces |

**50 tree-sitter languages dispatched · 46 fixture-proven** — golden-file parity tests gate CI on every grammar, verify: `python3 tests.py` · **130+ extensions via regex fallback**.

## MCP server (82 tools + 1 router)

```json
{"command": "python3", "args": ["-m", "codeloom_mcp"]}
```

Or auto-wire any of 17 agents: `codeloom --install-agent <name>`.

82 tools total, but the agent's effective surface is **one tool**:
`codeloom_ask` takes natural language and routes deterministically — a
table-driven dispatch, not an LLM tool-selection step. Full listing:
[`docs/mcp-listing.md`](docs/mcp-listing.md).

The v0.79 MCP surface adds the **Memory OS trio**: `codeloom_memory_add`
(typed memory objects with importance), `codeloom_remember` (graph-linked
retrieval) and `codeloom_memory_stats` (the distribution report) — the
`memory`/`remember` retrieval phrases route from `codeloom_ask`, and all
three stay directly callable (alongside `codeloom_query_memory`). v0.78's
loop-closure pair is still there: `verify_edit`
(post-edit integrity oracle) and `blindspot` (unread-file warning), plus
`loom://resources` exposing state/delta/hotset/resume as resources, not
just tools.

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
Summary against the 8-row field — code-review-graph, code-context-engine,
claude-context, codeseek, jcodemunch, codegraph, codebase-memory-mcp, repowise
(README-level review May 2026; crg measured live on the same fastapi clone
2026-08-22 — see [`benchmarks/README.md`](benchmarks/README.md) for numbers and
reproduction commands):

| | **codeloom** | code-review-graph (30.6k★) | code-context-engine | claude-context |
|---|---|---|---|---|
| Install | **one stdlib file** | pip: **75 packages** + daemon + TOML config ([bench](benchmarks/README.md), counted from their `pyproject.toml`, 2026-08-22) | pip + ONNX + server | npm |
| Background process | **none** | `crg-daemon` (16MB RSS, health checks) | `cce serve` + resource governor | — |
| Compaction memory | ✅ **decision ledger + Memory OS: typed, graph-linked `memory.jsonl` objects** — 2 calls / ~985 tok to recover, 95.4% fewer than bare re-derive ([bench](benchmarks/README.md), measured 2026-08-22, same repo & symbols as the others) | ⚠️ markdown Q&A journal — no compaction coverage found in README review (May 2026) | ⚠️ agent-called `record_decision` MCP | memsearch plugin |
| MCP surface | **82 + 1 NL router** | 30, no router | 22 | many |
| Semantic search | ✅ zero-dep, offline | ❌ `[embeddings]` extra (~2GB) or cloud key | ❌ ONNX required | ✅ (Zilliz) |
| Language proof | **46 fixture-proven in CI** (verify: `python3 tests.py`) | not published | — | — |
| Setup→answer | **0.13s warm** (measured 2026-08-22 on the same fastapi clone; verify: `python3 benchmarks/vs_crg.py --repo /tmp/bench-fastapi --symbols Body,Cookie,File,Header --no-setup`) | 8.6s pip + 4s build + daemon (measured 2026-08-22) | after indexing | after indexing |

Measured numbers (same repo, same symbols, same tokenizer — crg live
2026-08-22): symbol retrieval **43–54× fewer tokens** than crg (9–10 vs 428–485) — verify: `python3 benchmarks/vs_crg.py --repo /tmp/bench-fastapi --symbols Body,Cookie,File,Header --no-setup`; compaction recovery **95.4% fewer tokens** — verify: `python3 benchmarks/compaction_recovery.py --repo /tmp/bench-fastapi`;
Linux kernel full graph (C engine) ~89-113s ([bench](benchmarks/hardware-scaling.md)).
Details and reproduction commands in [`benchmarks/README.md`](benchmarks/README.md);
run the whole suite with `python3 benchmarks/eval_runner.py bench --root /tmp/bench-fastapi`.

Where competitors are ahead, stated plainly: jcodemunch has broader safety
preflight (edit/delete-safe, SCIP compiler verification); codegraph has 67k★
community scale; codebase-memory ships 158 grammars and an arXiv-published
eval; repowise (AGPL) has defect-validated risk scoring. We claim speed +
shape + proof-per-grammar + memory depth — not their moats.

Shipped in v0.78, we close a loop the preflight tools don't: `--verify-edit` gives
the post-edit GO/FAIL verdict (their preflight stops at *before*),
`--blindspot` warns when files you never read are about to break,
`--savings-report` publishes a **local-only** token-savings ledger (no
telemetry — receipts live in the repo, not in our README), and
`--install-hook`/`--uninstall-hook` add a warn-only pre-commit risk hook.
v0.79 adds what no competitor README we examined (May 2026) ships:
**Memory OS — typed, importance-scored memory objects linked to the code
graph**, retrieved by symbol and graph neighbors (`--memory <symbol>`,
`codeloom_remember`).

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
| [`docs/FAQ.md`](docs/FAQ.md) | "vs LSP/RAG/repomix/code-review-graph" — honest tradeoffs |
| [`docs/mcp-listing.md`](docs/mcp-listing.md) | MCP marketplace listing copy |
| [`bench/RESULTS.md`](bench/RESULTS.md) | Replay-bench results (loss rows published) |
| [`BENCHMARKS.md`](BENCHMARKS.md) | Measured performance numbers |
| [`TECHNICAL_REPORT.md`](TECHNICAL_REPORT.md) | Architecture decisions write-up |
| [`AGENT_TRACE.md`](AGENT_TRACE.md) | Agent before/after task trace |

## Trust & verification

- **CI**: Linux/macOS/Windows × Python 3.8–3.12, 128 tests, ≥46 grammar
  fixtures gated by golden files
- **Checksums**: every release publishes the SHA-256 of `codeloom.py`;
  verify with `codeloom --verify codeloom.py`
- **Auditable**: one stdlib file — read the whole thing before running it

## Evidence: verification

The numbers below are our own measurements, published with their reproduction
commands. They are honest but self-published — the fastest way to verify is to
run the one-command check yourself.

| Claim | How to verify | Time |
|---|---|---|
| Token efficiency: 98.8% fewer tokens than grep-and-read (15 task-runs, 3 repos, cl100k_base) | `python3 benchmarks/token_efficiency.py` (auto-clones the 3 repos into /tmp if missing) | ~2–5 min |
| Compaction recovery: 95.4% fewer tokens; 2 calls vs 33 | `python3 benchmarks/compaction_recovery.py --repo /tmp/bench-fastapi` (clone fastapi first: `git clone --depth 1 https://github.com/fastapi/fastapi.git /tmp/bench-fastapi`) | ~1 min |
| Sealed retrieval: ~9× fewer tokens, 2.9× fewer calls, no LLM | `python3 benchmarks/live_sealed_run/harness.py --mode all --repo /tmp/bench-fastapi` | ~1 min |
| Symbol retrieval vs code-review-graph: 43–54× fewer tokens | `python3 benchmarks/vs_crg.py --repo /tmp/bench-fastapi --symbols Body,Cookie,File,Header --no-setup` (needs crg installed for the crg side) | ~2 min |
| Memory graph retrieval: 10/10 direct + graph-neighbor hits, 85 ms avg (synthetic repo) | `python3 benchmarks/memory_eval.py` (default = fast synthetic repo; `--repo` for a real one) | ~30 s |
| Whole suite, one command | `python3 benchmarks/eval_runner.py bench --root /tmp/bench-fastapi` | ~5 min |

### Dogfood head-to-head (2026-08-23, pallets/flask, same-session)

We also ran codeloom against a plain grep+read agent on a small repo — and the
result is honestly mixed. On small repos with terminal-payload counting,
codeloom used **more** total tokens (+14.5%) and wall time (+2.6×) than the
plain grep+read agent; it won on evidence — `--impact` gave the blast radius
(5 direct + 33 transitive in 0.23s), `--task` ranked the exact 4 modules, and
`--checkpoint`/`--checkpoint-restore` reproduced the exact edit diff. The
token-efficiency claims above (98.8%, 43–54×) hold for **big repos and
chains-of-calls vs grep+read baselines** — that scope is where they were
measured, not on small single-shot tasks.

### Verification status

| Status | Meaning |
|---|---|
| SELF-PUBLISHED — reproducible by anyone: **yes** | Every number above comes with the exact command that produced it (same repo, same symbols, same tokenizer). |
| INDEPENDENT THIRD-PARTY BENCHMARK — **none yet** | All numbers are first-party measurements. We welcome independent ones; file an issue or PR if you run the suite and get different figures. |
| REAL-WORLD USAGE REPORTS — pending (out of scope) | We don't collect telemetry and don't claim adoption numbers. |
| LONG-TERM MAINTENANCE — pending (out of scope) | No track record to point at yet; the test suite (`python3 tests.py`) is the current gate. |

We'd rather you check: if a number doesn't reproduce on your machine, file an
issue with your output — that's a bug.

## Contributing

PRs welcome. Run tests with `python3 tests.py`. Ethos: zero-dependency, fast,
one file, honest claims.

## Translations

[简体中文](docs/translations/README.zh-CN.md) · [日本語](docs/translations/README.ja.md) · [Español](docs/translations/README.es.md) · [हिन्दी](docs/translations/README.hi.md)

Generated with v0.79.1 — may lag after upgrades.

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
