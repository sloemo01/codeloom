# codeloom static-replay benchmark

> **Static replay, not live-model measurement.** Tool-call counts and
> context bytes come from scripted policies (one `--answer` call vs.
> grep+read chains). Live-agent numbers require the sealed-harness
> protocol described in TECHNICAL_REPORT.md §7. Never cite these as
> agent-token savings.

Repo: `/Users/apple/Desktop/codeloom/--help` · 20 questions · 19 codeloom wins / 1 bare wins / 0 ties

| question | codeloom calls | codeloom bytes | bare calls | bare bytes | winner |
|---|---|---|---|---|---|
| where is the retry logic | 1 | 14008 | 2 | 0 | codeloom |
| what calls the main entry point | 1 | 14008 | 3 | 0 | codeloom |
| how does the engine initialize | 1 | 14008 | 2 | 0 | codeloom |
| where are config values read | 1 | 14008 | 3 | 0 | codeloom |
| what breaks if I change the parser | 1 | 14008 | 1 | 0 | bare |
| which module handles authentication | 1 | 14008 | 3 | 0 | codeloom |
| where is error handling centralized | 1 | 14008 | 3 | 0 | codeloom |
| what tests cover the api layer | 1 | 14008 | 3 | 0 | codeloom |
| who uses the cache helper | 1 | 14008 | 3 | 0 | codeloom |
| where do imports of utils converge | 1 | 14008 | 3 | 0 | codeloom |
| what is the data model | 1 | 14008 | 2 | 0 | codeloom |
| where is logging configured | 1 | 14008 | 2 | 0 | codeloom |
| how do modules depend on each other | 1 | 14008 | 3 | 0 | codeloom |
| which functions have no callers | 1 | 14008 | 3 | 0 | codeloom |
| where is serialization done | 1 | 14008 | 2 | 0 | codeloom |
| what owns database access | 1 | 14008 | 3 | 0 | codeloom |
| where are cli flags parsed | 1 | 14008 | 2 | 0 | codeloom |
| what is the public api surface | 1 | 14008 | 2 | 0 | codeloom |
| where would a rate limiter go | 1 | 14008 | 3 | 0 | codeloom |
| which file defines the version | 1 | 14008 | 3 | 0 | codeloom |

## Rows we lose

- **what breaks if I change the parser**: bare policy was leaner (1 vs 1 calls, 0 vs 14008 bytes)
