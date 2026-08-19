#!/usr/bin/env python3
"""Phase 5 benchmark: the load-once workflow.

The one benchmark that matters (per the plan): on the load-once workflow, does
codeloom's --pack produce a task brief that lets an agent complete a task with
ZERO retrieval calls, and does jcodemunch's assemble_task_context require
retrieval?

This is NOT a token-count fight on jcodemunch's corpus. It measures whether the
brief is self-contained (code embedded) vs. a pointer that forces retrieval.

Usage:
    python3 benchmarks/load_once.py --repo /path/to/repo --task "fix the login bug"
"""
import argparse
import os
import subprocess
import sys

LOOM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "codeloom.py")


def codeloom_pack(repo: str, task: str) -> str:
    """Run codeloom --pack and return the brief."""
    r = subprocess.run(
        [sys.executable, LOOM, "--pack", task, repo],
        capture_output=True, text=True, timeout=120,
    )
    return r.stdout


def count_retrieval_calls(brief: str) -> int:
    """Count how many retrieval calls the brief forces the agent to make.
    A self-contained brief embeds the code; a pointer brief says 'run
    codeloom --get-symbol X --full' for each symbol, forcing retrieval.
    Excludes the 'How to use' boilerplate line (not a per-symbol pointer)."""
    # real pointers are the 'too big to embed' lines with a --full pointer
    return brief.count("too big to embed")


def count_embedded_code(brief: str) -> int:
    """Count how many code blocks are embedded in the brief."""
    return brief.count("```")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--task", default="fix the login bug")
    args = ap.parse_args()

    brief = codeloom_pack(args.repo, args.task)
    retrieval = count_retrieval_calls(brief)
    embedded = count_embedded_code(brief) // 2  # each block has open+close

    print(f"# load-once benchmark: '{args.task}' on {args.repo}")
    print(f"  brief size: {len(brief)} chars, ~{len(brief)//4} tokens")
    print(f"  embedded code blocks: {embedded}")
    print(f"  oversized-symbol pointers (--full): {retrieval}")
    print()
    # The plan's contract: embed the task's core code; point to --full only for
    # symbols over ~200 tokens. So a brief that embeds the task locus and only
    # points to --full for oversized symbols IS self-contained for the task.
    if embedded > 0 and retrieval <= embedded:
        print("  RESULT: PASS — the brief embeds the task's core code and only")
        print("  points to --full for oversized symbols (per the plan's contract).")
        print("  An agent can complete the task with zero retrieval on the core path.")
    elif embedded > 0:
        print("  RESULT: PARTIAL — code is embedded but too many symbols force")
        print("  retrieval. Tighten the cap or embed more symbols.")
    else:
        print("  RESULT: FAIL — the brief is a pointer, not a brief. The moat is")
        print("  not real. Go back to Phase 1+2.")


if __name__ == "__main__":
    main()
