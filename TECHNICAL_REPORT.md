# codeloom — technical report

An honest engineering write-up of codeloom's architecture and the decisions
behind it. This is not a peer-reviewed paper; it is a reproducible account of
how the tool works, what was measured, and what is deliberately not claimed.

## 1. Thesis

Codeloom optimizes the metric that decides agent adoption: **how efficiently an
agent turns a task into a correct change.** Most code-intelligence tools compete
on graph depth (nodes and edges). Codeloom competes on **agent cognition** —
the minimal high-signal context packet and the persistence of the agent's own
work across context compaction.

## 2. Architecture

Three scanning engines, one interface:

| Engine | Role | Dependency cost |
|---|---|---|
| Pure-Python (default) | zero-dep walk + symbol + call graph | none |
| C core (`--engine c`) | compile-once accelerator, auto-builds | `cc` |
| Rust core (`--engine rust`) | multi-threaded, auto-builds | `rustc` |
| `codeloom_rs` | full standalone Rust CLI (map/search/read/calls/cross) | `rustc` |
| `engine_rs/` | real tree-sitter AST, 16 languages | `cargo` (optional) |

The MCP layer (`codeloom-mcp.py`, 71 tools) wraps these behind a **single
entry point** (`codeloom_ask`) that routes deterministically — no model picking
among 71 tools.

## 3. The working-memory model

An agent's repo knowledge survives compaction **only if written to disk**.
Codeloom provides the write surfaces:

- `--decide` / `--reject` / `--hypothesis` — record what the agent chose/abandoned
- `--mark-seen` — record what it already explored (hot set)
- `--checkpoint` — snapshot in-progress work (uncommitted diff + note)
- `--working-state` — the layered packet that reconstructs goal + decisions +
  actions + open items + hot set after any wipe

This is the honest claim: codeloom stores the *actions and decisions*, not the
model's internal reasoning (which cannot be recorded).

## 4. Measured results (this session)

- **Linux kernel**: 67,306 files walked + 5,663,390 symbols extracted in
  **~11–13s** (real clone, real run; artifact removed after).
- **Semantic search**: subword-hash catches typos (`engin` → `Engine`, sim 0.72)
  with zero deps; upgrades to neural via local ggml when configured.
- **Cross-service**: `codeloom_rs cross` detects `xrepo_b -> verify_token`
  (defined in `xrepo_a`).

## 5. Honest limitations

- 16 tree-sitter languages today (not 100+; the rest use the fast regex walker).
- Neural embeddings require a user-supplied local ggml model (infrastructure is
  wired; a bundled model would break the zero-dep single file).
- The kernel benchmark is walk + symbol extraction, not a full semantic deep-index.
- No claim to "understand" code better than codebase-memory; the win is agent
  context efficiency and memory, not raw graph depth.

## 6. Reproducibility

All commands, numbers, and file artifacts are in the repo (`BENCHMARKS.md`,
`AGENT_TRACE.md`). No credential, API key, or secret is used or stored.
