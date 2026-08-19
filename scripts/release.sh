#!/usr/bin/env bash
# Create a GitHub release for the current version.
# Usage: bash scripts/release.sh
set -euo pipefail

VERSION=$(grep -m1 'VERSION = ' codemap.py | sed -E 's/.*"([^"]+)".*/\1/')
TAG="v$VERSION"

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
SHA=$(python3 codemap.py --verify codemap.py | grep sha256 | awk '{print $2}')
gh release create "$TAG" \
  --title "codemap $VERSION" \
  --notes "codemap $VERSION

- Single-file, zero-dependency, no-daemon codebase intelligence for AI agents.
- Map, import graph, cross-file call graph, symbol search, usage search, grep,
  task-awareness, git diff, incremental cache, MCP server.
- Optional tree-sitter / embedding / runtime-trace backends for precision.

**SHA-256 of codemap.py:** \`$SHA\`

Install: copy codemap.py into your repo, or \`curl -O\` + \`codemap --verify\`." \
  demo.gif

echo "Released $TAG"
