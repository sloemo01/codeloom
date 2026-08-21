#!/usr/bin/env bash
# Create a GitHub release for the current version.
# Usage: bash scripts/release.sh
set -euo pipefail

VERSION=$(grep -m1 'VERSION = ' codeloom.py | sed -E 's/.*"([^"]+)".*/\1/')
TAG="v$VERSION"

# inject the language count into docs/index.html (placeholder -> real number)
sed -i.bak "s/<!--LANG_COUNT-->/${LANG_COUNT:-30}/" docs/index.html && rm -f docs/index.html.bak

echo "Releasing $TAG"

# ensure clean tree
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree not clean. Commit first." >&2
  exit 1
fi

# create tag + push
git tag "$TAG"
git push origin "$TAG"

# create GitHub release with the demo GIF + checksum
SHA=$(python3 codeloom.py --verify codeloom.py | grep sha256 | awk '{print $2}')
gh release create "$TAG" \
  --title "codeloom $VERSION" \
  --notes "codeloom $VERSION

- Single-file, zero-dependency, no-daemon codebase intelligence for AI agents.
- Map, import graph, cross-file call graph, symbol search, usage search, grep,
  task-awareness, git diff, incremental cache, MCP server.
- Optional tree-sitter / embedding / runtime-trace backends for precision.

**SHA-256 of codeloom.py:** \`$SHA\`

Install: copy codeloom.py into your repo, or \`curl -O\` + \`codeloom --verify\`." \
  demo.gif

echo "Released $TAG"
