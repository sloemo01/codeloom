#!/usr/bin/env bash
# codeloom pre-commit risk hook — warn-only by default.
#
# Runs the repo's deterministic change-risk screen (codeloom.py --risk) against
# the STAGED changes only (no commit needed: the staged index is exported to a
# tree via `git write-tree` and diffed against HEAD), prints the verdict, and:
#   - default (warn-only): always exits 0 — never blocks a commit
#   - STRICT=1: exits 1 when the staged diff lands in the high/critical band
#
# Usage (installed by `python3 codeloom.py --install-hook` into .git/hooks/):
#   bash scripts/pre-commit-hook.sh
#
# Toggle below (single user-editable line) — no other edits required.
STRICT=${STRICT:-0}   # set to 1 to BLOCK commits whose staged diff scores high/critical

set -u

# --- locate repo root (hook cwd is the repo, but be safe) --------------------
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "codeloom pre-commit: not inside a git repo — skipping." >&2
    exit 0
}
cd "$ROOT" || exit 0

# --- locate codeloom.py -------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -n "${CODELOOM:-}" ] && [ -f "$CODELOOM" ]; then
    CODELOOM_PY="$CODELOOM"
elif [ -f "$ROOT/codeloom.py" ]; then
    CODELOOM_PY="$ROOT/codeloom.py"
else
    echo "codeloom pre-commit: codeloom.py not found (set CODELOOM or place it at repo root) — skipping." >&2
    exit 0
fi

# --- staged code files ------------------------------------------------------
# Only additions/copies/renames/modifications of code-ish files; deletions of
# code are not a risk trigger worth reporting.
CODE_EXTS='py|js|mjs|cjs|jsx|ts|tsx|go|rs|c|h|cc|cpp|cxx|hpp|hh|java|kt|rb|php|sh|bash|zsh|cs|swift|scala|clj|cljs|ex|exs|lua|pl|pm|r|dart|vue|svelte|m'
STAGED_FILES="$(git diff --cached --name-only --diff-filter=ACMR | grep -Ei "\.($CODE_EXTS)$" || true)"
if [ -z "$STAGED_FILES" ]; then
    echo "codeloom pre-commit: no staged code files — skipping risk check."
    exit 0
fi

# --- revspec for the staged changes only -------------------------------------
# HEAD..<index-tree> = exactly what is staged, without creating a commit.
# Fresh repo with no commits yet: diff the empty tree against the index tree.
EMPTY_TREE=4b825dc642cb6eb9a060e54bf8d69288fbee4904
if git rev-parse --verify --quiet HEAD >/dev/null 2>&1; then
    BASE=HEAD
else
    BASE=$EMPTY_TREE
fi
TREE="$(git write-tree 2>/dev/null)" || {
    echo "codeloom pre-commit: cannot resolve staged tree (unmerged index?) — skipping." >&2
    exit 0
}

# --- run the deterministic risk screen --------------------------------------
echo "codeloom pre-commit: staged files:"
printf '%s\n' "$STAGED_FILES" | sed 's/^/  /'
echo "codeloom pre-commit: assessing change risk (codeloom --risk $BASE..$TREE)..."
REPORT="$(python3 "$CODELOOM_PY" --risk "$BASE..$TREE" 2>&1)"
RC=$?
printf '%s\n' "$REPORT"

BAND="$(printf '%s\n' "$REPORT" | grep -oE '\[(none|low|medium|high|critical)\]' | head -n1 | tr -d '[]')"

if [ -z "$BAND" ]; then
    echo "codeloom pre-commit: no risk band parsed (screen errored?) — warn-only, not blocking." >&2
    exit 0
fi

echo "codeloom pre-commit: band = ${BAND}"

if [ "$STRICT" = "1" ]; then
    case "$BAND" in
        high|critical)
            echo "codeloom pre-commit [STRICT]: $BAND staged risk — blocking commit (exit 1)." >&2
            exit 1
            ;;
        *)
            echo "codeloom pre-commit [STRICT]: band '$BAND' below high — allowing."
            exit 0
            ;;
    esac
fi

echo "codeloom pre-commit: warn-only mode (STRICT=0) — never blocks."
exit 0
