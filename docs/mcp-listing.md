# codeloom — MCP server listing

## One-liner

Give your AI coding agent a map of the repo in one second. Single-file, zero-dependency, no daemon, 100% local — with agent memory that survives context compaction.

## What makes it different (the honest version)

Every code-intel MCP server does retrieval. codeloom also does **task orientation** and **agent memory**:

- **Working memory across compaction** — `--decide/--checkpoint/--resume/--working-state` record the agent's decision stream and replay it after a context wipe. No competitor ships this.
- **Structural pattern search** — `codeloom_pattern`: ast-grep-style `$F($$$ARGS)` matching over real ASTs, zero external binaries.
- **1 natural-language entry point** (`codeloom_ask`) routing deterministically to 82 tools — no tool-selection misfires.
- **50 tree-sitter grammars dispatched, 46 fixture-proven** — golden-file parity tests gate CI on every grammar.
- **Sub-second everything**: cold index <1s to first result, health screen 0.2s, Linux-kernel full graph (C engine) ~89–113s via the auto-built C core.
- **Zero dependencies**: one MIT-licensed Python file, stdlib only, auditable in one sitting. Accelerators (C/Rust) compile from committed source on demand — never downloaded.

## Install

```bash
pip install codeloom        # or just copy codeloom.py
```

MCP config (any client):

```json
{"command": "python3", "args": ["-m", "codeloom_mcp"]}
```

or point at the single file directly:

```json
{"command": "python3", "args": ["/path/to/codeloom-mcp.py"]}
```

## Tools (78)

Routers: `codeloom_ask` (natural language). Retrieval: search, embed-search,
grep-symbolic,
hybrid-search, get-symbol, read, snippet, grep, usages, similar. Task-shaped:
context-card, answer, why, pack, plan, ask. Graph: graph, cross, impact,
refactor, rename, precision, trace. Quality: health, risk, deadcode,
pattern. Memory: decide/reject/hypothesis/seen/checkpoint/resume/
working-state/lessons/supersede/query-memory/remember/loom.

## License

MIT
