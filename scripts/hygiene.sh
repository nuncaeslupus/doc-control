#!/usr/bin/env bash
# Repo hygiene gate: no Word lock files, no real documents outside test
# fixtures, no oversized files. Runs locally (host-gate) and in CI.
set -euo pipefail
fail=0
files=$(git ls-files)

locks=$(grep -E '(^|/)~\$' <<<"$files" || true)
if [[ -n "$locks" ]]; then echo "Word lock files tracked:"; echo "$locks"; fail=1; fi

docs=$(grep -iE '\.(docx|docm|dotx|dotm|doc|odt)$' <<<"$files" | grep -v '^tests/fixtures/' || true)
if [[ -n "$docs" ]]; then echo "Documents outside tests/fixtures/:"; echo "$docs"; fail=1; fi

while IFS= read -r f; do
  [[ -f "$f" ]] || continue
  size=$(wc -c <"$f")
  if (( size > 5000000 )); then echo "File over 5 MB: $f ($size bytes)"; fail=1; fi
done <<<"$files"

if (( fail )); then exit 1; fi
echo "hygiene: OK"
