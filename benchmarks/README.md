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

## The honest caveat

CodeLoom is a **complement, not a replacement**, for the heavy tools. If you
need tree-sitter precision across 158 languages, a persistent knowledge graph,
or snippet-level search on a 28M-LOC monorepo, the heavyweight tools are
genuinely better at those. CodeLoom wins on **speed, zero-setup, freshness, and
task-awareness** — the 80% case for everyday agent use.
