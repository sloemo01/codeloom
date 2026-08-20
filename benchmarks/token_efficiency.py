#!/usr/bin/env python3
"""jcodemunch-style token-efficiency benchmark (15 task-runs, tiktoken cl100k_base).

Reproduces jcodemunch's official benchmark structure:
- 3 canonical repos, 5 query tasks each = 15 task-runs
- Measures token reduction vs traditional grep-and-read LLM exploration
- Encoder: tiktoken cl100k_base (the standard used by Claude and GPT-4)

Usage:
    python3 benchmarks/token_efficiency.py
"""
import os
import re
import subprocess
import sys

try:
    import tiktoken
    ENC = tiktoken.get_encoding("cl100k_base")
except ImportError:
    print("tiktoken not installed. Run: python3 -m pip install tiktoken")
    sys.exit(1)

LOOM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "codeloom.py")

# 5 real, resolvable symbols per repo (matching jcodemunch's 5-task structure)
REPOS = {
    "express": {
        "path": "/tmp/bench-express",
        "symbols": ["View", "acceptParams", "append", "all", "accepts"],
    },
    "fastapi": {
        "path": "/tmp/bench-fastapi",
        "symbols": ["Body", "Cookie", "File", "Header", "Depends"],
    },
    "gin": {
        "path": "/tmp/bench-gin",
        "symbols": ["Abort", "AddParam", "Engine", "Default", "New"],
    },
}


def tk(text):
    return len(ENC.encode(text))


def grep_read_baseline(repo, symbol):
    """Simulate the grep-and-read baseline: find the symbol, read the file.
    Returns the token count of the full file read (the naive approach)."""
    # use codeloom's byte index to find the file defining the symbol
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    import codeloom
    files = []
    for root, dirs, fs in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules')]
        for fn in fs:
            if fn.endswith(('.py', '.go', '.js', '.ts')):
                files.append(os.path.join(root, fn))
    idx = codeloom.build_byte_index(files, repo)
    locs = idx.get(symbol)
    if not locs:
        return 0
    path = locs[0].get("path")
    if not path or not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        return tk(fh.read())


def codeloom_tokens(repo, symbol):
    """Token count of codeloom's --get-symbol retrieval result."""
    r = subprocess.run([sys.executable, LOOM, "--get-symbol", symbol, repo],
                       capture_output=True, text=True, timeout=120)
    return tk(r.stdout)


def main():
    print("# jcodemunch-style token-efficiency benchmark")
    print("# encoder: tiktoken cl100k_base (Claude/GPT-4 standard)")
    print("# 3 repos x 5 tasks = 15 task-runs\n")
    rows = []
    total_base = 0
    total_loom = 0
    for repo, cfg in REPOS.items():
        print(f"=== {repo} ===")
        for sym in cfg["symbols"]:
            baseline = grep_read_baseline(cfg["path"], sym)
            loom = codeloom_tokens(cfg["path"], sym)
            pct = (1 - loom / max(baseline, 1)) * 100
            rows.append((repo, sym, baseline, loom, pct))
            total_base += baseline
            total_loom += loom
            print(f"  {sym:15s} baseline={baseline:6d}t  codeloom={loom:5d}t  saved={pct:.1f}%")
        print()
    overall = (1 - total_loom / max(total_base, 1)) * 100
    print("=" * 60)
    print(f"TOTAL (15 task-runs): baseline={total_base}t codeloom={total_loom}t")
    print(f"OVERALL TOKEN REDUCTION: {overall:.1f}%")
    print(f"jcodemunch's claimed range: 95.0-96.4%")
    print("=" * 60)


if __name__ == "__main__":
    main()
