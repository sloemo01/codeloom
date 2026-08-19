# codemap benchmarks

Honest, reproducible numbers comparing codemap to the heavyweight competitors
(semble, codebase-memory-mcp, jcodemunch-mcp). The point is not "codemap is
faster at everything" — it's that codemap wins decisively on the axis that
matters most for everyday agent use: **time to first structural context, with
zero setup.**

## Methodology

- **Repo:** `browser-use/browser-use` (468 files, ~14MB) — a real production codebase.
- **codemap:** `python3 codemap.py <flag> .` from the repo root, cold start, no index.
- **Competitors:** their own published claims (indexing time, setup) from their READMEs.
- All timings are wall-clock on a MacBook (Apple Silicon).

## Results

| Operation | codemap | semble | codebase-memory-mcp |
|---|---|---|---|
| **Setup** | none (copy one file) | `pip install` + deps | native exe + index |
| **First structural context** | **0.8s** | minutes (index) | minutes (index) |
| **Import graph** | 0.8s (385 modules, 1126 edges) | after index | after index |
| **Call graph** | 0.8s (multi-language) | after index | after index |
| **Git-aware `--diff`** | 0.1s | n/a | n/a |
| **Task relevance (`--task`)** | 0.8s | n/a | n/a |
| **Change impact (`--impact`)** | 0.8s | n/a | n/a |
| **Freshness** | always (reads live) | stale between re-index | stale between re-index |

## The honest caveat

codemap is **not** a replacement for the heavy tools. It does not do code
*search* (semble's core job), and its regex call graph is less precise than
tree-sitter (codebase-memory-mcp's 158-language analysis). codemap wins on
**speed, zero-setup, freshness, and task-awareness** — the 80% case. For deep
search or production-scale indexing, the competitors are genuinely better.

## Reproduce

```bash
# clone the test repo
git clone --depth 1 https://github.com/browser-use/browser-use.git /tmp/bu
cd /tmp/bu

# time codemap operations
time python3 /path/to/codemap.py --graph .
time python3 /path/to/codemap.py --task "browser automation click" .
time python3 /path/to/codemap.py --impact browser_use/agent/service .
```
