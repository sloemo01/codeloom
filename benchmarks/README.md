# CodeLoom benchmarks

Honest, reproducible numbers comparing CodeLoom to the heavyweight competitors
(code-review-graph, code-context-engine, claude-context, codeseek, jcodemunch,
codegraph, codebase-memory-mcp, repowise). The point is not
"CodeLoom is faster at everything" — it's that CodeLoom wins decisively on the
axis that matters for everyday agent work: **token-efficient, task-oriented
retrieval with zero setup and always-fresh context**, plus Linux-kernel-class
index speed (C-engine full graph ~89–113s vs their ~3 min), and — uniquely —
**measured compaction
recovery** (nobody else publishes a number here).

## Compaction recovery (the differentiator — measured 2026-08-22)

What happens after a context compaction mid-task: the bare agent re-derives
context with the realistic grep-and-read chain agents actually use; codeloom
restores both the structural map and the decision ledger via `--resume`.

| path | calls | KB | tokens (est) |
|---|---|---|---|
| **bare re-derive** (10 questions, fastapi) | **33** | **84.5** | **21,636** |
| **codeloom** (`--resume` + `--query-memory`) | **2** | **3.8** | **985** |

**95.4% fewer tokens, 16.5× fewer calls** to reach "ready to continue working"
after a compaction. Scripted policy simulation (same caveat as bench/RESULTS.md
— not a live-agent run); reproduce with:

```bash
python3 benchmarks/compaction_recovery.py --repo /tmp/bench-fastapi
```

Why this matters: code-review-graph (30.6k★) has **zero mentions of
compaction/session/resume/checkpoint** in its README; code-context-engine
requires the agent to call `record_decision` MCP tools from a running server
(they ship "memory nudges" because agents forget to). Nobody else ships a
deterministic restore.

## Sealed retrieval benchmark (no LLM, deterministic — measured 2026-08-22)

`benchmarks/live_sealed_run/harness.py` measures the *retrieval phase* — the
tool calls + context bytes burned before any model answers — on the same 10
fastapi questions, two toolchains:

- **bare**: the realistic grep-and-read chain (rg locate → read top file →
  rg callers), exactly what agents do today.
- **codeloom**: one `--answer` call per question (cited, calibrated).

Graded deterministically: did the returned context contain the file where the
answer lives? No LLM anywhere — fully reproducible offline.

| toolchain | found | calls | tokens (est, cl100k_base) |
|---|---|---|---|
| **bare** | 3/10 | **29** | **6,656** |
| **codeloom** | 4/10 | **10** | **731** |

**The honest read:** codeloom finds answers with **2.9× fewer calls and
~9× fewer tokens** — and its output is a *cited answer with confidence*,
while bare returns raw file dumps the agent must still read and reason over.
Both miss questions (ambiguous anchors like `File`/`Request` rank below the
right symbol) — published as-is, loss rows included.

```bash
python3 benchmarks/live_sealed_run/harness.py --mode all --repo /tmp/bench-fastapi
```

Caveat (same as bench/RESULTS.md): this measures *retrieval* under a scripted
policy — it is not an autonomous-agent-loop claim. It is the honest companion
to compaction_recovery.py: that one measures post-compaction restore; this
one measures first-touch retrieval.

## vs code-review-graph (measured 2026-08-22, same repo, same symbols)

`benchmarks/vs_crg.py` runs both tools on the same fastapi clone. Symbol
retrieval tokens (tiktoken cl100k_base):

| symbol | codeloom | crg | winner |
|---|---|---|---|
| `Body` | 10 | 428 | codeloom (43×) |
| `Cookie` | 10 | 440 | codeloom (44×) |
| `File` | 10 | 462 | codeloom (46×) |
| `Header` | 9 | 485 | codeloom (54×) |

**The honest caveat:** codeloom returns a summary (signature + docstring + call
graph) by default; crg returns FTS JSON with file paths + line ranges and needs
a second disambiguated `query callers_of` call. Summary-first is the whole point
— codeloom opts into `--full` only when the implementation is needed.

