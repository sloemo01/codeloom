# Agent before/after trace

How a coding agent completes a realistic task *without* codeloom vs. *with* it.
Illustrative but grounded in the real commands below.

## Task
"Add rate limiting to the login endpoint, using the existing retry utility."

## Without codeloom (or a codebase-memory-style tool)

1. Read `src/main.py` (full file) — context bloat, no idea where login lives
2. Grep for "login" — finds the route, no idea what it touches
3. Read `auth/login.py` (full file) — still can't see the retry dependency
4. Re-grep "retry" — **rediscovery**, wasted turns
5. Finally read `utils/retry.py` — the utility that already exists
6. Agent nearly re-implements retry (it forgot it exists)

**Cost:** 5+ full-file reads, ~2 wasted greps, context bloat, risk of re-doing work.

## With codeloom

```bash
codeloom --pack "rate limit login" .     # ONE call: reading order + exact code
```

Returns a **single-shot context packet** (2–4k tokens):
- Reading order: `auth/login.py`, `utils/retry.py` (on the call path)
- **The relevant code embedded** (not names — the actual source)
- Impact: what breaks if you change each
- Call path: `login -> retry`

```bash
codeloom --embed-search "rate limiter" . # fuzzy: finds the retry utility
codeloom --impact utils/retry.py          # what calls it
```

**Result:** 1 read instead of 5, zero wasted greps, the retry utility is found
instantly, and the agent remembers it (working-memory journal: `--decide`).

## The honest difference

codeloom doesn't "understand" the code more deeply — it **serves the exact,
minimal context the agent needs next** and **never forgets what it already
learned** (`--working-state` after any compaction). That's fewer tokens, fewer
loops, fewer failures — the metric that actually decides adoption.
