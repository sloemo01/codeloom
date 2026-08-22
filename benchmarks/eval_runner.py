#!/usr/bin/env python3
"""eval_runner.py — one command to re-run every published codeloom benchmark.

Zero-dep CLI (stdlib only). Wraps the existing benchmark scripts:

    python3 benchmarks/eval_runner.py <kind> [--json] [--root PATH] [--crg-bin PATH]

Kinds:
    token      jcodemunch-style token-efficiency bench (3 repos x 5 tasks = 15
               task-runs, tiktoken cl100k_base). Reuses the core loop from
               benchmarks/token_efficiency.py by importing it (no copy).
    compaction benchmarks/compaction_recovery.py --repo <root> (post-compaction
               restore: bare grep-and-read vs codeloom --resume/--query-memory).
    sealed     benchmarks/live_sealed_run/harness.py --mode all --repo <root>
               (sealed retrieval: bare toolchain vs codeloom, no LLM).
    bench      the whole suite in one pass; emits a single JSON object.

Honesty rules (from the relay consensus):
    - loss rows are NEVER filtered — they are printed in a LOSS ROWS section
      and included in --json output.
    - labels stay honest: scripted policy simulation, not live-agent claims.
    - exit code is non-zero if any sub-benchmark fails.

--root overrides the default repo (/tmp/bench-fastapi; cloned if missing).
--crg-bin is accepted for parity with the planned vs-crg kind (reserved).

Core loop attribution: the token kind imports token_efficiency.py (the
canonical implementation) and calls its ensure_repo / grep_read_baseline /
codeloom_tokens functions instead of re-implementing them, so the numbers can
never drift from the published script.
"""
import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BENCH_DIR)
DEFAULT_ROOT = "/tmp/bench-fastapi"
FASTAPI_URL = "https://github.com/fastapi/fastapi.git"
SHALLOW = ["--depth", "1"]

KINDS = ("token", "compaction", "sealed", "bench", "all")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run(cmd, cwd=None, timeout=600):
    """Run a sub-benchmark; merge stderr into stdout so failures are visible."""
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, timeout=timeout)


def ensure_root(root):
    """Root must be a git checkout; clone fastapi if missing (same pattern as
    token_efficiency.py's ensure_repo)."""
    root = os.path.abspath(root)
    if os.path.isdir(os.path.join(root, ".git")):
        return root
    print(f"[setup] cloning {FASTAPI_URL} -> {root} (shallow) ...")
    subprocess.run(["git", "clone", "-q", "--depth", "1", FASTAPI_URL, root],
                   check=True)
    print("[setup] cloned.")
    return root


# --------------------------------------------------------------------------- #
# kind: token (imports benchmarks/token_efficiency.py — the canonical loop)
# --------------------------------------------------------------------------- #

def run_token(root, json_mode):
    sys.path.insert(0, BENCH_DIR)
    sys.path.insert(0, REPO_ROOT)
    try:
        import token_efficiency as te
    except Exception as exc:  # e.g. tiktoken missing -> script exits 1 at import
        raise RuntimeError(f"cannot import token_efficiency.py: {exc}")

    rows = []
    total_base = 0
    total_loom = 0
    print("# jcodemunch-style token-efficiency benchmark (via eval_runner)")
    print("# encoder: tiktoken cl100k_base")
    for repo, cfg in te.REPOS.items():
        path = cfg["path"]
        # --root overrides the fastapi checkout (kept on the other two repos)
        if root and repo == "fastapi":
            path = ensure_root(root)
        else:
            path = te.ensure_repo(repo, cfg)
        print(f"=== {repo} ({path}) ===")
        for sym in cfg["symbols"]:
            baseline = te.grep_read_baseline(path, sym)
            loom = te.codeloom_tokens(path, sym)
            pct = (1 - loom / max(baseline, 1)) * 100
            rows.append({"repo": repo, "symbol": sym, "baseline_tokens": baseline,
                         "codeloom_tokens": loom, "savings_pct": round(pct, 1)})
            total_base += baseline
            total_loom += loom
            if not json_mode:
                print(f"  {sym:15s} baseline={baseline:6d}t  codeloom={loom:5d}t  "
                      f"saved={pct:.1f}%")
        if not json_mode:
            print()
    overall = (1 - total_loom / max(total_base, 1)) * 100
    if not json_mode:
        print("=" * 60)
        print(f"TOTAL (15 task-runs): baseline={total_base}t codeloom={total_loom}t")
        print(f"OVERALL TOKEN REDUCTION: {overall:.1f}%")
        print("=" * 60)
    return {
        "kind": "token",
        "created_at": iso_now(),
        "runs": len(rows),
        "rows": rows,
        "baseline_tokens_total": total_base,
        "codeloom_tokens_total": total_loom,
        "token_savings_pct": round(overall, 1),
        "claim": "jcodemunch's claimed range: 95.0-96.4%",
        "loss_rows": [r for r in rows if r["codeloom_tokens"] >= r["baseline_tokens"]],
    }


