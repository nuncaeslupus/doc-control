"""Live plain writes (D-21) through the fake adapter: dispatch, refusals, history.

The app side (Word, LibreOffice) is covered by ``tests/live/test_plain_live.py``; these
need no app, so they run with the rest of the suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import using
from doc_control.live.protocol import FakeAdapter
from doc_control.model.address import fingerprint
from tests.helpers import make_docx

TEXTS = ["One long.", "Two long.", "Three long."]


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    return make_docx(tmp_path / "novel.docx", *TEXTS)


def at(n: int, span: str = "") -> str:
    return f"p{n + 1}#{fingerprint(TEXTS[n])}{span}"


def live(doc: Path, ops: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    reply = api.run("doc.batch", {"doc": str(doc), "engine": "live", "ops": ops, **extra})
    result = reply.result if hasattr(reply, "result") else reply
    assert isinstance(result, dict)
    return dict(result)


def opened(doc: Path, fake: FakeAdapter) -> Any:
    api.run("live.open", {"doc": str(doc)})
    return fake.docs[str(doc.resolve())]


def test_plain_replace_and_insert_write_untracked(doc: Path) -> None:
    fake = FakeAdapter({str(doc): TEXTS})
    with using(fake):
        state = opened(doc, fake)
        ops = [
            {"op": "text.replace", "at": at(0, ":0-3"), "text": "Uno"},
            {"op": "para.insert", "after": at(2), "text": "Four."},
        ]
        result = live(doc, ops, tracked=False)
        assert result["tracked"] is False and result["applied"] == 2
        assert sorted(state.writes) == [("tracked_insert", False), ("tracked_replace", False)]
        assert state.paragraphs == ["Uno long.", "Two long.", "Three long.", "Four."]
        entries = api.run("history.list", {"doc": str(doc)}).result["entries"]
        assert len(entries) == 2
        assert all(e.get("tracked") is False and e["engine"] == "live" for e in entries)
        # live entries carry no bytes: history.undo never matches a plain live write
        with pytest.raises(DocControlError) as err:
            api.run("history.undo", {"doc": str(doc)})
        assert err.value.code is ErrorCode.HISTORY_NOT_FOUND
        assert state.paragraphs[0] == "Uno long."


def test_tracked_stays_the_default(doc: Path) -> None:
    fake = FakeAdapter({str(doc): TEXTS})
    with using(fake):
        state = opened(doc, fake)
        result = live(doc, [{"op": "text.replace", "at": at(1, ":0-3"), "text": "Dos"}])
        assert "tracked" not in result
        assert state.writes == [("tracked_replace", True)]
        entry = api.run("history.list", {"doc": str(doc)}).result["entries"][0]
        assert "tracked" not in entry


def test_plain_write_next_to_a_tracked_change_is_refused_before_writing(doc: Path) -> None:
    fake = FakeAdapter({str(doc): TEXTS})
    with using(fake):
        state = opened(doc, fake)
        state.revised = {1}
        ops = [
            {"op": "text.replace", "at": at(0, ":0-3"), "text": "Uno"},
            {"op": "text.replace", "at": at(1, ":0-3"), "text": "Dos"},
        ]
        with pytest.raises(DocControlError) as err:
            live(doc, ops, tracked=False)
        assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT
        assert err.value.message == (
            "a plain write cannot touch a paragraph that holds another unresolved tracked "
            "change (1 paragraph(s)); resolve it first or write tracked"
        )
        assert err.value.details["paragraphs"] == ["body #1"]
        with pytest.raises(DocControlError) as err:
            live(doc, [{"op": "para.insert", "after": at(1), "text": "New."}], tracked=False)
        assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT
        assert not state.writes and not fake.dirty(doc)
        assert state.paragraphs == TEXTS
        # tracked, the same edit is fine; plain, a clean paragraph too
        live(doc, ops[1:])
        live(doc, ops[:1], tracked=False)
        assert state.writes == [("tracked_replace", True), ("tracked_replace", False)]


def test_plain_comments_are_refused(doc: Path) -> None:
    fake = FakeAdapter({str(doc): TEXTS})
    with using(fake):
        state = opened(doc, fake)
        with pytest.raises(DocControlError) as err:
            live(doc, [{"op": "comment.add", "at": at(0), "text": "Sure?"}], tracked=False)
        assert err.value.code is ErrorCode.SCHEMA_INVALID
        assert not state.comments


def test_non_boolean_tracked_and_unknown_parameters_are_rejected(doc: Path) -> None:
    fake = FakeAdapter({str(doc): TEXTS})
    ops = [{"op": "text.replace", "at": at(0, ":0-3"), "text": "Uno"}]
    with using(fake):
        state = opened(doc, fake)
        for extra in ({"tracked": "false"}, {"tracked": 0}, {"plain": True}):
            with pytest.raises(DocControlError) as err:
                live(doc, ops, **extra)
            assert err.value.code is ErrorCode.SCHEMA_INVALID, extra
        assert not state.writes


def test_direct_text_replace_live_plain(doc: Path) -> None:
    fake = FakeAdapter({str(doc): TEXTS})
    with using(fake):
        state = opened(doc, fake)
        request = {"doc": str(doc), "engine": "live", "tracked": False}
        api.run("text.replace", {**request, "at": at(2, ":0-5"), "text": "Tres"})
        assert state.writes == [("tracked_replace", False)]
        assert state.paragraphs[2] == "Tres long."


class _PreD21Adapter(FakeAdapter):
    """An adapter written before D-21: it has no ``unresolved``."""

    def __getattribute__(self, name: str) -> Any:
        if name == "unresolved":
            raise AttributeError(name)
        return super().__getattribute__(name)


def test_an_adapter_without_plain_support_answers_engine_unavailable(doc: Path) -> None:
    fake = _PreD21Adapter({str(doc): TEXTS})
    with using(fake):
        state = opened(doc, fake)
        with pytest.raises(DocControlError) as err:
            live(doc, [{"op": "text.replace", "at": at(0, ":0-3"), "text": "Uno"}], tracked=False)
        assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE
        assert state.paragraphs == TEXTS
