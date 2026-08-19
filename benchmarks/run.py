#!/usr/bin/env python3
"""codemap benchmark harness — reproducible timing on a real repo.

Usage:
    python3 benchmarks/run.py [--repo /path/to/repo] [--runs 3]

Clones browser-use (or uses a given repo), times each codemap operation, and
prints a table. This is the honest, reproducible proof of codemap's speed.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

CODEmap = os.path.join(os.path.dirname(__file__), "..", "codemap.py")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None, help="path to an existing repo (else clone browser-use)")
    ap.add_argument("--runs", type=int, default=3)
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
        cmd = [sys.executable, CODEmap] + args_list
        best, avg = time_op(cmd, repo, args.runs)
        print(f"{name:<10} {best:<10.3f} {avg:<10.3f}")


if __name__ == "__main__":
    main()
