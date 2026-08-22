# Competitive landscape — verified against sources

Every competitor cell below was checked against the project's own repo/README
this week (GitHub API fetches, dated), and code-review-graph was additionally
measured live (installed, graph built, MCP handshake counted) on 2026-08-22.
codeloom cells are command-verified in CI. Claims we could not verify are
marked. Last updated: 2026-08-22.

## The field

| Project | Stars | License | Impl | Source checked |
|---|---|---|---|---|
| **codeloom** | solo | **MIT** | Python + auto-built C/Rust | this repo |
| code-review-graph | **30,682** | MIT | Python (+daemon) | tirth8205/code-review-graph, measured live |
| claude-context | 12,427 | MIT | TypeScript | zilliztech/claude-context |
| repomix | 28,000 | MIT | TypeScript | yamadashy/repomix |
| jcodemunch-mcp | 2,596 | **Dual-use: free non-commercial; paid commercial** (jMunch LLC) | Python | jgravelle/jcodemunch-mcp |
| codegraph | **67,581** | MIT | C/Rust kernel | colbymchenry/codegraph |
| codebase-memory-mcp | **39,796** | MIT | pure C binary | DeusData/codebase-memory-mcp |
| repowise | 6,135 | AGPL-3.0 / commercial | Python | repowise-dev/repowise |
| codeseek | 762 | MIT | Rust | CodeBendKit/codeseek |
| code-context-engine | 397 | MIT | Python | elara-labs/code-context-engine |

## Capability matrix

| Capability | codeloom | crg | claude-context | jcodemunch | codegraph | codebase-memory | CCE | repowise |
|---|---|---|---|---|---|---|---|---|
| MCP tools | **79 + `codeloom_ask` NL router** | 30, no router (counted live) | many (code-search) | 90+, 6 routers | 1 + 7 unlisted | 15 | 22 | 10 task-shaped |
| Tree-sitter languages | **50 dispatched / 46 fixture-proven** | claims (not per-grammar proven) | — | 70+ claimed | 20 byte-verified | 158 vendored | — | n/a |
| Per-language CI proof | ✅ golden fixtures gate CI | not seen | — | not seen | ✅ byte-for-byte | tests badge, no per-grammar | not seen | n/a |
| Cold index | <1s first result; ~89–113s kernel full graph (C engine) | build 4s (fastapi) → 42MB graph; needs pip install first | after indexing | after indexing | ~100s | 3min claim | after indexing | "slowest indexer" |
| Freshness | reads-live; `--watch`→`--watch-merge` | watcher + daemon | — | watch modes | watcher default-on | daemon watchers | daemon | `watch` + hooks |
| Session memory across compaction | ✅ **decision ledger** (decide/checkpoint/resume/supersede/lessons) | ⚠️ markdown journal of Q&A (no compaction mention) | memsearch plugin (separate) | ❌ | ✅ session memory + learning | ❌ | ⚠️ `record_decision` MCP calls | ADRs only |
| Semantic search | ✅ zero-dep subword hash (ggml opt-in) | ❌ requires `[embeddings]` extra (~2GB) or cloud key | ✅ (Zilliz) | opt-in | ❌ | ✅ bundled | ❌ requires ONNX embeddings | vector hybrid |
| One-call cited answer | ✅ calibrated confidence | ✅ search FTS JSON | ✅ | ✅ calibrated | ✅ | ✅ | ✅ | ✅ quality tiers |
| Install weight | **one file, stdlib-only** | pip + 78 pkgs + daemon + TOML config | npm | pip + index store | bundled binary | single binary | pip + ONNX + server | pip + dashboard |
| PR review | ✅ deterministic zero-LLM + risk labels | ✅ risk labels + GitHub Action | — | ✅ | blast radius | — | — | ✅ defect-validated |

## Honest position

Where codeloom leads:
- **Zero-dependency single file under MIT** — none of the eight match all three.
- **Compaction-survival decision ledger** — measured (95.4% fewer tokens to recover, `benchmarks/compaction_recovery.py`); code-review-graph has zero mentions of compaction/session/resume in its README; CCE requires agent-called MCP tools + running server.
- **78→79 tools behind 1 NL router** — crg's 30 tools have no router (adherence problem the CodeRLM thread hit); v0.78 adds `verify_edit` + `blindspot` and `loom://resources`.
- **Fixture-gated grammar claims** — 46 languages golden-file parity in CI.
- **Zero-dep offline semantic search** — crg needs a 2GB model extra or cloud key; CCE needs ONNX.
- **Setup-to-first-answer** — measured: 0.13s warm vs crg 41s pip + 4s build + daemon.
- **Local savings ledger, all-local, no telemetry** — `--savings-report` (v0.78) compiles your own `--session` log into per-repo token/cost receipts; the numbers live in your repo, not in our README. CCE's sealed 94% ledger is better evidence discipline — ours is local-first and independently auditable.
- **Post-edit loop closure** — `--verify-edit` (v0.78) re-derives a GO/CHECK/STOP verdict *after* an edit; jcodemunch's preflight (the strongest in the field) stops at *before*. `--blindspot` (v0.78) adds the unread-file warning — the files your edit breaks that you never read. That's the loop-closure differentiator: preflight → edit → verify → commit.

Where competitors lead (stated plainly):
- **crg**: community scale, 5 translated READMEs, eval runner, multi-repo registry, incremental rebuilds.
- **CCE**: sealed 94% savings ledger (better evidence discipline; ours is labeled static/live honestly).
- **codegraph/codebase-memory**: community scale, vendored grammars, arXiv evals.
- **claude-context**: Zilliz backing, memsearch tie-in.
- **repomix**: 28k★ packing ecosystem (complementary — their #1620 asks for a memory layer = people asking repomix to become codeloom).

Claims we retired after checking sources:
- ~~"Nobody else ships pattern matching"~~ — jcodemunch's `search_ast` does (presets + DSL; ours adds metavariable capture).
- ~~"Nobody else survives compaction"~~ — codegraph does (session memory + learning).
- ~~"Their licenses are unknown"~~ — verified: dual-use / MIT / MIT / MIT / MIT / AGPL.
