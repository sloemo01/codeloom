# CodeLoom benchmarks

Honest, reproducible numbers comparing CodeLoom to the heavyweight competitors
(semble, codebase-memory-mcp, jcodemunch-mcp). The point is not "CodeLoom is
faster at everything" — it's that CodeLoom wins decisively on the axis that
matters for everyday agent work: **token-efficient, task-oriented retrieval
with zero setup and always-fresh context.**

## Token savings (vs grep-and-read baseline)

Measured on the same repos jcodemunch benchmarks against. `--get-symbol` is
summary-first by default (signature + docstring + call graph, not full source).

| repo | symbols found | baseline (grep+read) | codeloom | savings |
|---|---|---|---|---|
| **fastapi** | 5/5 | 665,765 | 7,465 | **98.9%** |
| express | 2/5 | 17,806 | ~0 | (partial — see note) |
| gin | 3/4 | 2,227 | ~0 | (partial — see note) |

**The honest headline is fastapi: 98.9%** — a real, large repo (329 files)
where CodeLoom found all 5 queried symbols. The express/gin figures are
excluded from the headline because CodeLoom only resolved a subset of the
queried symbols there (2/5 and 3/4), so their "100%" is not a fair comparison.

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

CodeLoom is a **single-file, zero-dependency code-intelligence tool** — that's
the design. It covers 25 languages via `--install-grammars`, keeps a persistent
knowledge graph on disk, and makes retrieval irrelevant for the load-once,
task-shaped workflow with its code-embedded `--pack` brief. It wins on **speed,
zero-setup, freshness, task-awareness, and offline operation** — the workflow
that dominates everyday agent use.
