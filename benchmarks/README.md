# CodeLoom benchmarks

Honest, reproducible numbers comparing CodeLoom to the heavyweight competitors
(semble, codebase-memory-mcp, jcodemunch-mcp). The point is not "CodeLoom is
faster at everything" — it's that CodeLoom wins decisively on the axis that
matters for everyday agent work: **token-efficient, task-oriented retrieval
with zero setup and always-fresh context.**

## Scale / hardware scaling (Linux kernel)

- [`hardware-scaling.md`](hardware-scaling.md) — full Linux kernel (64,814
  files) index time across CPU-resource configs with the C engine: full cores
  **89s**, serial **113s**, throttled **106s**.

## Token savings (vs grep-and-read baseline)

Measured on the same repos jcodemunch benchmarks against. `--get-symbol` is
summary-first by default (signature + docstring + call graph, not full source).

| repo | symbols found | baseline (grep+read) | codeloom | savings |
|---|---|---|---|---|
| **fastapi** | 5/5 | 665,765 | 7,465 | **98.9%** |
| **express** | 4/4 | 17,806 | ~210 | **98.8%** |
| **gin** | 4/4 | 2,227 | ~93 | **95.8%** |

Every queried symbol now resolves on all three repos (the earlier partial
resolution was a bug — non-Python symbols lacked the `path` key and
assignment-style JS methods like `res.append = function append(...)` were
missed. Both are fixed). The `as` symbol on express was a bad query — it's a
JS keyword, not a real symbol.

**The honest headline: 95.8–98.9% token savings on all three repos** — a real,
reproducible result now that codeloom resolves every queried symbol.

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
| `Body` | 13 | 89 | YES |
| `Cookie` | 13 | 810 | YES |
| `File` | 13 | 821 | YES |
| `Header` | 5 | 95 | YES |

**The honest caveat:** this measures *retrieval tokens* — codeloom returns a
summary (signature + docstring + call graph) by default, while jcodemunch's
`get_symbol_source` returns the **full source**. So codeloom wins on token count
by design: summary-first is the whole point. The tradeoff is that jcodemunch
gives you the complete source in one call, while codeloom gives you the summary
and you opt into `--full` when you need the implementation. Some symbols
(`Depends`) were excluded because jcodemunch's fuzzy search didn't return a
clean match — we only report symbols both tools resolved.

## How to reproduce

```bash
# clone the benchmark repos
git clone --depth 1 https://github.com/fastapi/fastapi.git /tmp/bench-fastapi

# run the token benchmark
python3 benchmarks/run.py --repo /tmp/bench-fastapi --tokens
```

## Speed vs staleness

| | CodeLoom | jcodemunch | semble / codebase-memory |
|---|---|---|---|
| Time to first result | **< 1s, no index** | after indexing (minutes) | after indexing (minutes) |
| Freshness | **always live** (reads files) | stale until re-indexed | stale until re-indexed |
| Setup | **one file, copy it** | pip + index + daemon | pip + daemon |

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
python3 benchmarks/token_efficiency.py
```

Result — **15/15 task-runs, 97.9% overall token reduction**:

| repo | baseline | codeloom | savings |
|---|---|---|---|
| express | 13,871t | 306t | 97.8% |
| fastapi | 28,775t | 987t | 96.6% |
| gin | 39,461t | 438t | 98.9% |
| **TOTAL** | **82,107t** | **1,731t** | **97.9%** |

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
brief size: 6622 chars, ~1655 tokens
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
code-intelligence tools** — that's the design. It covers 25 languages via
`--install-grammars`, keeps a persistent knowledge graph on disk, makes
retrieval irrelevant for the load-once, task-shaped workflow with its
code-embedded `--pack` brief, and runs zero-install, zero-telemetry, offline.
For everyday agent work it replaces what the search tools do **and** adds
task-orientation they don't have — no daemon, no install, no stale index, no
telemetry.
