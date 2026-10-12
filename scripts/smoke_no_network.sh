#!/usr/bin/env bash
# Install the built wheel into a clean Python 3.11 venv and run the CLI with the
# network off. Usage: scripts/smoke_no_network.sh [wheel]   (default: newest dist/*.whl)
set -euo pipefail
wheel=${1:-$(ls -t dist/*.whl | head -n 1)}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
uv venv --quiet --python 3.11 "$work/venv"
uv pip install --quiet --python "$work/venv/bin/python" "$wheel"
py="$work/venv/bin/python"

# Network off: a fresh network namespace when the kernel allows it, otherwise a
# sitecustomize that makes every socket connection fail.
if unshare -rn true 2>/dev/null; then
  isolate=(unshare -rn)
  echo "isolation: network namespace"
else
  mkdir "$work/block"
  cat >"$work/block/sitecustomize.py" <<'PY'
import socket


def _blocked(*args, **kwargs):
    raise OSError("network disabled for smoke test")


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.getaddrinfo = _blocked
PY
  export PYTHONPATH="$work/block"
  isolate=()
  echo "isolation: socket block"
fi

# The isolation must really cut the network, or the smoke test proves nothing.
"${isolate[@]}" "$py" -c '
import socket, sys
try:
    socket.create_connection(("192.0.2.1", 80), timeout=2)
except OSError:
    sys.exit(0)
sys.exit("network is reachable; isolation failed")
'
"${isolate[@]}" "$py" -c 'import sys; assert sys.version_info[:2] == (3, 11), sys.version'
for cmd in capabilities help; do
  out=$("${isolate[@]}" "$work/venv/bin/doc-control" "$cmd")
  "$py" -c 'import json, sys; d = json.loads(sys.argv[1]); assert d["ok"] is True, d' "$out"
  echo "smoke: doc-control $cmd ok"
done
echo "no_network_smoke_failures=0"
