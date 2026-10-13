"""Lock awareness (LIVE T2): which app holds a document, and whether the lock is left over."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import using
from doc_control.live.protocol import FakeAdapter, LiveUnavailable
from doc_control.safety import lock as lock_module
from doc_control.safety.lock import check_unlocked, lock_info
from tests.helpers import make_docx

HERE = socket.gethostname()

# (lock file name, its content, LibreOffice running here?, app, stale)
KINDS = [
    ("~$Novela.docx", "owner", False, "word", None),
    ("~$Novela.docx", "owner", True, "word", None),
    (
        ".~lock.Novela.docx#",
        f",ana,{HERE},13.10.2026 17:26,file:///x;",
        True,
        "libreoffice",
        False,
    ),
    (
        ".~lock.Novela.docx#",
        f",ana,{HERE},13.10.2026 17:26,file:///x;",
        False,
        "libreoffice",
        True,
    ),
    (
        ".~lock.Novela.docx#",
        ",ana,another-machine,13.10.2026 17:26,file:///x;",
        False,
        "libreoffice",
        None,
    ),
    (".~lock.Novela.docx#", f",ana,{HERE},13.10.2026 17:26,file:///x;", None, "libreoffice", None),
    (".~lock.Novela.docx#", "", False, "libreoffice", None),
]


def test_lock_kinds_report_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    assert lock_info(doc) is None
    misreports = 0
    for name, content, running, app, stale in KINDS:
        monkeypatch.setattr(lock_module, "_libreoffice_running", lambda r=running: r)
        lock = tmp_path / name
        lock.write_text(content, encoding="utf-8")
        want = {"app": app, "lock_file": str(lock), "stale": stale}
        with pytest.raises(DocControlError) as err:
            check_unlocked(doc)
        named = ("Word" if app == "word" else "LibreOffice") in err.value.message
        with using(FakeAdapter()):
            status = api.run("live.status", {"doc": str(doc)}).result
        open_in = {"app": app, "controlled": False, "stale": stale}
        if (
            lock_info(doc) != want
            or err.value.code is not ErrorCode.DOC_LOCKED
            or {k: err.value.details[k] for k in want} != want
            or not named
            or status["open_in"] != open_in
        ):
            misreports += 1
        lock.unlink()
    assert misreports == 0  # live_lock_misreports


def test_a_controlled_window_wins_over_its_lock_file(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    (tmp_path / ".~lock.Novela.docx#").write_text(f",ana,{HERE},d,p;", encoding="utf-8")
    with using(FakeAdapter()):
        api.run("live.open", {"doc": str(doc)})
        status = api.run("live.status", {"doc": str(doc)}).result
    assert status["open_in"] == {"app": "fake", "controlled": True}


def test_running_check_reads_this_machine() -> None:
    assert lock_module._libreoffice_running() in (True, False, None)


def test_status_survives_an_app_that_vanishes(tmp_path: Path) -> None:
    class Vanishes(FakeAdapter):
        def is_open(self, path: Path) -> bool:
            raise LiveUnavailable("connection to fake lost")

    doc = make_docx(tmp_path / "gone.docx", "One.")
    with using(Vanishes()):
        assert api.run("live.status", {"doc": str(doc)}).result["open_in"] is None
