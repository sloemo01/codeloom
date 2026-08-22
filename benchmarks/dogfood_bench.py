#!/usr/bin/env python3
"""dogfood_bench.py — head-to-head: plain grep+read vs codeloom, same repo, same task.

Zero-dependency (stdlib only) harness that measures, deterministically and
without any LLM call, what an agent burns on the SAME task two ways:

  RUN A  plain grep+read simulation (the baseline agent loop):
         1. `git ls-files`            (inventory, truncated to 100 files)
         2. `grep -rn <expect>`       (symbol search over *.py, truncated to 40 hits)
         3. `sed -n 1,200p <top hit>` (naive full-ish read of the top hit file)
  RUN B  codeloom command sequence (on a scratch --local clone so the target
         repo's .codeloom-* state is never touched):
         map, graph, task, index, get-symbol xN, impact <top module>,
         verify-edit (on a dirty scratch tree), memory-add, memory, memory-stats

Metrics (per run): subprocess calls, tokens-in (captured stdout payloads),
tokens-out (the synthesized "answer" each side would feed the model), total
tokens, wall time, completeness (which --expect symbols appear in each side's
answer). Token encoder: tiktoken cl100k_base if importable from the
environment, else a bytes/4 estimate — the harness prints which it used.

The report is HONEST by construction: if run B's totals exceed run A's, the
report says "B exceeded A on N metrics" — nothing is hidden. Exit code is 0
on completion regardless of who won (we are measuring, not competing);
non-zero only for usage errors (bad flags, missing repo/codeloom).

Determinism: same repo + same --task + same --expect + same --seed
=> identical calls/tokens/completeness every run (wall time varies with
machine load, which is expected; --runs N reports the median wall).

Usage:
  python3 benchmarks/dogfood_bench.py --repo /tmp/bench-fastapi \\
      --task "explain the request lifecycle" --expect request,response,route
  python3 benchmarks/dogfood_bench.py --repo /tmp/bench-fastapi --json \\
      --runs 3 --seed 7
"""

import argparse
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

try:
    import tiktoken  # type: ignore
    ENCODER = "tiktoken cl100k_base"
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover — fallback path
    ENCODER = "bytes/4 fallback (tiktoken not importable)"
    _ENC = None


