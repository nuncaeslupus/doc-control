"""LibreOffice live session (LIVE T3): open, save, close and the unsaved-work guard."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live import lo_uno
from doc_control.live.dispatch import using
from doc_control.live.lo_uno import LibreOffice
from doc_control.live.protocol import LiveAdapter
from doc_control.ooxml.zipio import sha256_file
from tests.helpers import make_docx, symlink_or_skip


def _run(op: str, request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = api.run(op, request).result
    return result


def _type(adapter: LibreOffice, doc: Path, text: str) -> None:
    """What the writer does: add text at the end of the open document."""
    body = adapter._doc(doc).Text
    body.insertString(body.getEnd(), text, False)


def _texts(doc: Path) -> list[str]:
    with api.open(doc) as handle:
        return [p["text"] for p in handle.paragraphs()]


def test_close_dirty_without_flag_refused(live: LibreOffice, lo_home: Path) -> None:
    doc = make_docx(lo_home / "dirty.docx", "One.", "Two.")
    request = {"doc": str(doc)}
    assert _run("live.status", request)["open_in"] is None
    assert _run("live.open", request) == {
        "app": "libreoffice",
        "path": str(doc.resolve()),
        "dirty": False,
    }
    assert _run("live.open", request)["app"] == "libreoffice"  # attaches, no second window
    _type(live, doc, " More.")

    with pytest.raises(DocControlError) as err:
        _run("live.close", request)
    assert err.value.code is ErrorCode.UNSAVED_CHANGES
    status = _run("live.status", request)
    assert status["open_in"] == {"app": "libreoffice", "controlled": True}
    assert status["dirty"] is True  # unsaved_work_lost == 0
    assert _texts(doc) == ["One.", "Two."]

    assert _run("live.close", {**request, "save": True}) == {"closed": True, "saved": True}
    assert _run("live.status", request)["open_in"] is None
    assert _texts(doc) == ["One.", "Two. More."]


def test_save_keeps_it_open_and_discard_drops_changes(live: LibreOffice, lo_home: Path) -> None:
    doc = make_docx(lo_home / "discard.docx", "One.")
    request = {"doc": str(doc)}
    _run("live.open", request)
    _type(live, doc, " Kept.")
    assert _run("live.save", request) == {"saved": True}
    assert _run("live.status", request)["dirty"] is False
    saved = sha256_file(doc)
    _type(live, doc, " Dropped.")
    assert _run("live.close", {**request, "discard": True})["saved"] is False
    assert sha256_file(doc) == saved
    # Reopen: the saved text is what the app shows.
    _run("live.open", request)
    assert live._doc(doc).Text.getString() == "One. Kept."
    _run("live.close", request)


def test_a_document_opened_through_a_symlink_is_found_by_its_target(
    live: LibreOffice, lo_home: Path
) -> None:
    doc = make_docx(lo_home / "target.docx", "One.")
    link = lo_home / "link.docx"
    symlink_or_skip(link, doc)
    _run("live.open", {"doc": str(link)})
    try:
        assert live.is_open(doc)
        assert _run("live.open", {"doc": str(doc)})["app"] == "libreoffice"  # no second window
        assert sum(1 for _ in _windows(live)) == 1
    finally:
        _run("live.close", {"doc": str(doc), "discard": True})
    assert not live.is_open(link)


def test_open_documents_are_listed_and_found_by_name(live: LibreOffice, lo_home: Path) -> None:
    doc = make_docx(lo_home / "Named.docx", "One.")
    _run("live.open", {"doc": str(doc)})
    try:
        listed = _run("live.status", {})["documents"]
        assert {"app": "libreoffice", "path": str(doc.resolve()), "name": "Named.docx"} in listed
        status = _run("live.status", {"doc": "Named.docx"})
        assert status["path"] == str(doc.resolve())
        assert status["open_in"] == {"app": "libreoffice", "controlled": True}
    finally:
        _run("live.close", {"doc": "Named.docx", "discard": True})
    assert not live.is_open(doc)


def _windows(adapter: LibreOffice) -> list[Any]:
    found = adapter._desktop().Components.createEnumeration()
    out = []
    while found.hasMoreElements():
        component = found.nextElement()
        if getattr(component, "URL", "").startswith("file:"):
            out.append(component)
    return out


def test_no_instance_and_no_soffice_is_engine_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOC_CONTROL_LO_PORT", "1")
    monkeypatch.setattr(lo_uno, "_soffice", lambda: None)
    doc = make_docx(tmp_path / "a.docx", "One.")
    adapter = LibreOffice()
    probe = adapter.probe()
    assert probe["reachable"] is False and probe["reason"]
    with using(cast(LiveAdapter, adapter)):
        assert _run("live.status", {"doc": str(doc)})["open_in"] is None
        with pytest.raises(DocControlError) as err:
            _run("live.open", {"doc": str(doc)})
    assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE


def test_probe_never_loads_the_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """pyuno aborts the process in a Python it was not built for: the probe only looks."""

    def loaded() -> None:
        raise AssertionError("probe imported uno")

    monkeypatch.setattr(lo_uno, "_uno", loaded)
    with using(cast(LiveAdapter, LibreOffice())):
        found = api.run("capabilities", {}).result["live"]["apps"][0]
    assert found["app"] == "libreoffice" and "probe imported uno" not in found.get("reason", "")

    # A bridge built for another Python is a reason, found without importing it.
    (tmp_path / "uno.py").write_text("raise SystemExit('imported')\n")
    (tmp_path / "libpyuno.so").write_bytes(b"\0libpython3.99.so.1.0\0")
    monkeypatch.setenv("DOC_CONTROL_LO_PROGRAM", str(tmp_path))
    monkeypatch.setattr(lo_uno, "_PROGRAM_DIRS", ())
    monkeypatch.setattr(lo_uno.importlib.util, "find_spec", lambda name: None)
    monkeypatch.delitem(sys.modules, "uno", raising=False)
    probe = LibreOffice().probe()
    assert probe["reachable"] is False and "needs Python 3.99" in probe["reason"]
    monkeypatch.setenv("DOC_CONTROL_LO_PORT", "soon")
    monkeypatch.setattr(lo_uno, "_bridge", lambda: None)
    assert "not a port number" in LibreOffice().probe()["reason"]
