"""Live engine dispatch (LIVE T1): routing, the fake adapter, and a machine with no app."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_control import api
from doc_control.cli import execute
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import using
from doc_control.live.protocol import FakeAdapter, LiveUnavailable
from doc_control.registry import load_operations
from tests.helpers import make_docx


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    return make_docx(tmp_path / "novel.docx", "One.", "Two.")


def _live_calls(doc: Path) -> list[tuple[str, dict[str, object]]]:
    """Every live operation, plus ``batch`` and a file-only operation asked to run live."""
    ops = load_operations()
    calls: list[tuple[str, dict[str, object]]] = [
        (name, {"doc": str(doc)}) for name in ops if name.startswith("live.")
    ]
    assert len(calls) >= 4
    calls.append(("batch", {"doc": str(doc), "engine": "live", "ops": []}))
    calls.append(("para.get", {"doc": str(doc), "engine": "live", "at": "p1#00000000"}))
    return calls


def test_live_call_without_app_returns_engine_unavailable(doc: Path) -> None:
    registry = load_operations()
    tracebacks = 0
    with using():
        for op, request in _live_calls(doc):
            envelope, _ = execute(registry, op, request)
            if op == "live.status":
                assert envelope["result"] == {"apps": [], "open_in": None, "dirty": False}
                continue
            error = envelope["error"]
            if error["code"] != "ENGINE_UNAVAILABLE" or not error["details"]["reason"]:
                tracebacks += 1
        assert execute(registry, "capabilities", {})[0]["result"]["live"] == {"apps": []}
    assert tracebacks == 0  # live_unavailable_tracebacks


def test_unreachable_app_gives_its_reason(doc: Path) -> None:
    class Down(FakeAdapter):
        def probe(self) -> dict[str, object]:
            return {"app": "fake", "available": True, "reachable": False, "reason": "not running"}

    with using(Down()), pytest.raises(DocControlError) as err:
        api.run("live.open", {"doc": str(doc)})
    assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE
    assert err.value.details["reason"] == "fake: not running"


def test_app_that_dies_mid_call_is_engine_unavailable(doc: Path) -> None:
    class Dies(FakeAdapter):
        def save(self, path: Path) -> None:
            raise LiveUnavailable("connection to fake lost")

    with using(Dies()), api.open(doc, engine="live") as handle:
        with pytest.raises(DocControlError) as err:
            handle.run("live.save")
        assert err.value.details["reason"] == "connection to fake lost"


def test_open_save_close_through_the_handle(doc: Path) -> None:
    fake = FakeAdapter({str(doc): ["One.", "Two."]})
    with using(fake), api.open(doc, engine="live", app="fake") as handle:
        assert handle.run("live.status")["open_in"] == {"app": "fake", "controlled": True}
        fake.tracked_insert(doc, 1, "Three.", "Claude")
        assert handle.run("live.status")["dirty"] is True
        with pytest.raises(DocControlError) as err:
            handle.run("live.close")
        assert err.value.code is ErrorCode.UNSAVED_CHANGES
        assert fake.is_open(doc) and fake.dirty(doc)  # unsaved_work_lost == 0
        assert handle.run("live.close", {"save": True}) == {"closed": True, "saved": True}
        assert fake.files[str(doc.resolve())] == ["One.", "Two.", "Three."]
        with pytest.raises(DocControlError) as err:
            handle.run("live.save")
        assert "not open" in err.value.details["reason"]


def test_close_discard_drops_changes(doc: Path) -> None:
    fake = FakeAdapter({str(doc): ["One."]})
    with using(fake):
        api.run("live.open", {"doc": str(doc)})
        fake.tracked_insert(doc, 0, "Two.", "Claude")
        api.run("live.close", {"doc": str(doc), "discard": True})
    assert fake.files[str(doc.resolve())] == ["One."] and not fake.is_open(doc)


def test_unknown_app_and_missing_file(doc: Path) -> None:
    with using(FakeAdapter()):
        with pytest.raises(DocControlError) as err:
            api.run("live.open", {"doc": str(doc), "app": "word"})
        assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE
        with pytest.raises(DocControlError) as err:
            api.run("live.open", {"doc": str(doc.with_name("missing.docx"))})
        assert err.value.code is ErrorCode.DOC_NOT_FOUND


def test_a_broken_app_is_reported_not_raised(doc: Path) -> None:
    class Broken(FakeAdapter):
        def probe(self) -> dict[str, object]:
            raise OSError("no such program")

    with using(Broken()):
        apps = api.run("live.status", {}).result["apps"]
    assert apps == [
        {
            "app": "fake",
            "available": False,
            "reachable": False,
            "version": None,
            "reason": "no such program",
        }
    ]
