# Competitive landscape — verified against sources

Every competitor cell below was checked against the project's own repo/README
this week (GitHub API fetches, dated). codeloom cells are command-verified in
CI. Claims we could not verify are marked. Last updated: 2026-08-21.

## The field

| Project | Stars | License | Impl | Source checked |
|---|---|---|---|---|
| **codeloom** | solo | **MIT** | Python + auto-built C/Rust | this repo |
| jcodemunch-mcp | 2,596 | **Dual-use: free non-commercial; paid commercial** (jMunch LLC) | Python | jgravelle/jcodemunch-mcp |
| codegraph | **67,581** | MIT | C/Rust kernel | colbymchenry/codegraph |
| codebase-memory-mcp | **39,796** | MIT | pure C binary | DeusData/codebase-memory-mcp |
| repowise | 6,135 | AGPL-3.0 / commercial | Python | repowise-dev/repowise |

## Capability matrix

| Capability | codeloom | jcodemunch | codegraph | codebase-memory | repowise |
|---|---|---|---|---|---|
| MCP tools | **77 + `codeloom_ask` NL router** | 90+, 6 routers (`route`/`menu`) | **1 listed** (`codegraph_explore`) + 7 unlisted | **15** | **10 task-shaped** |
| Tree-sitter languages | **50 dispatched / 46 fixture-proven** | 70+ claimed (84-row matrix) | 20, byte-for-byte graph verification | **158 vendored** | n/a (Python-focused) |
| Per-language CI proof | ✅ golden fixtures gate CI | not seen | ✅ byte-for-byte vs reference engine | tests badge (6,768 passing), no per-grammar matrix published | n/a |
| Cold index | <1s first result; ~91s kernel full-graph | after indexing | ~100s Swift-compiler class | Linux kernel 3min claim | "slowest indexer" (own words) |
| Freshness | reads-live; native watcher → merge pipe; connect-time catch-up via persistent index | watch modes, agent hooks, VS Code ext | ✅ watcher default-on + reconnect catch-up | daemon-owned watchers | `repowise watch` + post-commit hook/webhook/polling |
| Session memory across compaction | ✅ decide/checkpoint/resume/lessons/supersede/working-state | ❌ | ✅ session memory + learning system + SKILL.md distillation — publishes resume benchmark (99.5%, 210×) | ❌ (daemon coordinates sessions, doesn't remember content) | ADRs only |
| Structural pattern search | ✅ `$VAR`/`$$$REST` metavariable capture over stdlib ASTs | ✅ `search_ast`: 10 anti-pattern presets + mini-DSL (`call:*.unwrap`), 70+ langs | ❌ not seen | ❌ not seen | ❌ |
| Change-risk on PRs | ✅ deterministic 0–100 + drivers; PR bot posts task brief | ✅ `get_pr_risk_profile` composite 0–1 + edit/delete preflight | blast-radius inline | `detect_changes` risk classification | ✅ defect-validated percentiles (AUC 0.737) |
| Health/dead-code | ✅ health screen 0.01–0.2s + deadcode | ✅ `find_dead_code`, hotspots, complexity | dead-code inline | ✅ dead code, hotspots, Louvain clusters | ✅ 49 detectors, corpus-validated |
| Semantic search offline | ✅ subword-hash zero-dep + opt-in ggml batched | opt-in embeddings ("zero mandatory deps") | ❌ not seen | ✅ bundled Nomic embed (40K tokens int8) + 11-signal scoring | vector hybrid |
| One-call cited answer | ✅ calibrated confidence | ✅ calibrated confidence + coverage contracts | ✅ explore returns verbatim source + call paths | ✅ | ✅ quality tiers |
| Cross-service linking | ✅ multi-repo `cross` | cross-repo contracts (`get_group_contracts`) | per-project `projectPath` | ✅ HTTP/gRPC/GraphQL routes + K8s/IaC nodes | workspaces |
| Runtime call edges | ✅ `--trace` | runtime traffic evidence | dynamic-dispatch hops (React/interface→impl) | runtime hits | ❌ |
| Token-efficiency evidence | static replay 19/1/0, loss-row published; live-agent run pending | 50-iter A/B: 80% vs 72% success, 15–25% tool-layer savings; user issue #142 reports overhead vs native tools | sealed harness, CLI blocked both arms, −44% cost; honest residual-context disclosure (+80% resident) | arXiv preprint: 31 repos, 83% quality, 10× fewer tokens, 2.1× fewer calls | sealed split, live models: −31.6% output tokens (n=43, p<0.0001); 35.6× commit-load |
| Install weight | one file, stdlib-only, zero deps | pip + tree-sitter + index store | bundled runtime binary | single static binary (VirusTotal-scanned releases) | pip + dashboard |

## Honest position

Where codeloom leads:
- **Zero-dependency single file under MIT** — none of the five matches all three.
- **Tool-count discipline with the broadest surface**: 77 tools but effective agent surface of 1 via natural-language routing (codegraph shares the philosophy with 8 total).
- **Fixture-gated grammar claims**: 46 languages with golden-file parity proofs in CI — a quality guarantee nobody else publishes per language.
- **Fastest health/risk tier**: sub-second structural screens; and the only PR bot posting task *briefs* (risk-score comment upgrade pending).
- **Memory depth**: checkpoint/resume/supersede/lessons is richer than codegraph's session memory (theirs has learning/reflection we lack; ours has decision-ledger semantics theirs lacks).

Where competitors lead (stated plainly):
- **jcodemunch**: breadth of safety preflight (edit/delete-safe, terminal stop-rules), SCIP compiler-verified references, secret redaction, 70+ languages.
- **codegraph**: scale of community (67.6k★), byte-for-byte graph verification culture, watcher default-on, honest residual-context research.
- **codebase-memory**: 158 vendored grammars, bundled embeddings, Cypher queries, IaC/K8s indexing, arXiv-published evals, security process (Scorecard + VirusTotal).
- **repowise**: defect-validated risk (labeled corpus, ROC AUC 0.737), sealed live-model token savings, generated docs wiki.

Claims we retired after checking sources:
- ~~"Nobody else ships pattern matching"~~ — jcodemunch's `search_ast` does (presets + DSL; ours adds metavariable capture).
- ~~"Nobody else survives compaction"~~ — codegraph does (session memory + learning).
- ~~"Their licenses are unknown"~~ — now verified: dual-use commercial / MIT / MIT / AGPL.
