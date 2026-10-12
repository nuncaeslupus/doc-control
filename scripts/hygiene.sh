#!/usr/bin/env bash
# Repo hygiene gate: no Word lock files, no real documents outside test
# fixtures, no oversized files. Runs locally (host-gate) and in CI.
set -euo pipefail
fail=0
shopt -s nocasematch

while IFS= read -r -d '' f; do
  name=${f##*/}
  if [[ $name == '~$'* ]]; then echo "Word lock file tracked: $f"; fail=1; fi
  if [[ $f =~ \.(docx|docm|dotx|dotm|doc|odt)$ && $f != tests/fixtures/* ]]; then
    echo "Document outside tests/fixtures/: $f"; fail=1
  fi
  if [[ -f $f ]]; then
    size=$(wc -c <"$f")
    if (( size > 5000000 )); then echo "File over 5 MB: $f ($size bytes)"; fail=1; fi
  fi
done < <(git ls-files -z)

if (( fail )); then exit 1; fi
echo "hygiene: OK"
