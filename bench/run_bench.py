#!/usr/bin/env python3
"""Static-replay benchmark harness for codeloom.

HONESTY RULE: this is a STATIC REPLAY, not a live-model measurement. It counts
tool calls and bytes an agent would need to answer architecture questions
using codeloom vs. raw grep+read, on scripted policies. Live-agent token
savings require the sealed-harness protocol in TECHNICAL_REPORT.md §7 — never
present replay numbers as agent-token savings.

Usage: python3 bench/run_bench.py <repo_root> [questions.jsonl]
Writes bench/RESULTS.md with wins AND loss rows.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

DEFAULT_QUESTIONS = [
    "where is the retry logic",
    "what calls the main entry point",
    "how does the engine initialize",
    "where are config values read",
    "what breaks if I change the parser",
    "which module handles authentication",
    "where is error handling centralized",
    "what tests cover the api layer",
    "who uses the cache helper",
    "where do imports of utils converge",
    "what is the data model",
    "where is logging configured",
    "how do modules depend on each other",
    "which functions have no callers",
    "where is serialization done",
    "what owns database access",
    "where are cli flags parsed",
    "what is the public api surface",
    "where would a rate limiter go",
    "which file defines the version",
]


def codeloom_arm(root, question):
    """One codeloom call answers; count it + output size."""
    r = subprocess.run([sys.executable, os.path.join(REPO, "codeloom.py"),
                        root, "--answer", question],
                       capture_output=True, text=True, timeout=120)
    return {"calls": 1, "bytes": len(r.stdout)}


def bare_arm(root, question):
    """Scripted bare-agent policy: grep for terms, then read top hits.
    Mirrors what a file-reading agent does without code intelligence."""
    terms = [w for w in question.lower().split()
             if len(w) >= 4 and w not in ("what", "where", "which", "does", "breaks", "change")]
    calls = 0
    total_bytes = 0
    seen = set()
    for t in terms[:3]:
        g = subprocess.run(["grep", "-ril", t, root, "--include=*.py"],
                           capture_output=True, text=True, timeout=60)
        calls += 1
        hits = [h for h in g.stdout.splitlines() if h not in seen][:2]
        seen.update(hits)
        for h in hits:
            try:
                with open(h, encoding="utf-8", errors="replace") as f:
                    total_bytes += len(f.read())
                calls += 1
            except OSError:
                pass
    return {"calls": max(calls, 1), "bytes": total_bytes}


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else REPO
    qfile = sys.argv[2] if len(sys.argv) > 2 else None
    questions = DEFAULT_QUESTIONS
    if qfile and os.path.isfile(qfile):
        with open(qfile, encoding="utf-8") as f:
            questions = [json.loads(l)["q"] for l in f if l.strip()]

    rows = []
    win_c = win_b = tie = 0
    for q in questions:
        c = codeloom_arm(root, q)
        b = bare_arm(root, q)
        verdict = "codeloom" if (c["calls"], c["bytes"]) < (b["calls"], b["bytes"]) \
            else ("bare" if (b["calls"], b["bytes"]) < (c["calls"], c["bytes"]) else "tie")
        if verdict == "codeloom":
            win_c += 1
        elif verdict == "bare":
            win_b += 1
        else:
            tie += 1
        rows.append((q, c, b, verdict))

    lines = [
        "# codeloom static-replay benchmark",
        "",
        "> **Static replay, not live-model measurement.** Tool-call counts and",
        "> context bytes come from scripted policies (one `--answer` call vs.",
        "> grep+read chains). Live-agent numbers require the sealed-harness",
        "> protocol described in TECHNICAL_REPORT.md §7. Never cite these as",
        "> agent-token savings.",
        "",
        "Repo: `%s` · %d questions · %d codeloom wins / %d bare wins / %d ties"
        % (os.path.abspath(root), len(rows), win_c, win_b, tie),
        "",
        "| question | codeloom calls | codeloom bytes | bare calls | bare bytes | winner |",
        "|---|---|---|---|---|---|",
    ]
    for q, c, b, v in rows:
        lines.append("| %s | %d | %d | %d | %d | %s |" %
                     (q, c["calls"], c["bytes"], b["calls"], b["bytes"], v))
    lines += ["", "## Rows we lose", ""]
    losses = [r for r in rows if r[3] == "bare"]
    if not losses:
        lines.append("(none in this run)")
    else:
        for q, c, b, _v in losses:
            lines.append("- **%s**: bare policy was leaner (%d vs %d calls, %d vs %d bytes)"
                         % (q, b["calls"], c["calls"], b["bytes"], c["bytes"]))
    out = os.path.join(HERE, "RESULTS.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote %s (%d questions, %d/%d/%d W/L/T)" % (out, len(rows), win_c, win_b, tie))


if __name__ == "__main__":
    main()
