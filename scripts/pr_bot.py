#!/usr/bin/env python3
"""Assemble the codeloom PR-bot review (clean-room design).

Two outputs, chosen by --format:

  markdown (default)  one sticky comment: risk verdict, diff digest,
                      touched-file health, new-symbol orphan detection,
                      security sweep, generated checklist, reviewer brief.

  json                machine-readable findings for INLINE review comments:
                      [{"path": str, "line": int, "severity": "P1"|"P2"|"P3",
                        "kind": str, "message": str}, ...]
                      The workflow maps these to exact diff lines via the
                      GitHub review API — same UX as human inline comments.

All analysis is deterministic codeloom primitives. Zero LLM, zero network.

Usage: pr_bot.py <revspec> [pr-title] [--format markdown|json] [--max-bytes N]
"""
import io
import json as _json
import os
import re
import subprocess
import sys

MARKER = "<!-- codeloom-pr-bot:v2 -->"

# severity order for sorting; P1 must-fix, P3 nit
_SEV_RANK = {"P1": 0, "P2": 1, "P3": 2}


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
    ("P1", r"eval\(|exec\(", "dynamic execution (`eval`/`exec`) — "
     "RCE sink if input is user-controlled"),
    ("P1", r"(?i)(password|secret|api_key|apikey|auth_token)\s*[=:]\s*[\"'][^\"']{8,}[\"']",
     "possible hardcoded secret — move to env/config"),
    ("P2", r"http://(?!localhost|127\.0\.0\.1)", "insecure http:// URL"),
    ("P2", r"subprocess\.\w+\([^)]*shell\s*=\s*True", "shell=True subprocess — "
     "injection risk; prefer argv lists"),
]

SELF_REF = re.compile(r"SECURITY_PATTERNS|insecure http|hardcoded secret"
                      r"|shell=True subprocess")


def _diff_lines(path: str, lo: str, hi: str):
    """Yield (new_file_line_number, '+', text) for added lines of one file."""
    patch = sh(["git", "diff", "--unified=0", lo, hi, "--", path])
    new_ln = 0
    for hunk in patch.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", hunk)
        if m:
            new_ln = int(m.group(1))
            continue
        if new_ln == 0 or hunk.startswith(("+++", "---")):
            continue
        if hunk.startswith("+"):
            yield new_ln, "+", hunk[1:]
            new_ln += 1
        elif hunk.startswith("-"):
            pass  # removed lines don't advance the new-file counter
        else:
            new_ln += 1


def collect_findings(revspec: str, files):
    """All line-pinned findings: dicts path/line/severity/kind/message."""
    base, _, head = revspec.partition("..")
    lo, hi = base or "HEAD~1", head or "HEAD"
    out = []

    # 1. security patterns on added lines
    for path, _, _ in files[:40]:
        for ln, sign, text in _diff_lines(path, lo, hi):
            for sev, pat, label in SECURITY_PATTERNS:
                if re.search(pat, text):
                    if (path == "scripts/pr_bot.py"
                            and SELF_REF.search(text)):
                        continue  # our own pattern definitions
                    out.append({"path": path, "line": ln, "severity": sev,
                                "kind": label, "message": f"`{text.strip()[:110]}`"})
                    break

    # 2. orphan new symbols (defined but zero detected callers)
    names = []
    sym_line = {}
    for path, _, _ in files[:30]:
        for ln, sign, text in _diff_lines(path, lo, hi):
            m = NEW_SYMBOL_RE.match("+" + text)
            if m:
                n = next(g for g in m.groups() if g)
                if not n.startswith("_"):
                    names.append(n)
                    sym_line[(path, n)] = ln
    for n in dict.fromkeys(names[:12]):
        u = sh([sys.executable, "codeloom.py", "--usages", n, "."])
        cnt = sum(1 for l in u.splitlines()
                  if l.strip() and not l.startswith("#")
                  and "definition(s)" not in l and "usage" not in l.lower())
        if cnt <= 0:
            for (p, name), ln in sym_line.items():
                if name == n:
                    out.append({"path": p, "line": ln, "severity": "P3",
                                "kind": "orphan symbol",
                                "message": f"`{n}` has no detected callers — "
                                    "wire it up, add a test, or prefix with `_`."})
                    break

    # 3. long functions introduced wholesale (>80 added lines in one file
    #    is covered by health; here flag TODO/FIXME left in additions)
    for path, _, _ in files[:40]:
        for ln, sign, text in _diff_lines(path, lo, hi):
            if re.search(r"\b(TODO|FIXME|XXX)\b", text) and path != self_path_guard():
                out.append({"path": path, "line": ln, "severity": "P3",
                            "kind": "unfinished-work marker",
                            "message": text.strip()[:110]})
    return sorted(out, key=lambda f: (_SEV_RANK.get(f["severity"], 9),
                                      f["path"], f["line"]))


