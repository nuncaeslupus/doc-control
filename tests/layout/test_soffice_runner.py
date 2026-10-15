"""The headless LibreOffice runner behind `field.update`, without a real LibreOffice: a
timeout ends the whole process tree, and REF results LibreOffice cannot compute are not
written."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml import soffice
from doc_control.ops import fieldwrite
from tests.layout.test_fields import _build, _field, _paragraph, _run

REF_ERROR = "Error: Reference source not found"


def _gone(pid: int) -> bool:
    """Whether ``pid`` has ended (a zombie nobody reaps counts as ended)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_timeout_kills_whole_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "grandchild.pid"
    fake = tmp_path / "soffice"
    # A launcher that starts a grandchild (as soffice starts oosplash and soffice.bin) and
    # hangs; the grandchild holds stderr open, so killing only the launcher is not enough.
    fake.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(pid_file)!r}, "w").write(str(child.pid))
            time.sleep(60)
            """
        )
    )
    fake.chmod(0o755)
    monkeypatch.setattr(soffice, "TIMEOUT", 2)
    started = time.monotonic()
    with pytest.raises(DocControlError) as caught:
        soffice._run(
            str(fake), tmp_path / "profile", "--terminate_after_init", env=dict(os.environ)
        )
    assert caught.value.code is ErrorCode.ENGINE_UNAVAILABLE
    assert "did not finish within 2s" in str(caught.value)
    assert time.monotonic() - started < 30
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while not _gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _gone(grandchild)


class _FakeProc:
    """A ``Popen`` stand-in whose ``communicate`` raises what it is given, in turn."""

    def __init__(self, raises: list[BaseException]) -> None:
        self.pid = 4321
        self.stdout = None
        self.stderr = io.StringIO()
        self.raises = raises
        self.killed = 0
        self.waited: list[float | None] = []

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self.raises:
            raise self.raises.pop(0)
        return "", ""

    def kill(self) -> None:
        self.killed += 1

    def wait(self, timeout: float | None = None) -> int:
        self.waited.append(timeout)
        if timeout is not None and len(self.waited) == 1:
            raise subprocess.TimeoutExpired("soffice", timeout)
        return -9


def test_timeout_kills_whole_tree_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(soffice.sys, "platform", "win32")
    monkeypatch.setattr(soffice.subprocess, "run", fake_run)
    timeout = subprocess.TimeoutExpired("soffice", 1)
    for raises, reaped in (
        ([timeout, subprocess.TimeoutExpired("soffice", 10)], [10, None]),
        ([KeyboardInterrupt()], [10, None]),
    ):
        proc = _FakeProc(list(raises))
        monkeypatch.setattr(soffice.subprocess, "Popen", lambda *a, _p=proc, **k: _p)
        calls.clear()
        with pytest.raises((DocControlError, KeyboardInterrupt)):
            soffice._run("soffice.exe", tmp_path / "profile", env={})
        assert calls == [["taskkill", "/T", "/F", "/PID", "4321"]]
        # The launcher is killed too, and the process is waited for: a bounded wait, then,
        # after a second kill, an unbounded one.
        assert proc.killed == 2 and proc.waited == reaped


def _fresh_docx(tmp_path: Path, *results: str) -> bytes:
    """What LibreOffice hands back: the same REF fields with these results."""
    body = "".join(_paragraph(_field(f" REF _Ref{i} \\h ", r)) for i, r in enumerate(results))
    return _build(tmp_path / "fresh.docx", body).read_bytes()


def test_ref_results_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    olds = ("Table 1", "Figure 2", "WRONG")
    body = "".join(
        _paragraph(_run(f"See {i}: "), _field(f" REF _Ref{i} \\h ", old))
        for i, old in enumerate(olds)
    )
    doc = _build(tmp_path / "doc.docx", body)
    fresh = _fresh_docx(tmp_path, REF_ERROR, "", "Table 3")
    monkeypatch.setattr(fieldwrite, "soffice_path", lambda: "soffice")
    monkeypatch.setattr(fieldwrite, "refresh_fields", lambda path, exe: fresh)
    out = api.run("field.update", {"doc": str(doc)}).result
    assert [(u["before"], u["after"]) for u in out["updated"]] == [(["WRONG"], ["Table 3"])]
    reasons = [s["reason"] for s in out["skipped"] if s["type"] == "REF"]
    assert len(reasons) == 2
    assert "an error" in reasons[0] and "an empty result" in reasons[1]
    rows = api.run("field.list", {"doc": str(doc)}).result["fields"]
    assert [r["result"] for r in rows if r["type"] == "REF"][:2] == ["Table 1", "Figure 2"]
    with zipfile.ZipFile(doc) as zf:
        xml = zf.read("word/document.xml").decode()
    assert REF_ERROR not in xml