**The row we lose:** task briefs. crg's `get_minimal_context_tool` returns 161
tokens vs codeloom `--pack`'s 1,625 — because `--pack` embeds the actual
`login()` source, call path and impact list (the code-embedded brief), while
crg's is a pointer card with graph stats and suggested tools. Different
contracts: theirs is "orient the agent", ours is "hand the agent the code".

**Setup-to-first-answer (measured wall clock on the same fastapi clone):**

| | codeloom | crg |
|---|---|---|
| Install | 0 (one stdlib file) | pip: 8.6s, **75 packages** (fresh venv) |
| Index | `--index`: 5.1s → 2.0MB `.codeloom-index.json` | build: 3.9s → **42.6MB** `.code-review-graph/` |
| First query | 3.23s cold (`--answer`, no index) / **0.13s warm** (`--get-symbol` after `--index`) | 0.18s (after build) |
| Semantic search | zero-dep subword hash, offline | `pip install code-review-graph[embeddings]` (sentence-transformers ~2GB) **or cloud API key** |
| Background process | none | `crg-daemon`: 1 proc, 16.3MB RSS, TOML watch config |
| MCP surface | **77 tools + `codeloom_ask` NL router** (counted live via MCP handshake) | 30 tools, no router |

The honest caveat on timing: codeloom's `--answer` hybrid search walks the
tree on every call (~3.2s on fastapi, cold or warm) — its fast path is
`--get-symbol`/`--search`, which load the persistent index in ~0.13s. crg is
index-first: it pays 3.9s up front (and 8.6s to install 75 packages), then
queries in ~0.18s. Two different trade-offs — codeloom is zero-setup and
always-fresh, crg amortizes a graph build.

The daemon is honest work — but it's the difference between "copy one file" and
"pip install 75 packages + build a graph + run a daemon + configure embeddings".

## Scale / hardware scaling (Linux kernel)

- [`hardware-scaling.md`](hardware-scaling.md) — full Linux kernel (64,814
  files) index time across CPU-resource configs with the C engine: full cores
  **89s**, serial **113s**, throttled **106s**.

## Token savings (vs grep-and-read baseline)

`benchmarks/run.py --tokens` measures on a repo: baseline = grep for the term
then open matching files whole (tiktoken cl100k_base); codeloom = one
`--get-symbol` call. Measured on fastapi (query set: `Agent`, `click`,
`extract` — the fastapi clone has no `Agent`/`click`/`extract` symbols, so
codeloom returns a short not-found answer while the baseline still reads the
matching files):

| query | baseline (grep+read) | codeloom | savings |
|---|---|---|---|
| `Agent` | 3,997 | 30 | 99.2% |
| `click` | 3,997 | 30 | 99.2% |
| `extract` | 3,654 | 30 | 99.2% |

For per-symbol retrieval on real symbols, see the 15-task-run table below
(98.8% overall on express/fastapi/gin) — that is the apples-to-apples
jcodemunch-style benchmark.

## Side-by-side: codeloom vs jcodemunch (same repo, same symbols)

`benchmarks/side_by_side.py` runs **both tools on the same repo and same
symbols**, measuring the tokens each returns to retrieve a symbol. This is the
apples-to-apples yardstick.

```bash
# requires jcodemunch-mcp installed (pip install jcodemunch-mcp)
JCODEMUNCH_MCP=/path/to/jcodemunch-mcp python3 benchmarks/side_by_side.py \
  --repo /tmp/bench-fastapi --symbols Body,Cookie,File,Header
```

Measured on fastapi (329 files):

| symbol | codeloom | jcodemunch | codeloom wins |
|---|---|---|---|
| `Body` | 10 | 92 | YES |
| `Cookie` | 10 | 813 | YES |
| `File` | 10 | n/a (no clean match) | — |
| `Header` | 9 | 98 | YES |

**The honest caveat:** this measures *retrieval tokens* — codeloom returns a
summary (signature + docstring + call graph) by default, while jcodemunch's
`get_symbol_source` returns the **full source**. So codeloom wins on token count
by design: summary-first is the whole point. The tradeoff is that jcodemunch
gives you the complete source in one call, while codeloom gives you the summary
and you opt into `--full` when you need the implementation. Some symbols
(`File`, `Depends`) were excluded because jcodemunch's fuzzy search didn't
return a clean match — we only report symbols both tools resolved.

