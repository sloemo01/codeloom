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

## `--trace` executes code (opt-in)

`codeloom --trace CMD` runs a command under `sys.settrace` to record runtime
call edges. This **executes code** and may have side effects (network, files).
It requires `--force` to acknowledge, and you should run it in an isolated
sandbox/container/CI job.

## `--install-grammars` installs packages (opt-in)

`codeloom --install-grammars --yes` runs `pip install` for tree-sitter grammars.
This is opt-in and only runs when you pass `--yes`. The core works without it.

## The trust model

codeloom's security posture is **auditability**: one readable file, no hidden
behavior, no network, no daemon. You can verify every byte that runs. In
contrast, pip packages with indexes and daemons (jcodemunch, semble) mean more
moving parts to audit and more surface area for a supply-chain or dependency
risk to hide.

## Reporting a vulnerability

Open an issue on https://github.com/sloemo01/codeloom with the details. Because
the tool is one file, a fix is a single-file update you can verify with
`codeloom --verify`.
