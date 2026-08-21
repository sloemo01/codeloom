#!/usr/bin/env bash
# Regenerate grammar parity fixtures. Run ONLY when an extraction change is
# intentional — CI compares engine output against these frozen files, so a
# casual regeneration defeats the parity check.
set -euo pipefail
cd "$(dirname "$0")"
cargo build --release
for d in fixtures/*/; do
  file=$(ls "$d"sample.* 2>/dev/null | head -1)
  [ -n "$file" ] || continue
  ./target/release/codeloom_engine --list "$d" | ./target/release/codeloom_engine > "$d/expected.jsonl"
done
echo "regenerated $(ls fixtures/*/expected.jsonl | wc -l | tr -d ' ') fixtures"
