"""The freezer spike's measurement script reports every metric for each candidate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging" / "spike" / "measure.py"

# A stand-in for a frozen doc-control: `server.capabilities` prints an ok envelope, `mcp` answers
# newline-delimited JSON-RPC on stdio like `doc_control.mcp.serve`.
STUB = r"""
import json, sys

if sys.argv[1:] == ["server.capabilities"]:
    print(json.dumps({"ok": True, "op": "server.capabilities", "result": {}}))
    sys.exit(0)
if sys.argv[1:] == ["mcp"]:
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif msg["method"] == "tools/list":
            result = {"tools": [{"name": "server_capabilities", "inputSchema": {}}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()
    sys.exit(0)
sys.exit(2)
"""


def _fake_build(root: Path, name: str, padding: int) -> Path:
    """A one-folder build: the stub, a wrapper exe that launches it, and some payload."""
    dist = root / name / "doc-control"
    dist.mkdir(parents=True)
    (dist / "stub.py").write_text(STUB, encoding="utf-8")
    (dist / "payload.bin").write_bytes(b"\0" * padding)
    exe = dist / "doc-control"
    exe.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/stub.py" "$@"\n', encoding="utf-8"
    )
    exe.chmod(0o755)
    return dist


@pytest.mark.skipif(os.name == "nt", reason="the stub exe is a POSIX shell wrapper")
def test_measure_reports_every_metric_per_candidate(tmp_path: Path) -> None:
    first = _fake_build(tmp_path, "pyinstaller", 2 * 1024 * 1024)
    second = _fake_build(tmp_path, "nuitka", 1024 * 1024)
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--exe",
            "doc-control",
            "--runs",
            "3",
            "--build-minutes",
            "pyinstaller=1.5",
            "--out",
            str(out),
            f"pyinstaller={first}",
            f"nuitka={second}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    printed = json.loads(proc.stdout)
    assert json.loads(out.read_text(encoding="utf-8")) == printed
    candidates = printed["candidates"]
    assert set(candidates) == {"pyinstaller", "nuitka"}
    for name, row in candidates.items():
        for metric in ("dist_mb", "startup_ms_median", "smoke_failures", "build_minutes"):
            assert metric in row, (name, metric)
        assert row["smoke_failures"] == 0, (name, row)
        assert row["startup_ms_median"] > 0
    assert candidates["pyinstaller"]["dist_mb"] > candidates["nuitka"]["dist_mb"] >= 1.0
    assert candidates["pyinstaller"]["build_minutes"] == 1.5
    assert candidates["nuitka"]["build_minutes"] is None
    assert printed["freezer_candidates_measured"] == 2