# --------------------------------------------------------------------------- #
# kind: compaction
# --------------------------------------------------------------------------- #

ROW_RE = re.compile(
    r"^(?P<q>.*?)\s+(?P<bare_calls>\d+)\s+(?P<bare_kb>\d+\.\d+)\s+(?P<cl_calls>\d+)$")


def parse_compaction(text):
    """Parse benchmarks/compaction_recovery.py stdout into structured numbers."""
    bare_calls = bare_kb = cl_calls = cl_kb = 0
    bare_tok = cl_tok = red_pct = None
    rows = []
    for line in text.splitlines():
        m = re.search(r"bare path:\s+(\d+) calls,\s+([\d.]+) KB", line)
        if m:
            bare_calls, bare_kb = int(m.group(1)), float(m.group(2))
            continue
        m = re.search(r"codeloom:\s+(\d+) calls,\s+([\d.]+) KB", line)
        if m:
            cl_calls, cl_kb = int(m.group(1)), float(m.group(2))
            continue
        m = re.search(r"tokens \(est\): bare (\d+) vs codeloom (\d+) = ([\d.]+)% fewer",
                      line)
        if m:
            bare_tok, cl_tok = int(m.group(1)), int(m.group(2))
            red_pct = float(m.group(3))
            continue
        m = ROW_RE.match(line.rstrip())
        if m and not line.startswith(("=", "-")) and m.group("q").strip() != "TOTAL":
            rows.append({"question": m.group("q").strip(),
                         "bare_calls": int(m.group("bare_calls")),
                         "bare_kb": float(m.group("bare_kb")),
                         "codeloom_calls": int(m.group("cl_calls"))})
    loss = [r for r in rows if r["bare_calls"] <= r["codeloom_calls"]]
    return {
        "bare": {"calls": bare_calls, "kb": bare_kb,
                 "tokens_est": bare_tok},
        "codeloom": {"calls": cl_calls, "kb": cl_kb,
                     "tokens_est": cl_tok},
        "tokens_reduction_pct": red_pct,
        "rows": rows,
        "loss_rows": loss,
    }


def run_compaction(root, json):
    root = ensure_root(root)
    script = os.path.join(BENCH_DIR, "compaction_recovery.py")
    print(f"[run] python3 {script} --repo {root}")
    res = run([sys.executable, script, "--repo", root], cwd=root)
    out = res.stdout
    sys.stdout.write(out)  # script prints its own table; keep it visible
    data = parse_compaction(out)
    data.update(kind="compaction", created_at=iso_now(), repo=root,
                exit_code=res.returncode)
    return data


# --------------------------------------------------------------------------- #
# kind: sealed
# --------------------------------------------------------------------------- #

