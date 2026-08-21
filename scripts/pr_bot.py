#!/usr/bin/env python3
"""Assemble the codeloom PR-bot review comment (clean-room design).

Pulls together three codeloom primitives and renders one markdown comment:

  1. --risk <merge-base>..HEAD   deterministic change-risk verdict + drivers
  2. --health                    structural findings in the touched area
  3. --pack "<PR title>"         task brief: what matters / read-first order

Sticky-comment contract: the whole output starts with a marker constant so
the workflow can find-and-update its own previous comment instead of
spamming a new one per push.

Usage: pr_bot.py <revspec> [pr-title] [max-bytes]
"""
import io
import os
import subprocess
import sys

MARKER = "<!-- codeloom-pr-bot:v1 -->"


def run(cmd: str, cap: int = 24000) -> str:
    """Run a codeloom CLI command, return stdout (truncated, never fatal)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=180)
        out = (r.stdout or "").strip() or (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return "(timed out)"
    if not out:
        return "(no output)"
    out = out.replace(MARKER, "")  # never nest our marker inside itself
    if len(out) > cap:
        out = out[:cap] + "\n… (truncated)"
    return out


def section(title: str, body: str) -> str:
    return f"\n### {title}\n\n{body}\n"


def main() -> int:
    revspec = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1..HEAD"
    pr_title = sys.argv[2] if len(sys.argv) > 2 else ""
    max_bytes = int(sys.argv[3]) if len(sys.argv) > 3 else 60000

    buf = io.StringIO()
    buf.write(MARKER + "\n")
    buf.write("## 🪄 codeloom review\n")
    buf.write("> Deterministic analysis — zero LLM, zero network. "
              "Regenerated on every push to this PR.\n")

    # 1. risk verdict (score, band, drivers)
    buf.write(section("Risk verdict", run(f"python3 codeloom.py --risk '{revspec}' .")))

    # 2. structural health — headline + worst files only (keep the comment tight)
    health_full = run("python3 codeloom.py --health .", cap=8000)
    lines = health_full.splitlines()
    keep: list = []
    for i, line in enumerate(lines):
        keep.append(line)
        if line.startswith("## Worst files") or line.startswith("## Top"):
            keep.extend(lines[i + 1:i + 7])
            break
    if keep:
        buf.write(section("Code health (whole repo)", "\n".join(keep)))

    # 3. task brief for reviewers
    task = pr_title.strip() or "review this pull request"
    safe_task = task.replace("'", "").replace("$", "")
    brief = run(f"python3 codeloom.py --pack '{safe_task}' .", cap=16000)
    if brief and brief != "(no output)":
        buf.write(section("Reviewer's task brief", brief))

    out = buf.getvalue()
    if len(out.encode()) > max_bytes:
        out = out[:max_bytes] + "\n… (comment truncated at limit)\n"
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
