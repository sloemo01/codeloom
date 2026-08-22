#!/usr/bin/env python3
"""codeloom benchmark harness — reproducible timing on a real repo.

Usage:
    python3 benchmarks/run.py [--repo /path/to/repo] [--runs 3]

Clones browser-use (or uses a given repo), times each codeloom operation, and
prints a table. This is the honest, reproducible proof of codeloom's speed.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

CodeLoom = os.path.join(os.path.dirname(__file__), "..", "codeloom.py")
DEFAULT_REPO = "https://github.com/browser-use/browser-use.git"

OPS = [
    ("map", ["."]),
    ("graph", ["--graph", "."]),
    ("cross", ["--cross", "."]),
    ("search", ["--search", "Agent", "."]),
    ("task", ["--task", "browser automation click", "."]),
    ("impact", ["--impact", "browser_use/agent/service", "."]),
]


def time_op(cmd, cwd, runs):
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=120)
        times.append(time.perf_counter() - t0)
    return min(times), sum(times) / len(times)


def token_consumption(repo, queries):
    """Measure tokens sent to the LLM for each retrieval strategy.
    Baseline: grep-and-read (open whole files). codeloom: --get-symbol.
    codeloom tokens = tiktoken count of its actual output (honest; a plain
    'not found' answer is a few tokens, not 0)."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        def tk(t):
            return len(enc.encode(t))
    except ImportError:
        def tk(t):
            return len(t) // 4
    import re
    results = []
    for q in queries:
        # baseline: grep for the query, open matching files whole
        # (use a Python fallback so rg isn't required)
        matching = []
        for root, _, fs in os.walk(repo):
            if ".git" in root or "node_modules" in root or ".venv" in root:
                continue
            for fn in fs:
                if fn.endswith((".py", ".js", ".ts", ".go", ".rs", ".java")):
                    p = os.path.join(root, fn)
                    try:
                        with open(p, "r", encoding="utf-8", errors="replace") as fh:
                            if q.lower() in fh.read().lower():
                                matching.append(p)
                    except OSError:
                        pass
        baseline_tokens = 0
        for f in matching[:3]:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    baseline_tokens += tk(fh.read())
            except OSError:
                pass
        # codeloom: --get-symbol returns just the symbol summary
        cmd = [sys.executable, CodeLoom, "--get-symbol", q, "."]
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        codeloom_tokens = tk(r.stdout)
        results.append({"query": q, "baseline_tokens": baseline_tokens,
                        "codeloom_tokens": codeloom_tokens})
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None, help="path to an existing repo (else clone browser-use)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--tokens", action="store_true", help="also run the token-consumption benchmark")
    args = ap.parse_args()

    if args.repo:
        repo = os.path.abspath(args.repo)
    else:
        tmp = tempfile.mkdtemp()
        repo = os.path.join(tmp, "browser-use")
        print(f"cloning {DEFAULT_REPO}...")
        subprocess.run(["git", "clone", "--depth", "1", "-q", DEFAULT_REPO, repo], check=True)

    print(f"repo: {repo}")
    print(f"runs: {args.runs}")
    print(f"{'op':<10} {'best (s)':<10} {'avg (s)':<10}")
    print("-" * 30)
    for name, args_list in OPS:
        cmd = [sys.executable, CodeLoom] + args_list
        best, avg = time_op(cmd, repo, args.runs)
        print(f"{name:<10} {best:<10.3f} {avg:<10.3f}")

    if args.tokens:
        print("\n=== Token consumption (vs grep-and-read baseline) ===")
        queries = ["Agent", "click", "extract"]
        results = token_consumption(repo, queries)
        print(f"{'query':<10} {'baseline':<12} {'codeloom':<12} {'savings':<10}")
        print("-" * 44)
        for r in results:
            savings = 0
            if r["baseline_tokens"] > 0:
                savings = (1 - r["codeloom_tokens"] / r["baseline_tokens"]) * 100
            print(f"{r['query']:<10} {r['baseline_tokens']:<12} {r['codeloom_tokens']:<12} {savings:<10.1f}%")


if __name__ == "__main__":
    main()
