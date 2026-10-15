"""Select in view (#196): a cursor mode that cannot replace the passage, and the app's
window brought to the front on select and open."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doc_control import api
from doc_control.live.dispatch import using
from doc_control.live.protocol import FakeAdapter, LiveUnavailable, Span
from doc_control.model.address import fingerprint
from tests.helpers import make_docx


def _run(op: str, request: dict[str, Any]) -> dict[str, Any]:
    return api.run(op, request).result


def _fake(doc: Path) -> FakeAdapter:
    fake = FakeAdapter({str(doc): ["One.", "Two words.", "Three."]})
    fake.open(doc)
    return fake


def test_cursor_mode_leaves_the_cursor_at_the_start(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "One.", "Two words.", "Three.")
    fake = _fake(doc)
    at = f"p2#{fingerprint('Two words.')}"
    with using(fake):
        got = _run("live.select", {"doc": str(doc), "at": at, "mode": "cursor"})
        assert got["text"] == "Two words."
        assert fake.docs[str(doc.resolve())].selection == Span(1, 1, 0, 0)
        assert _run("live.get_selection", {"doc": str(doc)})["text"] == ""
        _run("live.select", {"doc": str(doc), "at": at})
        assert _run("live.get_selection", {"doc": str(doc)})["text"] == "Two words."


def test_select_and_open_bring_the_window_to_the_front(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "One.", "Two words.", "Three.")
    fake = _fake(doc)
    at = f"p1#{fingerprint('One.')}"
    with using(fake):
        assert _run("live.select", {"doc": str(doc), "at": at})["front"] is True
        assert fake.shown == str(doc.resolve())
        del fake.shown
        assert "front" not in _run("live.select", {"doc": str(doc), "at": at, "front": False})
        assert not hasattr(fake, "shown")
        assert "front" not in _run("live.open", {"doc": str(doc), "front": False})
        assert not hasattr(fake, "shown")
        assert _run("live.open", {"doc": str(doc)})["front"] is True
        assert fake.shown == str(doc.resolve())


def test_a_window_that_will_not_come_says_so(tmp_path: Path) -> None:
    class Hidden(FakeAdapter):
        def show(self, path: Path) -> bool:
            return False

    doc = make_docx(tmp_path / "a.docx", "One.")
    fake = Hidden({str(doc): ["One."]})
    with using(fake):
        assert _run("live.open", {"doc": str(doc)})["front"] is False


def test_a_failure_to_come_to_the_front_does_not_fail_the_select(tmp_path: Path) -> None:
    class Busy(FakeAdapter):
        def show(self, path: Path) -> bool:
            raise LiveUnavailable("busy")

    doc = make_docx(tmp_path / "a.docx", "One.")
    fake = Busy({str(doc): ["One."]})
    fake.open(doc)
    with using(fake):
        got = _run("live.select", {"doc": str(doc), "at": f"p1#{fingerprint('One.')}"})
        assert got["front"] is False
        assert _run("live.get_selection", {"doc": str(doc)})["text"] == "One."
