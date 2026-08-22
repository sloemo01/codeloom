#!/usr/bin/env python3
"""Sealed retrieval benchmark: bare toolchain vs codeloom (no LLM required).

Measures the RETRIEVAL phase of agent work — the part that burns tokens before
any model even answers. Two toolchains on the SAME repo and SAME 10 questions:

  - bare: the realistic grep-and-read chain (rg locate -> read top file ->
          grep callers) — what every agent does today.
  - codeloom: one `--ask` call (cited answer with calibrated confidence).

Counts per question: tool CALLS and context BYTES returned (tokens = bytes/4
estimate, matching the static-replay convention). Retrieval correctness is
checked deterministically: did the retrieved context contain the file where
the answer lives? No LLM involved anywhere — fully reproducible offline.

This is the honest companion to compaction_recovery.py: that one measures
post-compaction restore; this one measures first-touch retrieval.

Usage:
    python3 benchmarks/live_sealed_run/harness.py --mode bare    --repo /tmp/bench-fastapi
    python3 benchmarks/live_sealed_run/harness.py --mode codeloom --repo /tmp/bench-fastapi
    python3 benchmarks/live_sealed_run/harness.py --mode all     --repo /tmp/bench-fastapi
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

try:
    import tiktoken
    ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(t):
        return len(ENC.encode(t))
except ImportError:
    def count_tokens(t):
        return len(t) // 4

CodeLoom = os.path.join(os.path.dirname(__file__), "..", "..", "codeloom.py")

# --- rg / grep selection -------------------------------------------------
# ripgrep may be absent (e.g. bare CI runners). Detect it once at startup;
# when missing, every `rg` invocation below is transparently translated to
# an equivalent `grep` invocation with the SAME output shape:
#   rg -l PAT -g '*.py' .          ->  grep -rl --include=*.py --exclude-dir=.git PAT .
#   rg -n 'RE' -g '*.py' .         ->  grep -rnE -I --include=*.py --exclude-dir=.git 'RE' .
# Both emit bare relative paths for -l and `path:line:text` for -n, which is
# all the harness consumes. Mapping notes:
#   * -g '*.py' (rg glob) == --include=*.py (grep); grep recurses into
#     .git by default, so --exclude-dir=.git keeps the file set equivalent.
#   * rg treats patterns as regex by default == grep -E.
#   * rg skips binary files by default == grep -I.
#   * rg -n line numbers == grep -n.
HAS_RG = shutil.which("rg") is not None


def rg(args, cwd, **kw):
    """Run an rg-style search, falling back to grep when ripgrep is absent.

    Accepts the exact argv shapes used by bare_retrieve() and returns the
    same subprocess.CompletedProcess shape (capture_output/text=True).
    """
    if HAS_RG:
        return subprocess.run(["rg"] + args, cwd=cwd, **kw)
    # Translate only the flag patterns this harness uses. Flags may appear
    # before or after the pattern (both `-l PAT` and `PAT -l` are accepted).
    args = list(args)
    mode_l = "-l" in args
    mode_n = "-n" in args
    args = [a for a in args if a not in ("-l", "-n")]
    if "-g" in args:
        i = args.index("-g")
        del args[i:i + 2]  # drop `-g '*.py'`; grep filters via --include below
    # Remaining positional args: [pattern, path] (path defaults to ".")
    pattern = args[0] if args else ""
    path = args[1] if len(args) > 1 else "."
    if mode_l and not mode_n:
        # grep -l takes the pattern as a literal string (BRE), not a regex
        return subprocess.run(
            ["grep", "-rl", "--include=*.py", "--exclude-dir=.git",
             "-F", pattern, path], cwd=cwd, **kw)
    if mode_n and not mode_l:
        return subprocess.run(
            ["grep", "-rnE", "-I", "--include=*.py", "--exclude-dir=.git",
             pattern, path], cwd=cwd, **kw)
    raise NotImplementedError(f"rg fallback: unsupported flag set: {args}")

# 10 fixed tasks on fastapi, with the file that holds the answer
TASKS = [
    ("Where is the File parameter class defined?", "fastapi/params.py"),
    ("Where is the HTTPException class defined?", "fastapi/exceptions.py"),
    ("Where is the FastAPI application class defined?", "fastapi/applications.py"),
    ("Which module defines the APIRouter class?", "fastapi/routing.py"),
    ("Where is the Depends function defined?", "fastapi/params.py"),
    ("Where are the HTTP status codes defined?", "fastapi/status.py"),
    ("What is the entry point of the fastapi command-line tool?", "fastapi/cli.py"),
    ("Where is the UploadFile class defined?", "fastapi/datastructures.py"),
    ("Where is the Request class defined?", "fastapi/requests.py"),
    ("Where is the JSONResponse class defined?", "fastapi/responses.py"),
]


STOPWORDS = {"Where", "Which", "What", "How", "Why", "Is", "Are", "The",
             "In", "On", "At", "A", "An", "HTTP", "Entry", "Point"}


def bare_retrieve(repo, question):
    """Realistic grep-and-read chain. Returns (calls, bytes, context)."""
    calls = 0
    total = 0
    anchor_m = re.search(r"\b([A-Z][A-Za-z0-9_]+)\b", question)
    anchor = anchor_m.group(1) if anchor_m else "class"
    if anchor in STOPWORDS:
        # re-grab: first capitalized identifier that is not a stopword
        for m in re.finditer(r"\b([A-Z][A-Za-z0-9_]+)\b", question):
            if m.group(1) not in STOPWORDS:
                anchor = m.group(1)
                break
    # 1. locate the anchor (rg with explicit python glob — portable;
    #    falls back to grep when ripgrep is unavailable)
    rc = rg(["-l", anchor, "-g", "*.py", "."], cwd=repo,
            capture_output=True, text=True)
    calls += 1
    files = [f for f in rc.stdout.splitlines() if f][:2]
    ctx = "\n".join(files)
    total += len(ctx.encode())
    # 2. read the top candidate file (the thing that costs tokens)
    for f in files[:1]:
        try:
            with open(os.path.join(repo, f), "r", encoding="utf-8",
                      errors="replace") as fh:
                content = fh.read()
            calls += 1
            total += len(content.encode())
            ctx += "\n---\n" + content[:4000]
        except OSError:
            pass
    # 3. grep for callers of the anchor
    rc = rg(["-n", rf"\b{anchor}\w*", "-g", "*.py", "."], cwd=repo,
            capture_output=True, text=True)
    calls += 1
    total += len(rc.stdout.encode())
    ctx += "\n---\n" + rc.stdout[:2000]
    return calls, total, ctx


def codeloom_retrieve(repo, question):
    """One codeloom --answer call (cited, calibrated). Returns (calls, bytes, ctx)."""
    rc = subprocess.run([sys.executable, CodeLoom, "--answer", question, "."],
                        cwd=repo, capture_output=True, text=True)
    return 1, len(rc.stdout.encode()), rc.stdout


def normalize(s):
    """fastapi/datastructures.py -> fastapi.datastructures (module notation)."""
    return s.replace("/", ".").replace(".py", "").lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["bare", "codeloom", "all"], required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--jsonl",
                    default=os.path.join(os.path.dirname(__file__), "run.jsonl"))
    args = ap.parse_args()

    modes = ["bare", "codeloom"] if args.mode == "all" else [args.mode]
    results = []
    for mode in modes:
        print(f"\n=== RETRIEVAL MODE: {mode} ===")
        for q, truth in TASKS:
            if mode == "bare":
                calls, nbytes, ctx = bare_retrieve(args.repo, q)
            else:
                calls, nbytes, ctx = codeloom_retrieve(args.repo, q)
            found = normalize(truth) in normalize(ctx)
            results.append({"mode": mode, "question": q, "truth": truth,
                            "found_answer_file": found, "calls": calls,
                            "bytes": nbytes, "tokens_est": count_tokens(ctx)})
            print(f"  {'HIT ' if found else 'MISS'} calls={calls} "
                  f"tok={count_tokens(ctx):>6}  {q[:52]}")

    with open(args.jsonl, "w") as fh:
        for r in results:
            fh.write(json.dumps(r) + "\n")

    print(f"\n===== SUMMARY =====")
    for mode in modes:
        rows = [r for r in results if r["mode"] == mode]
        found = sum(1 for r in rows if r["found_answer_file"])
        calls = sum(r["calls"] for r in rows)
        tok = sum(r["tokens_est"] for r in rows)
        print(f"{mode}: {found}/{len(rows)} found answer file, {calls} calls, "
              f"{tok} tokens (est)")


if __name__ == "__main__":
    main()
