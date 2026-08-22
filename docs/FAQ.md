# FAQ — how codeloom sits in the field

The questions people actually ask when they hear "code intelligence for
agents" — answered plainly, with the honest tradeoffs stated. Measured
numbers link to [`benchmarks/README.md`](../benchmarks/README.md).

## Isn't this just an LSP with extra steps?

No — and the difference matters exactly where LSPs are weakest.

- **One map, all languages.** An LSP is per-language and per-process: a
  multi-language repo means N language servers, N caches, N protocols to
  bridge. codeloom builds one cross-language structural model of the whole
  repo in one pass (50 tree-sitter languages dispatched, 46 fixture-proven).
- **No editor, no daemon.** LSPs are long-running services that must be
  installed, started, and kept alive. codeloom is one stdlib file — it
  exists only when you call it. The agent's shell is the only runtime.
- **Task-shaped, not symbol-shaped.** LSP answers "what references this
  symbol?" — the *retrieval* question. codeloom answers "what code actually
  runs for this task?" (`--pack` embeds the code, the call path, and the
  blast radius) — the *execution* question. That's the difference between a
  dictionary and a briefing.

LSP stays more precise per-symbol (that's their job). codeloom is the
cheap, zero-install, cross-language layer on top.

## Isn't this just RAG / embeddings?

RAG answers "what text is *similar* to my query" — semantic overlap, no
structure. codeloom's default path is the opposite: **structural edges
parsed from the AST** (imports, calls, inheritance), not similarity scores.
It knows `login()` calls `validate()` because the AST says so, not because
the words look alike.

The semantic layer is optional and honest: `--embed-search` runs on a
zero-dep subword hash (offline, typo-tolerant), with ggml neural embeddings
as an opt-in upgrade. RAG tools (code-context-engine, codeseek) *require* an
embedding model — a setup wizard or an ONNX install — before first use, and
they still can't tell you what breaks if you edit a symbol.

## Why not repomix / gitingest?

They're packers, not intelligence: dump the repo into one file, paste it,
done. The output is a snapshot — opaque, static, and stale the moment you
edit.

- codeloom is a **live, queryable map**: `--watch` → `--watch-merge` keeps
  it fresh, and `--answer`/`--pack`/`--impact` answer questions instead of
  dumping the whole tree.
- Their own community is asking for what we already ship: repomix issue
  #1620 requests "a portable AI memory layer for packed repos" — that's
  codeloom's `--decide`/`--checkpoint`/`--resume` ledger.

Packers are complementary: pack once for a single-shot context dump, and
use codeloom for the 99% of sessions that aren't one-shot.

## Why not code-review-graph?

Honest answer: they're the biggest in this niche (30.6k★) and they do real
work — an incremental graph, a watch daemon, a GitHub Action, 5 translated
READMEs. We measured them live on the same repo. Where we win:

- **30 tools, no router.** Their agent must pick from 30 MCP tools. Ours
  has 78 behind *one* deterministic NL router (`codeloom_ask`) — no
  tool-selection misfires, which is the "it loves to just grep" adherence
  problem every tree-sitter tool hits.
- **Compaction survival is a feature, not an afterthought.** Their "memory
  loop" is a markdown journal of Q&A — it remembers *answers*, not
  *decisions*. codeloom's ledger (`--decide` → `--checkpoint` → `--resume`)
  restores both the map *and* what the agent tried/decided/rejected, in
  **2 calls / ~985 tokens measured** (bare re-derive: 33 calls / ~21.6k
  tokens — [`benchmarks/README.md`](../benchmarks/README.md)).
- **Zero install.** One stdlib file vs pip (75 packages) + graph build +
  daemon (`crg-daemon` with health checks + TOML config) + an
  embeddings extra (~2GB) for semantic search. Setup→first answer:
  0.13s warm for us, 41s+pip+build for them.
- **Symbol retrieval: 24–36× fewer tokens** on the same fastapi symbols
  (13–20 vs 428–485, measured).

Where they lead, stated plainly: community scale, an eval runner, a
multi-repo registry, incremental rebuilds, translated docs. We're not
claiming to replace them — we're claiming the axes above, with the numbers
to back it.

## What does "survives compaction" mean?

Coding agents periodically compact their context window — old tool output
gets summarized away, and with it the agent's model of the codebase.
Standard tools can't help: their index lives in a server, not in the
agent's context.

codeloom's working-memory model is **file-based and deterministic**:
`--decide` appends a decision to `DECISIONS.md`; `--checkpoint` snapshots
the working state; `--resume` restores *both* the structural map and the
decision ledger in one call. No server, no embedding, no prompt engineering
— the file *is* the memory. That's the same reason `AGENTS.md` works; we
make the agent's memory survivable the same way: as plain text it can
re-read.

## Is there a catch?

Yes, honestly:

- Python gets the deepest analysis (stdlib `ast`); other languages get
  tree-sitter outlines + regex fallbacks. Not all 50 languages are equal.
- Health/risk are structural heuristics — not defect-validated against a
  labeled corpus (repowise's moat; we don't claim it).
- The sealed benchmarks measure *retrieval under a scripted policy* — not
  an autonomous agent loop. Loss rows are published.
- Neural embeddings are opt-in (ggml). The zero-dep hash is good, not
  great — it catches typos, it won't catch synonym drift.

## Where does the token math come from?

[`benchmarks/README.md`](../benchmarks/README.md) — every number has a
reproduction command. Symbol retrieval, compaction recovery, and the sealed
retrieval run are all reproducible in under 5 minutes on a laptop.