def tk(text: str) -> int:
    """Token estimate: tiktoken cl100k_base when available, else bytes/4."""
    if _ENC is not None:
        return len(_ENC.encode(text))
    return max(1, len(text.encode("utf-8", errors="replace")) // 4)


def cmd_cmd(argv, cwd, timeout=900):
    """Run one command. Returns (row, out). Never raises on nonzero rc —
    every rc is recorded and reported honestly. The row carries the full
    stdout payload for token accounting."""
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        rc, out = p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        rc, out = -1, f"[TIMEOUT after {timeout}s]"
    except FileNotFoundError:
        rc, out = -2, f"[not found: {argv[0]}]"
    wall = time.monotonic() - t0
    row = {"step": " ".join(argv), "rc": rc, "wall_s": round(wall, 3),
           "out": out}
    return row, rc


# ---------------------------------------------------------------------------
# Run A — the plain agent loop (deterministic, no LLM, no codeloom)
# ---------------------------------------------------------------------------

def run_a(repo, expect):
    """Fixed grep+read sequence. Returns (rows, answer)."""
    rows = []
    outputs = []

    # A1: repo inventory (what the plain agent greps against)
    row, rc = cmd_cmd(["git", "-C", repo, "ls-files"], repo)
    rows.append(row)
    outputs.append("\n".join(row["out"].splitlines()[:100]))  # head -100

    # A2: grep for the expected symbols over tracked python files
    pat = "|".join(re.escape(e) for e in expect)
    row, rc = cmd_cmd(["grep", "-rn", "-E", pat, "--include=*.py", repo],
                      repo)
    rows.append(row)
    outputs.append("\n".join(row["out"].splitlines()[:40]))  # head -40

    # A3: sed the top hit file (first file path in the grep output)
    top_file = None
    for line in row["out"].splitlines():
        m = re.match(r"^([^:]+):", line)
        if m:
            top_file = m.group(1)
            break
    if top_file:
        row, rc = cmd_cmd(["sed", "-n", "1,200p", top_file], repo)
        rows.append(row)
        outputs.append(row["out"])
    else:
        rows.append({"step": "sed -n 1,200p (no hits — skipped)", "rc": 0,
                     "wall_s": 0.0, "out": ""})

    return rows, "\n".join(outputs)


# ---------------------------------------------------------------------------
# Run B — codeloom sequence
# ---------------------------------------------------------------------------

def run_b(repo, task, expect, loom, seed):
    """codeloom sequence on a scratch clone of `repo`. Returns
    (rows, answer, evidence)."""
    scratch = tempfile.mkdtemp(prefix="dogfood_bench_")
    rows, outputs, evidence = [], [], {}
    try:
        row, rc = cmd_cmd(["git", "clone", "-q", "--local", repo, scratch],
                          repo)
        rows.append(row)
        if rc != 0:
            raise RuntimeError(f"git clone of {repo} failed (rc {rc})")

        # B1 map
        row, rc = cmd_cmd([sys.executable, loom, scratch], scratch)
        rows.append(row)
        outputs.append(row["out"])
        # B2 graph
        row, rc = cmd_cmd([sys.executable, loom, "--graph", scratch], scratch)
        rows.append(row)
        outputs.append(row["out"])
        # B3 task ranking
        row, rc = cmd_cmd([sys.executable, loom, "--task", task, scratch],
                          scratch)
        rows.append(row)
        outputs.append(row["out"])
        top_mod = None
        for line in row["out"].splitlines():
            m = re.match(r"^\s*\d+\.\s+(\S+)", line)
            if m:
                top_mod = m.group(1)
                break
        evidence["top_module"] = top_mod
        # B4 index (cold-start cost of symbol lookup — honest to charge it)
        row, rc = cmd_cmd([sys.executable, loom, "--index", scratch], scratch)
        rows.append(row)
        evidence["index_seconds"] = round(row["wall_s"], 3)
        outputs.append(row["out"])
        # B5 get-symbol for each expected function
        for sym in expect:
            row, rc = cmd_cmd([sys.executable, loom, "--get-symbol", sym,
                               scratch], scratch)
            rows.append(row)
            outputs.append(row["out"])
        # B6 impact on the top module
        if top_mod:
            row, rc = cmd_cmd([sys.executable, loom, "--impact", top_mod,
                               scratch], scratch)
            rows.append(row)
            outputs.append(row["out"])
            m = re.search(r"Direct dependents \((\d+)\)", row["out"])
            evidence["blast_radius_direct"] = int(m.group(1)) if m else None
            m = re.search(r"All dependents \((\d+)\)", row["out"])
            evidence["blast_radius_all"] = int(m.group(1)) if m else None
        # B7 verify-edit on a dirty scratch tree (append a harmless comment
        # line to the first tracked .py — a no-op edit that must not dangle)
        dirty = None
        row, rc = cmd_cmd(["git", "-C", scratch, "ls-files", "*.py"], scratch)
        for fn in row["out"].splitlines():
            if fn.endswith(".py"):
                dirty = os.path.join(scratch, fn)
                break
        if dirty:
            with open(dirty, "a", encoding="utf-8") as fh:
                fh.write("\n# dogfood-bench dirty edit\n")
        row, rc = cmd_cmd([sys.executable, loom, "--verify-edit", scratch],
                          scratch)
        rows.append(row)
        evidence["verify_edit_verdict"] = next(
            (ln for ln in row["out"].splitlines()
             if ln.startswith("VERDICT")), "")
        outputs.append(row["out"])
        # B8 memory-add + graph-linked retrieve + stats
        body = (f"dogfood bench {task!r}: deterministic memory write "
                f"(seed {seed})")
        row, rc = cmd_cmd([sys.executable, loom, "--memory-add",
                           "--type", "decision", "--title",
                           "dogfood benchmark entry", "--body", body,
                           "--symbols", expect[0] if expect else "main",
                           scratch], scratch)
        rows.append(row)
        evidence["memory_add_rc"] = rc
        if top_mod:
            row, rc = cmd_cmd([sys.executable, loom, "--memory", top_mod,
                               scratch], scratch)
            rows.append(row)
            evidence["memory_retrieve_rc"] = rc
            outputs.append(row["out"])
        row, rc = cmd_cmd([sys.executable, loom, "--memory-stats", scratch],
                          scratch)
        rows.append(row)
        evidence["memory_stats_rc"] = rc
        outputs.append(row["out"])

        return rows, "\n".join(outputs), evidence
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# Aggregation and reporting
# ---------------------------------------------------------------------------

def summarize(rows, answer):
    """Aggregate one run into the metric row (calls/tokens/wall)."""
    return {
        "calls": len(rows),
        "tokens_in": sum(tk(r["out"]) for r in rows),
        "tokens_out": tk(answer),
        "wall_s": sum(r["wall_s"] for r in rows),
    }


def completeness(answer, expect):
    """Which expected symbols appear in the side's answer text."""
    return {e: (e in answer) for e in expect}


def render_markdown(args, repo, loom, metrics, ev, comp_a, comp_b):
    m = metrics
    lines = []
    lines.append("# Dogfood head-to-head: plain grep+read vs codeloom")
    lines.append("")
    lines.append(f"- repo: `{repo}`")
    lines.append(f"- task: `{args.task}`")
    lines.append(f"- expect: `{', '.join(args.expect_list)}`")
    lines.append(f"- runs: `{args.runs}`  seed: `{args.seed}`")
    lines.append(f"- token encoder: {ENCODER}")
    lines.append(f"- codeloom: `{loom}`")
    lines.append("")
    lines.append("| metric | A (grep+read) | B (codeloom) | B vs A |")
    lines.append("|---|---|---|---|")
    rows_ = [
        ("subprocess calls", m["calls_a"], m["calls_b"]),
        ("tokens-in (payloads)", m["tokens_in_a"], m["tokens_in_b"]),
        ("tokens-out (answer)", m["tokens_out_a"], m["tokens_out_b"]),
        ("total tokens", m["total_a"], m["total_b"]),
        ("wall time (s, median)", m["wall_a"], m["wall_b"]),
    ]
    for name, av, bv in rows_:
        if av == 0:
            ratio = "—"
        else:
            ratio = f"{100.0 * (bv - av) / av:+.1f}%"
        lines.append(f"| {name} | {av} | {bv} | {ratio} |")
    lines.append("")
    lines.append("| completeness (expect symbol present in answer) | A | B |")
    lines.append("|---|---|---|")
    for s in args.expect_list:
        lines.append(f"| `{s}` | {'yes' if comp_a[s] else 'no'} | "
                     f"{'yes' if comp_b[s] else 'no'} |")
    lines.append("")
    # Loss rows — honest, never hidden: any metric where B's number exceeds A's
    all_rows = rows_  # [(name, a, b), ...] for every metric in the table
    losses = [(name, av, bv) for name, av, bv in all_rows if bv > av]
    if losses:
        lines.append(f"**LOSS — B exceeded A on {len(losses)} metric(s):**")
        for name, av, bv in losses:
            lines.append(f"- {name}: A {av} vs B {bv}")
    else:
        lines.append("**B did not exceed A on any metric.**")
    lines.append("")
    # Evidence lines (what B won on)
    lines.append("**Evidence from B's run (things grep+read cannot produce):**")
    if ev.get("top_module"):
        lines.append(f"- `--task` ranked top module: `{ev['top_module']}`")
    if ev.get("blast_radius_direct") is not None:
        lines.append(f"- `--impact` blast radius: "
                     f"{ev['blast_radius_direct']} direct + "
                     f"{ev.get('blast_radius_all', 0)} total "
                     f"(incl. transitive) dependents")
    if ev.get("verify_edit_verdict"):
        lines.append(f"- `--verify-edit` verdict on dirty tree: "
                     f"{ev['verify_edit_verdict']}")
    if ev.get("memory_add_rc") == 0:
        lines.append(f"- memory layer: wrote + retrieved + stats "
                     f"(rcs {ev.get('memory_add_rc')}/"
                     f"{ev.get('memory_retrieve_rc')}/"
                     f"{ev.get('memory_stats_rc')}) — state survives, "
                     f"grep+read has no memory")
    lines.append("")
    lines.append("_Verdict: measuring, not competing — exit 0 regardless. "
                 "Wall time is machine-load dependent; calls/tokens are "
                 "deterministic for a fixed repo/task/expect/seed._")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Dogfood head-to-head: plain grep+read vs codeloom "
                    "(deterministic, zero LLM).")
    ap.add_argument("--repo", required=True, help="path to a git repo to bench")
    ap.add_argument("--task", default="explain the request lifecycle",
                    help="the task text handed to --task and used by grep")
    ap.add_argument("--expect", default="request,response,route",
                    help="comma-separated symbol names to grep for / "
                         "--get-symbol / completeness check")
    ap.add_argument("--runs", type=int, default=1,
                    help="number of head-to-head repetitions (default 1)")
    ap.add_argument("--codeloom", default=None,
                    help="path to codeloom.py (default: sibling of benchmarks/)")
    ap.add_argument("--json", action="store_true",
                    help="emit the report as JSON instead of markdown")
    ap.add_argument("--seed", type=int, default=0,
                    help="PRNG seed (default 0; reserved for future tie-breaks)")
    args = ap.parse_args(argv)
    args.expect_list = [s.strip() for s in args.expect.split(",") if s.strip()]
    if not args.expect_list:
        print("error: --expect must name at least one symbol", file=sys.stderr)
        return 2

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"error: {repo} is not a git repository", file=sys.stderr)
        return 2
    loom = args.codeloom or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "codeloom.py")
    if not os.path.isfile(loom):
        print(f"error: codeloom not found at {loom} (use --codeloom)",
              file=sys.stderr)
        return 2

    header = (f"# dogfood bench — {repo}\n"
              f"task: {args.task}\n"
              f"expect: {', '.join(args.expect_list)}   runs: {args.runs}   "
              f"seed: {args.seed}\n"
              f"token encoder: {ENCODER}\n")
    if args.json:
        print(header, file=sys.stderr, end="")  # keep stdout pure JSON
    else:
        print(header, end="", flush=True)

    all_a, all_b, all_ev, all_comp_a, all_comp_b = [], [], [], [], []
    rng = random.Random(args.seed)  # deterministic; reserved for future use
    for _ in range(max(1, args.runs)):
        a_rows, a_ans = run_a(repo, args.expect_list)
        b_rows, b_ans, ev = run_b(repo, args.task, args.expect_list, loom,
                                  args.seed)
        all_a.append(summarize(a_rows, a_ans))
        all_b.append(summarize(b_rows, b_ans))
        all_ev.append(ev)
        all_comp_a.append(completeness(a_ans, args.expect_list))
        all_comp_b.append(completeness(b_ans, args.expect_list))

    a0, b0 = all_a[0], all_b[0]
    metrics = {
        "calls_a": a0["calls"], "calls_b": b0["calls"],
        "tokens_in_a": a0["tokens_in"], "tokens_in_b": b0["tokens_in"],
        "tokens_out_a": a0["tokens_out"], "tokens_out_b": b0["tokens_out"],
        "total_a": a0["tokens_in"] + a0["tokens_out"],
        "total_b": b0["tokens_in"] + b0["tokens_out"],
        "wall_a": statistics.median(r["wall_s"] for r in all_a),
        "wall_b": statistics.median(r["wall_s"] for r in all_b),
    }
    ev = all_ev[0]
    comp_a, comp_b = all_comp_a[0], all_comp_b[0]

    report = {"repo": repo, "task": args.task,
              "expect": args.expect_list, "runs": args.runs,
              "seed": args.seed, "encoder": ENCODER,
              "codeloom": loom, "metrics": metrics,
              "completeness_a": comp_a, "completeness_b": comp_b,
              "evidence": ev}

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(args, repo, loom, metrics, ev, comp_a,
                              comp_b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