def self_path_guard():
    return "scripts/pr_bot.py"


def inline_markdown(findings) -> str:
    """Human-readable digest of findings for the sticky comment."""
    if not findings:
        return ("✅ No line-pinned findings — security sweep clean, no orphan "
                "symbols, no unfinished-work markers.")
    rows = ["| severity | location | kind | note |", "|---|---|---|---|"]
    for f in findings[:20]:
        loc = f"{f['path']}:{f['line']}"
        rows.append(f"| **{f['severity']}** | `{loc}` | {f['kind']} | {f['message']} |")
    extra = len(findings) - 20
    if extra > 0:
        rows.append(f"\n…and {extra} more")
    return "\n".join(rows)


def security_sweep(revspec: str, files) -> str:
    base, _, head = revspec.partition("..")
    lo, hi = base or "HEAD~1", head or "HEAD"
    findings = []
    for path, _, _ in files[:40]:
        for ln, sign, text in _diff_lines(path, lo, hi):
            for sev, pat, label in SECURITY_PATTERNS:
                if re.search(pat, text):
                    if (path == "scripts/pr_bot.py"
                            and SELF_REF.search(text)):
                        continue
                    findings.append((path, label, text.strip()[:110]))
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
    args = sys.argv[1:]
    revspec = args[0] if args else "HEAD~1..HEAD"
    pr_title = ""
    fmt = "markdown"
    max_bytes = 60000
    i = 1
    while i < len(args):
        if args[i] == "--format" and i + 1 < len(args):
            fmt = args[i + 1]
            i += 2
        elif args[i] == "--max-bytes" and i + 1 < len(args):
            max_bytes = int(args[i + 1])
            i += 2
        else:
            pr_title = args[i]
            i += 1

    files = changed_files(revspec)

    if fmt == "json":
        findings = collect_findings(revspec, files)
        sys.stdout.write(_json.dumps(findings, indent=1))
        return 0

    buf = io.StringIO()
    buf.write(MARKER + "\n## 🪄 codeloom review\n")
    buf.write("> Deterministic analysis of this PR — zero LLM, zero network. "
              "Regenerated on every push.\n")

    base, _, head = revspec.partition("..")
    lo, hi = base or "HEAD~1", head or "HEAD"
    risk_body = run_codeloom(["--risk", f"{lo}..{hi}", "."])
    buf.write(section("Risk verdict", risk_body))

    findings = collect_findings(revspec, files)
    buf.write(section("Line-pinned findings (also posted inline)",
                      inline_markdown(findings)))

    if files:
        buf.write(section("What changed", diff_digest(files)))
        buf.write(section("Health of touched files", touched_health(files)))

    sec_clean = not any(f["kind"].startswith(("dynamic execution",
                                              "possible hardcoded secret",
                                              "insecure http",
                                              "shell=True")) for f in findings)
    buf.write(section("Review checklist",
                      checklist(files, sec_clean, risk_body)))

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
