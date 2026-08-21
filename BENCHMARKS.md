# codeloom benchmarks

Measured numbers only — no fabricated results. Every figure below was produced
by actually running the tool.

## Linux kernel (measured this session)

Cloned `torvalds/linux` (shallow, ~2 GB) and ran the Rust core (`codeloom_rs`):

| Metric | Result |
|---|---|
| Code files | **67,306** |
| Symbols extracted | **5,663,390** |
| Wall time (walk + map) | **~11–13 s** |

The kernel clone was a benchmark artifact and removed after measuring. Reproduce
with:

```bash
git clone --depth 1 https://github.com/torvalds/linux.git
./codeloom_rs map linux      # ~12s, 67,306 files, 5.6M symbols
./codeloom_rs files linux | wc -l
```

## Semantic search (subword-hash, zero-dep)

| Query | Top match | Similarity |
|---|---|---|
| `engin` (typo) | `Engine` | 0.72 |
| `getengine` (camel split) | `Engine` | 0.70 |
| `retrey` (typo) | `retry` | 0.43 |

## Multi-repo / cross-service

`codeloom_rs cross <a> <b>` detects a symbol defined in repo A called from repo B:

```
xrepo_b -> verify_token (defined in xrepo_a)
xrepo_b -> get_user (defined in xrepo_a)
```

## Honest scope

- The kernel benchmark is a **walk + symbol extraction**, not a full 100-language
  semantic deep-index (that's the ongoing tree-sitter work — 16 languages today).
- The `--embed-search` numbers use the zero-dependency subword hash. When a local
  ggml model is present (`CODELOOM_GGML_BIN`/`CODELOOM_GGML_MODEL`), it upgrades to
  real neural embeddings automatically.
