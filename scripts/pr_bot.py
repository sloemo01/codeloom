#!/usr/bin/env python3
"""Assemble the codeloom PR-bot review comment (clean-room design).

One sticky comment per PR, regenerated on every push, built entirely from
codeloom's own primitives (zero LLM, zero network):

  1. Risk verdict        --risk <merge-base>..HEAD   score + band + drivers
  2. What changed        git numstat digest         adds/dels per touched file
  3. Touched-area health --health scoped to the changed files
  4. New symbols         symbols defined in the diff that nobody calls yet
  5. Security sweep      eval/exec/hardcoded-secret/TODO patterns in the diff
  6. Review checklist    generated from what actually changed
  7. Reviewer brief      --pack keyed to the PR title

Sticky-comment contract: output starts with MARKER; the workflow finds and
updates its own previous comment instead of posting a new one per push.

Usage: pr_bot.py <revspec> [pr-title] [max-bytes]
"""
import io
import os
import re
import subprocess
import sys

MARKER = "<!-- codeloom-pr-bot:v1 -->"


def sh(cmd: list, timeout: int = 120) -> str:
    """Run an argv-style command (no shell) — safe against path injection."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return (r.stdout or "")
    except subprocess.TimeoutExpired:
        return ""


def run_codeloom(args: list, cap: int = 16000) -> str:
    """Run codeloom with argv args (no shell); return stdout truncated."""
    out = sh([sys.executable, "codeloom.py"] + args, timeout=180).strip()
    if not out:
        return "(no output)"
    out = out.replace(MARKER, "")
    if len(out) > cap:
        out = out[:cap] + "\n… (truncated)"
    return out


def section(title: str, body: str) -> str:
    return f"\n### {title}\n\n{body}\n"


# ----------------------------------------------------------------- #
# diff-derived sections
# ----------------------------------------------------------------- #

def changed_files(revspec: str):
    """[(path, adds, dels)] for code files in the range."""
    base, _, head = revspec.partition("..")
    raw = sh(["git", "diff", "--numstat", base or "HEAD~1", head or "HEAD"])
    files = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, p = parts
        if a == "-" or d == "-":   # binary
            continue
        if not re.search(r"\.(py|js|ts|jsx|tsx|go|rs|c|h|cpp|hpp|java|rb|php|cs|sh)$", p):
            continue
        files.append((p, int(a), int(d)))
    return files


def diff_digest(files) -> str:
    if not files:
        return "No code files changed."
    total_a = sum(a for _, a, _ in files)
    total_d = sum(d for _, _, d in files)
    buf = io.StringIO()
    buf.write(f"**{len(files)} code file(s)** · +{total_a}/−{total_d} lines\n\n")
    buf.write("| file | + | − | health |\n|---|---|---|---|\n")
    for p, a, d in sorted(files, key=lambda f: -(f[1] + f[2]))[:12]:
        buf.write(f"| {p} | {a} | {d} | see below |\n")
    if len(files) > 12:
        buf.write(f"\n…and {len(files) - 12} more\n")
    return buf.getvalue()


def touched_health(files, cap: int = 9000) -> str:
    """--health over the repo, filtered down to the touched files.

    Matches ANY line that names a touched file (covers both summary rows
    like 'path — N findings' and finding-detail rows like
    '[dead_symbol] path:12'), so nothing is dropped by format drift."""
    touched = {f for f, _, _ in files}
    basenames = {os.path.basename(f): f for f in touched}
    if not touched:
        return "No code files to screen."
    out = run_codeloom(["--health", "."], cap=cap)
    if not out or out == "(no output)":
        return "(no output)"
    lines = out.splitlines()
    keep = [l for l in lines[:2] if l.startswith("#")]  # headline
    for line in lines:
        if any(base in line for base in basenames):
            if line not in keep:
                keep.append(line)
    if len(keep) <= 1:
        return ("✅ No structural health findings in the "
                f"{len(touched)} touched file(s).")
    return "\n".join(keep)


NEW_SYMBOL_RE = re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"
                           r"|^\+\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)"
                           r"|^\+\s*func\s+([A-Za-z_]\w*)"
                           r"|^\+\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)"
                           r"|^\+\s*class\s+([A-Za-z_]\w*)", re.M)


def new_symbols(revspec: str, files) -> str:
    """Symbols introduced by this PR, with caller counts via --usages.

    Caller counts parse the codeloom output structurally (count of
    non-header lines) instead of grepping for keywords."""
    base, _, head = revspec.partition("..")
    lo, hi = base or "HEAD~1", head or "HEAD"
    names = []
    for path, _, _ in files[:30]:
        patch = sh(["git", "diff", lo, hi, "--", path])
        for m in NEW_SYMBOL_RE.finditer(patch):
            n = next(g for g in m.groups() if g)
            if n not in names and not n.startswith("_"):
                names.append(n)
    if not names:
        return "No new top-level symbols detected."
    rows = ["| symbol | callers found |", "|---|---|"]
    orphans = 0
    for n in names[:12]:
        u = sh([sys.executable, "codeloom.py", "--usages", n, "."])
        # count result lines that are neither headers nor the summary line
        cnt = sum(1 for l in u.splitlines()
                  if l.strip() and not l.startswith("#")
                  and "definition(s)" not in l and "usage" not in l.lower())
        if cnt <= 0:
            orphans += 1
            rows.append(f"| `{n}` | ⚠️ none — dead on arrival? |")
        else:
            rows.append(f"| `{n}` | {cnt} |")
    hint = ("\n\n⚠️ " + str(orphans) + " new symbol(s) have no detected callers — "
            "intentional entry points should be exempted or wired up." if orphans else "")
    return "\n".join(rows) + hint


SECURITY_PATTERNS = [
    (r"eval\(|exec\(", "dynamic execution (`eval`/`exec`)"),
    (r"(?i)(password|secret|api_key|apikey|auth_token)\s*[=:]\s*[\"'][^\"']{8,}[\"']",
     "possible hardcoded secret"),
    (r"http://(?!localhost|127\.0\.0\.1)", "insecure http:// URL"),
    (r"subprocess\.\w+\([^)]*shell\s*=\s*True", "shell=True subprocess"),
]


def security_sweep(revspec: str, files) -> str:
    findings = []
    self_path = "scripts/pr_bot.py"
    base, _, head = revspec.partition("..")
    lo, hi = base or "HEAD~1", head or "HEAD"
    for path, _, _ in files[:40]:
        patch = sh(["git", "diff", lo, hi, "--", path])
        for pat, label in SECURITY_PATTERNS:
            hits = re.findall(r"^\+.*$", patch, re.M)  # added lines only
            for line in hits:
                if re.search(pat, line):
                    snippet = line[1:].strip()[:110]
                    # don't flag the bot's own pattern definitions
                    if path == self_path and ("SECURITY_PATTERNS" in snippet
                                              or "insecure http" in snippet
                                              or "hardcoded secret" in snippet):
                        continue
                    findings.append((path, label, snippet))
    if not findings:
        return ("✅ Clean — no `eval`, hardcoded secrets, insecure URLs, or "
                "`shell=True` in added lines.")
    rows = ["| file | concern | snippet |", "|---|---|---|"]
    seen = set()
    for path, label, snip in findings[:10]:
        key = (path, label, snip)
        if key in seen:
            continue
        seen.add(key)
        rows.append(f"| {path} | {label} | `{snip}` |")
    extra = len(findings) - 10
    if extra > 0:
        rows.append(f"\n…and {extra} more finding(s)")
    return "\n".join(rows)


CHECKLIST_RULES = [
    (lambda fa, fd: fa > 400, "Very large diff (+{fa}) — consider splitting."),
    (lambda fa, fd: fd > fa * 2 and fd > 100,
     "Deletion-heavy change (−{fd}) — confirm removals are intentional."),
    (lambda fa, fd: any(p.endswith(("test.py", "_test.go")) is False for p in []) or False,
     ""),  # placeholder unused
]


def checklist(files, sec_findings_clean: bool, risk_body: str) -> str:
    items = []
    total_a = sum(a for _, a, _ in files)
    total_d = sum(d for _, _, d in files)
    test_touched = any(re.search(r"test|spec", p, re.I) for p, _, _ in files)
    src_touched = [p for p, _, _ in files
                   if not re.search(r"docs?/|README|\.md$|\.txt$", p)]
    items.append("Tests updated or added for changed behavior?"
                 + ("" if test_touched else " *(no test files in this diff)*"))
    if total_a > 400:
        items.append(f"Diff is large (+{total_a}) — consider splitting into "
                     "reviewable chunks.")
    if total_d > max(total_a * 2, 100):
        items.append(f"Deletion-heavy (−{total_d}) — confirm removals are intended.")
    if re.search(r"\[(high|critical)\]", risk_body):
        items.append("Risk band is high/critical — request a second reviewer.")
    if not sec_findings_clean:
        items.append("Security sweep flagged lines above — verify each one.")
    items.append("Docs/README mention the changed behavior if user-facing?")
    return "\n".join(f"- [ ] {i}" for i in items)


def main() -> int:
    revspec = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1..HEAD"
    pr_title = sys.argv[2] if len(sys.argv) > 2 else ""
    max_bytes = int(sys.argv[3]) if len(sys.argv) > 3 else 60000

    files = changed_files(revspec)

    buf = io.StringIO()
    buf.write(MARKER + "\n## 🪄 codeloom review\n")
    buf.write("> Deterministic analysis of this PR — zero LLM, zero network. "
              "Regenerated on every push.\n")

    base, _, head = revspec.partition("..")
    lo, hi = base or "HEAD~1", head or "HEAD"
    risk_body = run_codeloom(["--risk", f"{lo}..{hi}", "."])
    buf.write(section("Risk verdict", risk_body))

    if files:
        buf.write(section("What changed", diff_digest(files)))
        buf.write(section("Health of touched files", touched_health(files)))
        buf.write(section("New symbols & their callers",
                          new_symbols(revspec, files)))

    sec_body = security_sweep(revspec, files)
    buf.write(section("Security sweep", sec_body))

    clean_sec = sec_body.startswith("✅")
    buf.write(section("Review checklist", checklist(files, clean_sec, risk_body)))

    task = pr_title.strip() or "review this pull request"
    brief = run_codeloom(["--pack", task, "."], cap=14000)
    if brief and brief != "(no output)":
        buf.write(section("Reviewer's starting context", brief))

    out = buf.getvalue()
    if len(out.encode()) > max_bytes:
        out = out[:max_bytes] + "\n… (comment truncated at limit)\n"
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
