"""The CLI speaks UTF-8 on stdio whatever the locale (Windows pipes default to cp1252)."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _run(*args: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    return subprocess.run(
        [sys.executable, "-m", "doc_control", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_cli_stdout_is_utf8_under_cp1252_locale() -> None:
    proc = _run("¿estás-ahí")
    assert proc.returncode == 2, proc.stderr
    assert "¿estás-ahí" in json.loads(proc.stdout.decode("utf-8"))["error"]["message"]
    proc = _run("capabilities")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.decode("utf-8"))["ok"] is True
