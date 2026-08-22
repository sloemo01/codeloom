#!/usr/bin/env bash
# Sync the single-file tool + MCP server into the npm package before pack/publish.
# Run from the npm/ directory. Requires python3 at runtime (not here).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

cp codeloom.py npm/codeloom.py
cp codeloom-mcp.py npm/codeloom-mcp.py
cp codeloom_mcp.py npm/codeloom_mcp.py
cp LICENSE npm/LICENSE

# README is the repo README (npx shows it on the registry page)
cp README.md npm/README.md

echo "synced codeloom.py ($(wc -l < npm/codeloom.py) lines), codeloom-mcp.py, codeloom_mcp.py, LICENSE, README.md into npm/"
