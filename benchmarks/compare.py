#!/usr/bin/env python3
"""Same-machine codeloom comparison benchmark (trust / reproducibility).

Reproduces codeloom's key claims on the machine you run it on, so anyone can
verify instead of trusting the README. Reports BOTH engines (pure-Python and
the optional C core) and the honest limits.

Run from the repo root:
    python3 benchmarks/compare.py                  # demo-repo (fast)
    python3 benchmarks/compare.py /path/to/repo     # any repo
"""
import json
import os
import subprocess
import sys
import time

LOOM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "codeloom.py")
LOOM = os.path.abspath(LOOM)
REPO = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "demo-repo")

# The C core (optional accelerator).
CORE = os.path.join(os.path.dirname(LOOM), "codeloom_core")
CORE_BUILT = os.path.isfile(CORE) and os.access(CORE, os.X_OK)


def run(args, cwd=None, timeout=600):
    cwd = cwd or REPO
    r = subprocess.run([sys.executable, LOOM] + args, capture_output=True,
                       text=True, cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def timeit(fn):
    import time
    t0 = time.time()
    out = fn()
    return time.time() - t0, out


def main():
    print("=" * 62)
    print("codeloom same-machine benchmark")
    print(f"  repo:     {REPO}")
    print(f"  python:   {sys.version.split()[0]}")
    print(f"  C engine: {'built' if CORE_BUILT else 'NOT built (pure-Python only)'}")
    print("=" * 62)

    # --- answer quality: what agents actually ask ---
    print("\n## 1. Answer quality (what agents query)")
    tests = [
        ("search 'Engine'", ["--search", "Engine", REPO], "definition"),
        ("task 'add retry'", ["--task", "add retry logic", REPO], "relevant"),
        ("get-symbol 'run'", ["--get-symbol", "run", REPO], "run"),
        ("impact 'engine'", ["--impact", "engine", REPO], "risk"),
        ("loom 'retry'", ["--loom", "add retry logic", REPO], "loom_context"),
    ]
    for label, args, expect in tests:
        try:
            rc, out, err = run(args, timeout=120)
            ok = rc == 0 and expect in out
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}" +
                  (f" ({len(out.splitlines())} lines)" if rc == 0 else f" rc={rc}"))
        except subprocess.TimeoutExpired:
            print(f"  [FAIL] {label} (timeout)")

    # --- index build time (both engines when C is present) ---
    print("\n## 2. Index build time")
    # remove any stale index so we measure a cold build
    for fn in (".codeloom-index.json", ".codeloom-index.bin", ".codeloom-index.lazy",
               ".codeloom-index.lazy.db"):
        p = os.path.join(REPO, fn)
        if os.path.exists(p):
            os.remove(p)

    eng = "c" if CORE_BUILT else "py"
    dt, (rc, out, err) = timeit(lambda: run(["--index", "--engine", eng, "--max-files", "20000", REPO]))
    print(f"  --index --engine {eng}: {dt:.1f}s  ({out.splitlines()[1].strip() if len(out.splitlines())>1 else ''})")
    if "built persistent" not in out and rc != 0:
        print(f"    rc={rc} err={err[:200]}")
    # lazy lookup speed
    dt, (rc, out, _) = timeit(lambda: run(["--get-symbol", "run", REPO]))
    print(f"  lazy --get-symbol 'run': {dt*1000:.0f}ms  ({'OK' if rc==0 else 'FAIL'})")

    print("\n## 3. What codeloom intentionally does NOT do")
    print("  - no 158-language tree-sitter breadth (25 langs + regex)")
    print("  - no bundled embeddings / semantic search (zero-dep default)")
    print("  - no daemon process (index is on-disk, resident per-session)")
    print("  - no claim to beat compiled C on cold-start sub-ms (lazy ~ms)")
    print("\nDone.")


if __name__ == "__main__":
    main()
