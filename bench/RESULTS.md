# codeloom static-replay benchmark

> **Static replay, not live-model measurement.** Tool-call counts and
> context bytes come from scripted policies (one `--answer` call vs.
> grep+read chains). Live-agent numbers require the sealed-harness
> protocol described in TECHNICAL_REPORT.md §7. Never cite these as
> agent-token savings.

Repo: `/tmp/bench-fastapi` · 20 questions · 20 codeloom wins / 0 bare wins / 0 ties

Generated 2026-08-22 08:41 UTC by `python3 bench/run_bench.py <repo_root>` — re-run to reproduce.

| question | codeloom calls | codeloom bytes | bare calls | bare bytes | winner |
|---|---|---|---|---|---|
| where is the retry logic | 1 | 218 | 6 | 61805 | codeloom |
| what calls the main entry point | 1 | 261 | 8 | 265360 | codeloom |
| how does the engine initialize | 1 | 208 | 5 | 65363 | codeloom |
| where are config values read | 1 | 209 | 9 | 57616 | codeloom |
| what breaks if I change the parser | 1 | 258 | 2 | 32390 | codeloom |
| which module handles authentication | 1 | 192 | 9 | 55452 | codeloom |
| where is error handling centralized | 1 | 289 | 7 | 18319 | codeloom |
| what tests cover the api layer | 1 | 228 | 8 | 46236 | codeloom |
| who uses the cache helper | 1 | 260 | 9 | 105675 | codeloom |
| where do imports of utils converge | 1 | 395 | 5 | 22877 | codeloom |
| what is the data model | 1 | 299 | 6 | 42329 | codeloom |
| where is logging configured | 1 | 247 | 6 | 12047 | codeloom |
| how do modules depend on each other | 1 | 237 | 9 | 58018 | codeloom |
| which functions have no callers | 1 | 624 | 7 | 38772 | codeloom |
| where is serialization done | 1 | 149 | 6 | 36751 | codeloom |
| what owns database access | 1 | 325 | 8 | 56774 | codeloom |
| where are cli flags parsed | 1 | 252 | 6 | 228768 | codeloom |
| what is the public api surface | 1 | 389 | 4 | 38883 | codeloom |
| where would a rate limiter go | 1 | 254 | 9 | 19675 | codeloom |
| which file defines the version | 1 | 254 | 8 | 48103 | codeloom |

## Rows we lose

(none in this run)
