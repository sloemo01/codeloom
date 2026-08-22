#!/usr/bin/env python3
"""memory_extract.py -- deterministic git-history memory extractor.

Phase 3 of the "Memory OS": a zero-dependency companion script that reads
`git log` and auto-creates typed memories via the codeloom core's
`--memory-add` flag. Pure stdlib. No LLM, no network -- every heuristic is a
deterministic regex over commit messages.

Heuristics (applied to the full message = subject + body):
  bug          matches (fix|bug|resolve|resolved|close|closes|closes #|
                issue|crash|panic|segfault|regression) case-insensitively
                -> confidence 0.7, +0.2 when "regression"/"critical" present,
                capped at 0.95
  api          matches (api|endpoint|route|interface|schema|contract) -> 0.6
  architecture matches (migrat|refactor|re-architect|redesign), or bare
                "architect*" wording -- but NOT a typo/spelling fix -> 0.55

A single commit may yield multiple memories (e.g. "fix: api crash").

Dedupe: after each real run, a state file is written to
<repo>/.codeloom-memory/extract-state.json recording every extracted
(type, title-sha1) pair. Re-runs skip already-known pairs, so the extractor
is idempotent. --all ignores the state file and re-attempts everything
(state is still refreshed afterwards). --dry-run never calls the core and
never writes state.

The core is invoked as a subprocess argv (never a shell):
    python3 <core> --memory-add --type T --title TITLE --body BODY
                   [--symbols S1,S2,...]
where symbols are derived from the files changed by the commit: basename,
extension stripped, snake_case/kebab/dotted -> PascalCase module names.

Core resolution order: $CODELOOM_CORE override, then <repo>/codeloom.py,
then the codeloom.py sibling of this script (lets the extractor run against
any git repo straight from the codeloom checkout).

Exit code is 0 on success (including "nothing to extract"); non-zero only
for unexpected errors (not a git repo, missing core, git failure).
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

# --------------------------------------------------------------------------
# deterministic heuristics
# --------------------------------------------------------------------------

BUG_RE = re.compile(
    r"(fix|bug|resolve|resolved|close|closes|closes\s*#|issue|crash|panic|"
    r"segfault|regression)",
    re.IGNORECASE,
)
BUG_BOOST_RE = re.compile(r"(regression|critical)", re.IGNORECASE)
API_RE = re.compile(
    r"(api|endpoint|route|interface|schema|contract)", re.IGNORECASE
)
# "migrat" covers migrate/migration/migrating; "re-architect"/"redesign" are
# unambiguous structural verbs -- they count even if the message also mentions
# typos elsewhere.
ARCH_STRONG_RE = re.compile(r"(migrat|refactor|re-architect|redesign)", re.IGNORECASE)
# bare "architecture/architectural" wording only counts when the commit is
# not a typo/spelling fix (the spec's "AND NOT just a typo fix" guard).
ARCH_WORD_RE = re.compile(r"\barchitect", re.IGNORECASE)
TYPO_RE = re.compile(r"(typo|spelling)", re.IGNORECASE)

BUG_CONFIDENCE = 0.7
BUG_BOOST = 0.2
API_CONFIDENCE = 0.6
ARCH_CONFIDENCE = 0.55
MAX_CONFIDENCE = 0.95

# File extensions whose basenames count as "symbols" (code modules only --
# docs/readmes are noise). Extension stripped, then converted to PascalCase.
SYMBOL_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".rb", ".php", ".cs", ".swift", ".dart",
    ".c", ".cc", ".cpp", ".h", ".hpp",
    ".sh", ".lua", ".pl", ".sql",
}


def classify(message):
    """Deterministic heuristic pass over a commit message.

    Returns a list of (memory_type, confidence) tuples; a commit can match
    several types (e.g. "fix: api crash" -> bug + api).
    """
    found = []
    if BUG_RE.search(message):
        conf = BUG_CONFIDENCE
        if BUG_BOOST_RE.search(message):
            conf += BUG_BOOST
        found.append(("bug", round(min(conf, MAX_CONFIDENCE), 2)))
    if API_RE.search(message):
        found.append(("api", API_CONFIDENCE))
    if _is_architecture(message):
        found.append(("architecture", ARCH_CONFIDENCE))
    return found


def _is_architecture(message):
    if ARCH_STRONG_RE.search(message):
        return True
    # bare "architecture*" wording counts only when the commit is not a
    # typo/spelling fix (e.g. "fix typo: architecture" must NOT extract).
    return ARCH_WORD_RE.search(message) is not None and TYPO_RE.search(message) is None


def derive_symbols(files):
    """Changed file names -> module-ish PascalCase symbols.

    "api/sessions.py"     -> "Sessions"
    "auth_service.py"     -> "AuthService"
    "test_engine.go"      -> "Engine"          (test_ prefix stripped)
    "README.md"           -> (skipped, not code)
    Deduplicated, stable order.
    """
    out, seen = [], set()
    for path in files:
        base = os.path.basename(path.rstrip())
        if not base or base.startswith("."):
            continue
        stem, ext = os.path.splitext(base)
        if ext.lower() not in SYMBOL_EXTS:
            continue
        stem = re.sub(r"^test[_.-]", "", stem)
        stem = re.sub(r"[_.-]+test$", "", stem)
        parts = [p for p in re.split(r"[_.-]+", stem) if p]
        if not parts:
            continue
        name = "".join(p[:1].upper() + p[1:] for p in parts)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


# --------------------------------------------------------------------------
# git history
# --------------------------------------------------------------------------

GIT_FORMAT = "%x1e%H%x1f%s%x1f%b"


def git_log(repo, count):
    """Return [{'sha','subject','body','files'}, ...], newest first."""
    cmd = ["git", "-C", repo, "log"]
    if count:
        cmd.append("-%d" % count)
    cmd += ["--name-only", "--format=" + GIT_FORMAT]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise RuntimeError("git log failed in %s: %s" % (repo, proc.stderr.strip()))
    return _parse_log(proc.stdout)


def _parse_log(text):
    commits = []
    for chunk in text.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.split("\n")
        header = lines[0].split("\x1f", 2)
        if len(header) < 2:
            continue  # malformed record -- skip deterministically
        sha, subject = header[0], header[1]
        body = header[2] if len(header) > 2 else ""
        files = [ln.strip() for ln in lines[1:] if ln.strip()]
        commits.append(
            {"sha": sha, "subject": subject.strip(), "body": body.strip(), "files": files}
        )
    return commits


# --------------------------------------------------------------------------
# dedupe state
# --------------------------------------------------------------------------

STATE_VERSION = 1


def state_path(repo):
    return os.path.join(repo, ".codeloom-memory", "extract-state.json")


def load_state(repo):
    """Return dict { (type, title_sha): entry } from the state file."""
    path = state_path(repo)
    entries = {}
    if not os.path.exists(path):
        return entries
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for e in data.get("extracted", []):
            key = (e.get("type"), e.get("title_sha"))
            if None not in key:
                entries[key] = e
    except Exception:
        # Corrupt/partial state: start fresh rather than block extraction.
        entries = {}
    return entries


def save_state(repo, entries):
    """Atomically persist the (type, title_sha) -> entry map."""
    memdir = os.path.join(repo, ".codeloom-memory")
    os.makedirs(memdir, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "extracted": sorted(entries.values(), key=lambda e: (e["type"], e["title_sha"])),
    }
    fd, tmp = tempfile.mkstemp(dir=memdir, prefix=".extract-state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, state_path(repo))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def title_sha(title):
    """Deterministic dedupe key component: sha1 of normalized title."""
    norm = re.sub(r"\s+", " ", title).strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# core invocation
# --------------------------------------------------------------------------

def resolve_core(repo):
    """Order: $CODELOOM_CORE, <repo>/codeloom.py, sibling codeloom.py."""
    env = os.environ.get("CODELOOM_CORE")
    if env:
        if os.path.isfile(env):
            return os.path.abspath(env)
        raise RuntimeError("CODELOOM_CORE points to a missing file: %s" % env)
    own = os.path.join(repo, "codeloom.py")
    if os.path.isfile(own):
        return own
    sibling = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "codeloom.py")
    if os.path.isfile(sibling):
        return sibling
    raise RuntimeError(
        "no codeloom core found: tried <repo>/codeloom.py, the script's sibling "
        "codeloom.py, and $CODELOOM_CORE"
    )


def build_argv(core, mem_type, title, body, symbols):
    argv = [
        sys.executable or "python3",
        core,
        "--memory-add",
        "--type", mem_type,
        "--title", title,
        "--body", body,
    ]
    if symbols:
        argv += ["--symbols", ",".join(symbols)]
    return argv


def invoke(core, mem_type, title, body, symbols):
    """Call the core. Returns (ok, stderr_tail). Never uses a shell."""
    argv = build_argv(core, mem_type, title, body, symbols)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        return False, "could not launch %s: %s" % (argv[0], exc)
    if proc.returncode == 0:
        return True, ""
    tail = proc.stderr.strip().splitlines()
    return False, " ".join(tail[-3:]) if tail else ("exit %d" % proc.returncode)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="memory_extract.py",
        description="Extract bug/api/architecture memories from git history (deterministic, no LLM).",
    )
    ap.add_argument("--since", type=int, metavar="N", default=None,
                    help="only the last N commits (default: whole history)")
    ap.add_argument("--all", action="store_true",
                    help="scan the whole history and ignore the dedupe state")
    ap.add_argument("--repo", metavar="ROOT", default=".",
                    help="git repository root (default: current directory)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be created without calling the core; no state write")
    ap.add_argument("--min-confidence", type=float, default=0.0, metavar="N",
                    help="skip candidate memories below this confidence (default 0)")
    args = ap.parse_args(argv)

    if args.since is not None and args.since < 1:
        ap.error("--since must be >= 1")

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(os.path.join(repo, ".git")):
        sys.stderr.write("error: %s is not a git repository\n" % repo)
        return 1

    if args.since is None:
        count = None
    elif args.all:
        count = None
    else:
        count = args.since
    try:
        commits = git_log(repo, count)
    except RuntimeError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1

    if args.all:
        known = {}
    else:
        known = load_state(repo)
    core = resolve_core(repo)

    stats = {"bug": 0, "api": 0, "architecture": 0}
    skipped = 0
    failed = 0

    for commit in commits:
        subject = commit["subject"]
        if not subject:
            continue
        body = commit["body"]
        full = subject if not body else subject + "\n" + body

        for mem_type, conf in classify(full):
            if conf < args.min_confidence:
                continue
            key = (mem_type, title_sha(subject))
            if key in known:
                skipped += 1
                continue
            symbols = derive_symbols(commit["files"])

            if args.dry_run:
                argv = build_argv(core, mem_type, subject, full, symbols)
                print("WOULD " + " ".join(shlex.quote(a) for a in argv))
                stats[mem_type] += 1
                continue

            ok, err = invoke(core, mem_type, subject, full, symbols)
            if ok:
                stats[mem_type] += 1
                known[key] = {
                    "type": mem_type,
                    "title_sha": key[1],
                    "title": subject,
                    "confidence": conf,
                    "commit": commit["sha"],
                }
            else:
                failed += 1
                sys.stderr.write(
                    "warn: core rejected %s memory '%s' (commit %s): %s\n"
                    % (mem_type, subject[:60], commit["sha"][:8], err)
                )

    if not args.dry_run:
        # Only successfully-created memories are recorded, so failed calls
        # are retried on the next run and successes are never duplicated.
        save_state(repo, known)

    summary = "extracted %d bug, %d api, %d architecture memories" % (
        stats["bug"], stats["api"], stats["architecture"],
    )
    if args.dry_run:
        summary += " (dry-run)"
    if skipped:
        summary += " (%d skipped by dedupe)" % skipped
    if failed:
        summary += " (%d core call(s) failed)" % failed
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
