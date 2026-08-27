# codeloom security

How codeloom keeps you safe, and what to watch for.

## Design: auditable by construction

codeloom is a **single stdlib Python file** you can read end-to-end before
running. There are no hidden dependencies, no network calls
in the core, no telemetry, no background daemon. What you see is what runs
(the one opt-in network touch: `--eval` may clone a public benchmark repo
into /tmp when no local `--root` is given).

## Verify a download

Because codeloom is a single file you download, verify it's official:

```bash
codeloom --verify codeloom.py
# sha256: <checksum>
```

Every release ships the SHA-256 of `codeloom.py` in the release notes. Compare
the output to the release checksum to confirm the file wasn't tampered with.

## What codeloom does NOT do

- **No network calls in the core** — reading a repo, mapping, graphs, memory,
  and retrieval are all local file + git reads. The core never phones home.
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

- **The Memory OS / persistence layer (`--memory-add`, `--decide`, `--reject`,
  `--lesson`, `--adr`, `--goal`, `--hypothesis`, `--supersede`, `--remember`,
  `--checkpoint`, `--seen`, `--session`)** — this is the **input path of
  Memory OS**, not a bolt-on: `--memory <symbol>` graph retrieval returns only
  what these writers recorded. Use Memory OS without writing → it has nothing
  to retrieve. `--remember` (legacy) writes markdown only; the typed writers
  dual-write markdown + `.codeloom-memory/memory.jsonl` (plain JSONL: type,
  id, title, body, affected_symbols, importance, confidence, tier, timestamp),
  which rotates losslessly into `.codeloom-memory/archive/memory-<date>.jsonl`
  at the size cap. All local, all readable/deleteable, never leaves your
  machine. The zero-effort way to feed it is `scripts/memory_extract.py`,
  which mines your `git log` (read-only) into typed memories automatically —
  so the layer stays useful even if you never write memory by hand.
- **`scripts/memory_extract.py`** (v0.79) — runs `git log` on your repo
  (read-only) and invokes the core via subprocess argv (never a shell) to
  create typed memories from commit messages. Writes its dedupe state to
  `.codeloom-memory/extract-state.json`. It never modifies git history; it
  only *reads* it and *appends* memory entries.
- **`--install-hook` (v0.78)** — writes `.git/hooks/pre-commit` that runs
  `scripts/pre-commit-hook.sh` on staged files. The hook is **warn-only** by
  default (prints a risk verdict, always exits 0 unless you set `STRICT=1` in
  the hook file). It calls `git diff --cached`, `git write-tree`, and
  `codeloom.py --risk` — reads the staged index, never modifies it.
- **`--verify-edit` / `--risk` (v0.78)** — read-only git queries
  (`git status --porcelain`, `git ls-tree HEAD`, `git rev-parse`) to re-parse
  changed files and score change risk. No writes, no execution of your code.
- **`--eval KIND` / `benchmarks/eval_runner.py`** — the benchmark suite. When
  you run it **without** a local `--root`, it `git clone`s a public benchmark
  repo from GitHub into `/tmp` (the only network-touching behavior in the
  whole tool, and strictly opt-in — the core never does this). With a local
  `--root` it stays fully offline.
- **`--index`** — writes `.codeloom-index.json` (JSON-only; no binary cache is
  ever written or loaded). Repo-supplied index files are treated as untrusted:
  entries pointing outside the repo root are dropped (checked through
  `realpath`, so in-repo symlinks pointing outside are dropped too), the lazy
  `.codeloom-index.lazy` dbm store is containment-checked on every read, and
  legacy `.bin` files are ignored with a warning rather than unmarshalled.
- **Symlinks** — the file walker follows symlinks only when they resolve
  inside the repo root; a symlink pointing outside (e.g. `~/.ssh`) is skipped,
  so a hostile repo can't smuggle outside content into the index.
- **`--graph-html`** — module names (derived from file names on disk) are
  escaped before embedding into the generated page's inline JavaScript, so a
  hostile filename can't execute script when the HTML is opened. Note: the
  page loads the `force-graph` library from unpkg.com when opened — the only
  network touch in the generated artifact.

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
