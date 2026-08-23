#!/usr/bin/env python3
"""Compaction-recovery benchmark: the number nobody else can publish.

Simulates what happens AFTER a context compaction mid-task, and measures how
many calls + tokens each path needs to reach "ready to continue working":

Path A (bare): the agent re-derives context from scratch the way every coding
agent actually does — ripgrep for the anchor symbol, read the candidate file,
grep for callers, read the second file. Scripted from the same task questions
the static-replay bench uses.

Path B (codeloom): `--resume` restores BOTH the structural model and the
decision ledger (decisions recorded with `--decide` / `--checkpoint`) in one
call. `--query-memory` recalls the decision in one more call.

Honest framing: this is a scripted policy simulation, not a live-agent run —
same caveat as bench/RESULTS.md. The bare path is the *realistic* grep-and-read
sequence, and it's what agents do today (validated by the HN/community pain
quotes: "it re-reads the whole codebase after every compaction").

Usage:
    python3 benchmarks/compaction_recovery.py --repo /tmp/bench-fastapi
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import tiktoken
    ENC = tiktoken.get_encoding("cl100k_base")
    def count_tokens(t):
        return len(ENC.encode(t))
except ImportError:
    def count_tokens(t):
        return len(t) // 4

CodeLoom = os.path.join(os.path.dirname(__file__), "..", "codeloom.py")

QUESTIONS = [
    "where is the retry logic",
    "what calls the main entry point",
    "how does the engine initialize",
    "where are config values read",
    "what breaks if I change the parser",
    "which module handles authentication",
    "where is error handling centralized",
    "who uses the cache helper",
    "where do imports of utils converge",
    "what is the data model",
]

ANCHORS = ["retry", "main", "init", "config", "parser", "auth", "error",
           "cache", "utils", "model"]


# --------------------------------------------------------------------------- #
# Path A — bare re-derive (the realistic grep-and-read chain)
# --------------------------------------------------------------------------- #

def bare_recover(repo, anchor):
    """Scripted grep-and-read: what an agent does after compaction to re-learn
    'where is X'. Returns (calls, bytes)."""
    calls = 0
    bytes_read = 0
    # 1. locate the anchor (ripgrep)
    rc = subprocess.run(["rg", "-l", "-i", anchor, "--type", "py", "."],
                        cwd=repo, capture_output=True, text=True)
    calls += 1
    files = [f for f in rc.stdout.splitlines() if f and ".py" in f][:3]
    if not files:
        return calls, bytes_read
    # 2. read the top candidate file
    for f in files[:2]:
        try:
            with open(os.path.join(repo, f), "r", encoding="utf-8",
                      errors="replace") as fh:
                content = fh.read()
            bytes_read += len(content)
            calls += 1
        except OSError:
            continue
    # 3. grep for callers of the anchor in the read content
    if any(re.search(rf"\b{anchor}\w*", line) for line in
           open(os.path.join(repo, files[0]), errors="replace")):
        rc = subprocess.run(["rg", "-n", rf"\b{anchor}\w*", "--type", "py",
                             "."], cwd=repo, capture_output=True, text=True)
        calls += 1
        bytes_read += len(rc.stdout)
    return calls, bytes_read


# --------------------------------------------------------------------------- #
# Path B — codeloom resume
# --------------------------------------------------------------------------- #

def codeloom_resume(repo):
    """--resume: restores structural map + decision ledger in one call."""
    rc = subprocess.run([sys.executable, CodeLoom, "--resume"],
                        cwd=repo, capture_output=True, text=True)
    return 1, len(rc.stdout.encode("utf-8"))


def codeloom_query_memory(repo, query):
    """--query-memory: recall the recorded decision in one call."""
    rc = subprocess.run([sys.executable, CodeLoom, "--query-memory", query],
                        cwd=repo, capture_output=True, text=True)
    return 1, len(rc.stdout.encode("utf-8"))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)

    # Work on a scratch clone: --decide/--checkpoint write into the repo's
    # .codeloom-memory, and --resume re-reads it — seeding the TARGET repo
    # would (a) mutate the user's checkout and (b) make repeated runs drift
    # (each run appends ledger entries, so resume bytes grow run-over-run).
    scratch = tempfile.mkdtemp(prefix="codeloom-compaction-")
    try:
        subprocess.run(["git", "clone", "-q", "--local", repo, scratch],
                       check=True)
        _run(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run(repo: str) -> None:
    # seed the decision ledger exactly like a real mid-task checkpoint
    subprocess.run([sys.executable, CodeLoom, "--decide",
                    "use retry(3) not retry(inf) for the login flow"],
                   cwd=repo, capture_output=True, text=True)
    subprocess.run([sys.executable, CodeLoom, "--checkpoint",
                    "--task", "fix the login bug"],
                   cwd=repo, capture_output=True, text=True)

    bare_calls, bare_bytes = 0, 0
    cl_calls, cl_bytes = 0, 0

    print("=" * 66)
    print("Compaction-recovery benchmark (scripted policy simulation)")
    print(f"repo: {repo}")
    print("=" * 66)
    print(f"\n{'question':<42} {'bare calls':>10} {'bare KB':>8} "
          f"{'resume calls':>12}")
    print("-" * 80)
    for q, anchor in zip(QUESTIONS, ANCHORS):
        bc, bb = bare_recover(repo, anchor)
        bare_calls += bc
        bare_bytes += bb
        print(f"{q:<42} {bc:>10} {bb/1024:>7.1f}   {1:>12}")

    cc, cb = codeloom_resume(repo)
    cl_calls += cc
    cl_bytes += cb
    mc, mb = codeloom_query_memory(repo, "retry")
    cl_calls += mc
    cl_bytes += mb

    print("-" * 80)
    print(f"{'TOTAL':<42} {bare_calls:>10} {bare_bytes/1024:>7.1f}   "
          f"{cl_calls:>12}")
    print(f"\n  bare path:   {bare_calls} calls, {bare_bytes/1024:.1f} KB")
    print(f"  codeloom:    {cl_calls} calls, {cl_bytes/1024:.1f} KB")
    tok_bare = bare_bytes // 4
    tok_cl = cl_bytes // 4
    print(f"  tokens (est): bare {tok_bare} vs codeloom {tok_cl} "
          f"= {100*(1 - tok_cl/max(tok_bare,1)):.1f}% fewer")
    print(f"\n  VERDICT: bare needs {bare_calls} calls to re-derive what "
          f"codeloom restores in {cl_calls} call(s) after compaction")


if __name__ == "__main__":
    main()
