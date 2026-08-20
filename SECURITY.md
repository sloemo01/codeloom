# codeloom security

How codeloom keeps you safe, and what to watch for.

## Design: auditable by construction

codeloom is a **single stdlib Python file** you can read end-to-end before
running. There are no hidden dependencies, no network calls, no telemetry, no
background daemon. What you see is what runs.

## Verify a download

Because codeloom is a single file you download, verify it's official:

```bash
codeloom --verify codeloom.py
# sha256: <checksum>
```

Every release ships the SHA-256 of `codeloom.py` in the release notes. Compare
the output to the release checksum to confirm the file wasn't tampered with.

## What codeloom does NOT do

- **No network calls** — codeloom reads your files locally and exits. It never
  phones home.
- **No telemetry** — nothing is collected or sent.
- **No daemon** — no background process, no idle resource use.
- **No hidden deps** — stdlib only. No `pip install` required for the core.

## Optional surface (all opt-in — the core works without any of it)

codeloom stays zero-dep by default, but each opt-in enrichment you add expands
the trust surface. Know what each one does:

- **`--engine c` / `--watch-core` / `--serve`** — build `codeloom_core.c`
  (once, `cc -O3`) for a faster scan + native file watcher + resident symbol
  server. You compile the C core from the committed source yourself, so you
  can audit exactly what runs.
- **`--install-grammars --yes` / `--auto-grammars`** — runs `pip install` for
  tree-sitter grammars. `--install-grammars` only runs when you pass `--yes`;
  `--auto-grammars` only installs when `CODELOOM_AUTO_INSTALL_GRAMMARS=1`.
  Installing a pip package from PyPI is standard package trust, not a codeloom
  runtime behavior.
- **Embedding API (`CODELOOM_EMBED_BASE_URL`/`CODELOOM_EMBED_API_KEY`)** —
  sends symbol names to your configured endpoint for semantic scoring. Only
  when you set the env vars. Your key stays in your env, never in codeloom.
- **`--lsp-symbol`** — starts an LSP server (pyright/clangd/...) as a
  subprocess to resolve a symbol's real definition. Only when you run it;
  falls back to static parsing otherwise.
- **`--trace CMD`** — runs a command under `sys.settrace`. **Executes code**,
  may have side effects; requires `--force`; run in a sandbox/CI job.
- **`--routes` / `--channels`** — static, read-only analysis of your files to
  extract HTTP routes and pub-sub channels. No code executes, nothing is sent.
- **`--export FILE`** — writes a **local** graph snapshot (symbols + edges +
  routes + channels) to the file you name. Same pattern as `--write MAP.md`;
  stays on your machine, never leaves it.

## The trust model

codeloom's security posture is **auditability**: one readable file, no hidden
behavior, no network, no daemon. You can verify every byte that runs. In
contrast, pip packages with indexes and daemons (jcodemunch, codegraph,
codebase-memory-mcp) mean more moving parts to audit and more surface area for
a supply-chain or dependency risk to hide.

## Reporting a vulnerability

Open an issue on https://github.com/sloemo01/codeloom with the details. Because
the tool is one file, a fix is a single-file update you can verify with
`codeloom --verify`.
