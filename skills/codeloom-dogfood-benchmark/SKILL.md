---
name: codeloom-dogfood-benchmark
description: Benchmark codeloom vs a plain agent; audit its full surface.
version: 1.0.0
author: Nehal (sloemo01)
license: MIT
metadata:
  hermes:
    tags: [benchmark, codeloom, dogfood, comparison, tokens]
    related_skills: [codeloom]
---

# codeloom dogfood benchmark (plain agent vs codeloom full surface)

Deterministic head-to-head that answers "how good is codeloom vs a plain agent?" — measured, honest loss rows, no invented numbers.

## When to use
- Any "is codeloom worth it?" comparison
- Post-release regression of the token-savings claims (98.8%, 43–54×)
- Full-surface audit after a feature lands

## The protocol (measured 2026-08-23, ~10 min)

1. **Clone 3 scales** (record repo + commit SHA):
   - Small: `git clone --depth 1 https://github.com/psf/requests /tmp/bench-small`
   - Mid: `git clone --depth 1 https://github.com/pallets/flask /tmp/bench-mid`
   - Large: `git clone --depth 1 --filter=blob:none https://github.com/django/django /tmp/bench-large`
2. **Version + mode**: `python3 ~/Desktop/codeloom/codeloom.py --version`. Check `mcp_codeloom_*` tools live; if not, shell out. Note which.
3. **Tokenizer**: tiktoken `cl100k_base` if importable (try `/tmp/bench-venv`), else `bytes/4`. State which everywhere.
4. **Task (verbatim, every run)**: "Explain the request lifecycle end-to-end (entry → handler → response). Name the 5 most important functions involved. Propose adding a new middleware — with the blast radius of that change." (Adapt once per repo domain; state the new verbatim task.)
5. **Phase 1 — harness** (deterministic, no LLM):
   `python3 ~/Desktop/codeloom/benchmarks/dogfood_bench.py --repo /tmp/bench-mid --task "explain the request lifecycle" --expect request,response,route --runs 2`
6. **Phase 2 — same-session head-to-head** (mid + large): Run A = plain tools (search_files/read_file/grep), Run B = codeloom core loop: map, graph, health, task, context-card, pack, get-symbol ×N, search, answer, why, impact, calls --focus, cross; safety loop on a scratch copy (cp -r + git init + commit, ONE edit → verify-edit, risk, blindspot); memory loop (decide, lesson, checkpoint + checkpoint-restore, memory-add, memory <sym>, memory-stats, working-state). Record calls/tokens-in/tokens-out/total/wall/completeness for both.
7. **Phase 3 — full-surface sweep** (76 commands): orientation (map bare positional, graph, health, task, context-card, pack), retrieval (get-symbol, search, find, calls, focus, cross, why, answer, grep-symbolic, grep, read, explain, usages, similar, precision, pattern, hybrid-search, embed-search, routes, framework), safety (blindspot, risk, verify-edit, impact, deadcode, check-delete), memory (goal, decide, reject, hypothesis, lesson, remember, adr, memory-add, memory, memory-stats, query-memory, list-decisions, list-open, memory-prune, checkpoint, working-state), maintenance (churn, timeline, bug-predict, incremental, diff, export, graph-html, savings-report, session-report, architecture, heatmap), engines (--engine c / rust). Every command: worked ✓ / errored ✗ + latency + one-line output. Non-zero exit = finding.
8. **Report**: MASTER table (metric | plain | codeloom | delta | winner), scorecard (B won N of M), scale curve (does B's delta improve with size? crossover ~1–2k files), loss rows, coverage matrix, reproducibility commands. Never hide a loss row.

## Measured findings (2026-08-23, codeloom 0.79.0)
- Small/mid: B saves −71%/−43% total tokens, wins evidence+memory; loses wall (87–334×) and output tokens
- Large (7k files): B LOSES total tokens (+85%) — structured outputs become 40–100k dumps; use only cheap flags (task/impact/why/health) at that scale
- Crossover: ~1–2k files
- Biggest retrieval gap: `--answer` heuristic can misfire ("Request class" vs lifecycle)
- Real bugs found: `--graph --focus` KeyError on missing neighbor module (fixed: graph.get guard in reachable(), codeloom.py:2585)

## Pitfalls (each cost real time)
- `--risk` silently ignores the positional root — must run from INSIDE the repo
- `--blindspot` returns SKIP/no-op until `--mark-seen` is set (silent no-op trap)
- `--memory-prune` has no `--dry-run` — dry-run is the default; `--delete` opts in
- `--graph-html` takes no positional — writes `codeloom-graph.html` in cwd
- `--graph --focus <missing-module>` → KeyError in `reachable()` (fixed with `.get(cur, [])`)
- `map` is the bare positional — no `--map` flag
- `--savings-report` needs `--session` logging first, else empty
- `verify_all.py` needs the gitignored `demo-repo/` fixture (`src/core/engine.py`) or 6 checks fail
- `--memory <symbol>` direct retrieval misses entries unless the write pinned that exact symbol; graph-neighbor retrieval works
- Tail-piping `| tail` masks the real exit code — check `EXIT:$?` of the actual process
- Shell-out mode: `python3 ~/Desktop/codeloom/codeloom.py <flags> <root>`

## Reproducibility
```bash
git clone --depth 1 <repo> /tmp/bench-{small,mid,large}
python3 ~/Desktop/codeloom/codeloom.py --version
python3 ~/Desktop/codeloom/benchmarks/dogfood_bench.py --repo /tmp/bench-mid --task "..." --expect request,response,route --runs 2
# Phase 2/3: the 28-cmd core loop + 76-cmd sweep (see the prompt in session history)
python3 ~/Desktop/codeloom/benchmarks/memory_eval.py --repo /tmp/bench-mid
cd ~/Desktop/codeloom && python3 benchmarks/verify_all.py
```
Rerun: ~10 min (django harness alone 465s).