def parse_sealed(text, jsonl_path):
    """Extract harness summary lines + per-row data from the run.jsonl the
    harness itself writes (overwritten every run)."""
    summary = {}
    for line in text.splitlines():
        m = re.search(r"^(\w+): (\d+)/(\d+) found answer file, (\d+) calls, (\d+) tokens",
                      line.strip())
        if m:
            mode, found, total, calls, toks = m.groups()
            summary[mode] = {"found": int(found), "total": int(total),
                             "calls": int(calls), "tokens_est": int(toks)}
    rows = []
    if jsonl_path and os.path.isfile(jsonl_path):
        with open(jsonl_path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    rows.append(json.loads(ln))
    loss = [r for r in rows if not r.get("found_answer_file", True)]
    return {"summary": summary, "rows": rows, "loss_rows": loss}


def run_sealed(root, json):
    root = ensure_root(root)
    script = os.path.join(BENCH_DIR, "live_sealed_run", "harness.py")
    print(f"[sys] python3 {script} --mode all --repo {root}")
    res = run([sys.executable, script, "--mode", "all", "--repo", root], cwd=root)
    sys.stdout.write(res.stdout)
    jsonl = os.path.join(os.path.dirname(script), "run.jsonl")
    data = parse_sealed(res.stdout, jsonl)
    data.update(kind="sealed", created_at=iso_now(), repo=root,
                exit_code=res.returncode)
    return data


# --------------------------------------------------------------------------- #
# kind: bench — the whole suite in one pass
# --------------------------------------------------------------------------- #

def run_bench(root, json):
    results = {}
    ok = True
    for kind in ("token", "compaction", "sealed"):
        try:
            fn = {"token": run_token, "compaction": run_compaction,
                  "sealed": run_sealed}[kind]
            data = fn(root, json)
            results[kind] = data
            if data.get("exit_code"):
                ok = False
        except Exception as exc:  # noqa: BLE001 — surface every sub-bench failure
            ok = False
            results[kind] = {"kind": kind, "error": str(exc)}
            print(f"[ERROR] {kind}: {exc}")
    return {
        "kind": "bench",
        "created_at": iso_now(),
        "root": root,
        "runs": results,
        "ok": ok,
    }


# --------------------------------------------------------------------------- #
# LOSS ROWS (honesty rule: never filter loss rows)
# --------------------------------------------------------------------------- #

def print_loss_rows(data):
    loss = data.get("loss_rows") or []
    if not loss:
        return
    print("\nLOSS ROWS (published, never filtered):")
    for r in loss:
        if "symbol" in r:
            print(f"  [token] {r['repo']}/{r['symbol']}: "
                  f"codeloom={r['codeloom_tokens']}t >= baseline={r['baseline_tokens']}t")
        elif "question" in r:
            print(f"  [sealed] {r['mode']}: MISS  {r['question']} "
                  f"(truth: {r['truth']})")
        else:
            print(f"  [compaction] {r.get('question', '?')}: "
                  f"bare {r['bare_calls']}c <= codeloom {r['codeloom_calls']}c")


# --------------------------------------------------------------------------- #
# text rendering
# --------------------------------------------------------------------------- #

def render(data, kind):
    if kind in ("token",) or (kind == "bench" and "token" in (data.get("runs") or {})):
        pass  # run_token/run_compaction/run_sealed already printed their tables
    if kind == "compaction":
        print("\n===== COMPACTION RECOVERY (scripted policy simulation) =====")
        bare, cl = data["bare"], data["codeloom"]
        print(f"  bare:    {bare['calls']} calls, {bare['kb']:.1f} KB, "
              f"{bare['tokens_est']} tokens (est)")
        print(f"  codeloom:{cl['calls']} calls, {cl['kb']:.1f} KB, "
              f"{cl['tokens_est']} tokens (est)")
        if data.get("tokens_reduction_pct") is not None:
            print(f"  {data['tokens_reduction_pct']:.1f}% fewer tokens, "
                  f"{data['bare']['calls']/max(data['codeloom']['calls'],1):.1f}x "
                  f"fewer calls")
    if kind == "sealed":
        print("\n===== SEALED RETRIEVAL SUMMARY =====")
        for mode, s in (data.get("summary") or {}).items():
            print(f"  {mode}: {s['found']}/{s['total']} found answer file, "
                  f"{s['calls']} calls, {s['tokens_est']} tokens (est)")
    if kind == "bench":
        ok = data.get("ok")
        print(f"\n===== BENCH SUITE: {'OK' if ok else 'FAILED'} =====")
    print_loss_rows(data)
    if kind == "bench":
        for sub in ("token", "compaction", "sealed"):
            d = (data.get("runs") or {}).get(sub)
            if not d or d.get("error"):
                print(f"  {sub}: FAILED")
            else:
                print(f"  {sub}: ok")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="eval_bench",
        description="One command to re-run every codeloom benchmark "
                    "(offline, zero deps, loss rows included).")
    ap.add_argument("kind", choices=KINDS,
                    help="token | compaction | sealed | bench (all = bench)")
    ap.add_argument("--json", action="store_true",
                    help="emit a single JSON object to stdout; human-readable "
                         "progress goes to stderr so stdout is pure JSON")
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help=f"corpus root (default {DEFAULT_ROOT}; cloned if missing)")
    ap.add_argument("--crg-bin", default=None,
                    help="reserved: path to a code-review-graph binary for the "
                         "planned vs-crg kind (accepted for CLI parity)")
    args = ap.parse_args(argv)

    kind = "bench" if args.kind == "all" else args.kind
    start = time.time()

    # In --json mode stdout must be ONE valid JSON object: the sub-benchmark
    # tables are human output, so they go to stderr (the sub-scripts' own
    # stdout is captured for parsing and echoed to stderr for CI logs).
    if args.json:
        with contextlib.redirect_stdout(sys.stderr):
            data = _dispatch(kind, args)
        data["elapsed_sec"] = round(time.time() - start, 1)
        print(json.dumps(data, indent=2))
    else:
        data = _dispatch(kind, args)
        data["elapsed_sec"] = round(time.time() - start, 1)

    failed = data.get("exit_code") or (data.get("ok") is False)
    if failed:
        print(f"\n[FAILED] benchmark exit code {failed}", file=sys.stderr)
        return 1
    return 0


def _dispatch(kind, args):
    if kind == "token":
        data = run_token(args.root, args.json)
        if not args.json:
            render(data, kind)
    elif kind == "compaction":
        data = run_compaction(args.root, args.json)
        if not args.json:
            render(data, kind)
    elif kind == "sealed":
        data = run_sealed(args.root, args.json)
        if not args.json:
            render(data, kind)
    else:
        data = run_bench(args.root, args.json)
    return data


if __name__ == "__main__":
    sys.exit(main())