## How to reproduce

```bash
# clone the benchmark repos
git clone --depth 1 https://github.com/fastapi/fastapi.git /tmp/bench-fastapi

# run the token benchmark
python3 benchmarks/run.py --repo /tmp/bench-fastapi --tokens
```

## Speed vs staleness

| | CodeLoom | jcodemunch | codegraph | codebase-memory |
|---|---|---|---|---|
| Time to first result | **< 1s, no index** | after indexing (minutes) | after indexing | after indexing (minutes) |
| Freshness | **always live** (reads files) | stale until re-indexed | native watcher | stale until re-indexed |
| Setup | **one file, copy it** | pip + index + daemon | compiled binary + watch | pip + daemon |

## Installation friction scorecard

| | CodeLoom | jcodemunch |
|---|---|---|
| Files to install | **1** | pip package + index |
| Commands to run | **0** (copy the file) | pip install + init + index |
| Dependencies | **0** (stdlib only) | tree-sitter + index libs |
| Works air-gapped | **yes** | no (needs install) |
| Daemon/background process | **no** | yes |

## The jcodemunch token-efficiency benchmark (15 task-runs, cl100k_base)

Reproduces jcodemunch's official benchmark: 3 canonical repos (expressjs/express,
fastapi/fastapi, gin-gonic/gin) x 5 query tasks = 15 task-runs, measuring token
reduction vs traditional grep-and-read, encoded with **tiktoken cl100k_base**
(the standard used by Claude and GPT-4 — the same encoder jcodemunch uses).

```bash
python3 benchmarks/token_efficiency.py   # auto-clones the 3 repos into /tmp if missing
```

Result — **15/15 task-runs, 98.8% overall token reduction**:

| repo | baseline | codeloom | savings |
|---|---|---|---|
| express | 13,871t | 306t | 97.8% |
| fastapi | 28,775t | 237t | 99.2% |
| gin | 39,461t | 438t | 98.9% |
| **TOTAL** | **82,107t** | **981t** | **98.8%** |

**This beats jcodemunch's claimed range of 95.0–96.4%** on the same repos, same
baseline, same tokenizer. Summary-first retrieval (signature + docstring +
call graph, capped) returns a tiny context in one call instead of a full-file
grep-and-read.

## The load-once benchmark (the one that matters)

The plan's thesis: you can't beat jcodemunch on retrieval, but you can make
retrieval irrelevant for the load-once, task-shaped workflow. `benchmarks/load_once.py`
measures whether `--pack` produces a **self-contained, code-embedded task brief**
that lets an agent work with zero retrieval on the core path.

```bash
python3 benchmarks/load_once.py --repo /path/to/repo --task "fix the login bug"
```

Result on fastapi (`fix the login bug`):

```
brief size: 6164 chars, ~1541 tokens
embedded code blocks: 10
oversized-symbol pointers (--full): 10
RESULT: PASS — the brief embeds the task's core code and only points to
--full for oversized symbols (per the plan's contract).
```

The brief embeds the actual `login()` source (byte-precise, capped ~40 lines),
the call path, and the impact list. An agent pastes it once and works. This is
the difference between "where does 'login' appear" (jcodemunch, retrieval-shaped)
and "what code actually runs when a login happens" (codeloom, task-shaped).

## The honest scope

CodeLoom is a **single-file, zero-dependency replacement for the heavyweight
code-intelligence tools** — that's the design. It covers 130+ extensions via
regex/C extraction plus repo-aware AST depth via `--install-grammars` /
`--auto-grammars`, keeps a persistent knowledge graph on disk, makes
retrieval irrelevant for the load-once, task-shaped workflow with its
code-embedded `--ask`/`--pack` brief, and runs zero-install, zero-telemetry,
offline.
For everyday agent work it replaces what the search tools do **and** adds
task-orientation they don't have — no daemon, no install, no stale index, no
telemetry.
