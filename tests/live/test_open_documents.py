"""Open documents by name (#193): ``live.status`` lists them, a bare file name finds one."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import using
from doc_control.live.protocol import FakeAdapter, LiveUnavailable
from doc_control.model.address import fingerprint
from tests.helpers import make_docx


def _run(op: str, request: dict[str, Any]) -> dict[str, Any]:
    return api.run(op, request).result


class Other(FakeAdapter):
    app = "other"


@pytest.fixture
def two(tmp_path: Path) -> tuple[Path, Path]:
    """Two documents named alike in different folders, and one with another name."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    return (
        make_docx(tmp_path / "a" / "Novel.docx", "One.", "Two."),
        make_docx(tmp_path / "b" / "Novel.docx", "Uno."),
    )


def _opened(*paths: Path, adapter: FakeAdapter | None = None) -> FakeAdapter:
    fake = adapter or FakeAdapter()
    fake.files.update({str(p.resolve()): ["One.", "Two."] for p in paths})
    for p in paths:
        fake.open(p)
    return fake


def test_status_without_doc_lists_the_open_documents(two: tuple[Path, Path]) -> None:
    first, second = two
    with using(_opened(first), _opened(second, adapter=Other())):
        listed = _run("live.status", {})["documents"]
    assert listed == [
        {"app": "fake", "path": str(first.resolve()), "name": "Novel.docx"},
        {"app": "other", "path": str(second.resolve()), "name": "Novel.docx"},
    ]


def test_an_unreachable_app_lists_nothing(two: tuple[Path, Path]) -> None:
    class Down(FakeAdapter):
        def probe(self) -> dict[str, object]:
            return {"app": "fake", "available": True, "reachable": False, "reason": "down"}

        def documents(self) -> list[Path]:
            raise AssertionError("an unreachable app is not asked")

    with using(Down()):
        assert _run("live.status", {})["documents"] == []


def test_a_bare_name_finds_the_one_open_document(two: tuple[Path, Path]) -> None:
    first, _ = two
    fake = _opened(first)
    with using(fake):
        found = _run("live.status", {"doc": "novel.DOCX" if _folds() else "Novel.docx"})
        assert found["path"] == str(first.resolve())
        assert found["open_in"] == {"app": "fake", "controlled": True}
        reply = _run("live.select", {"doc": "Novel.docx", "at": f"p2#{fingerprint('Two.')}"})
        assert reply["text"] == "Two."
        assert _run("live.get_selection", {"doc": "Novel.docx"})["text"] == "Two."
        _run(
            "doc.batch",
            {
                "doc": "Novel.docx",
                "engine": "live",
                "ops": [
                    {
                        "op": "comment.add",
                        "at": _run("live.get_selection", {"doc": "Novel.docx"})["paragraph"]["at"],
                        "text": "Note.",
                    }
                ],
            },
        )
        assert fake.docs[str(first.resolve())].dirty
        _run("live.save", {"doc": "Novel.docx"})
        assert _run("live.open", {"doc": "Novel.docx"})["path"] == str(first.resolve())
        _run("live.close", {"doc": "Novel.docx"})
    assert not fake.docs


def test_several_documents_of_that_name_are_ambiguous(two: tuple[Path, Path]) -> None:
    first, second = two
    with using(_opened(first), _opened(second, adapter=Other())):
        for op in ("live.status", "live.save", "live.open"):
            with pytest.raises(DocControlError) as err:
                _run(op, {"doc": "Novel.docx"})
            assert err.value.code is ErrorCode.DOC_AMBIGUOUS
            assert err.value.details["candidates"] == sorted(
                [str(first.resolve()), str(second.resolve())]
            )


def test_the_same_document_seen_twice_is_not_ambiguous(two: tuple[Path, Path]) -> None:
    first, _ = two
    with using(_opened(first), _opened(first, adapter=Other())):
        assert _run("live.status", {"doc": "Novel.docx"})["path"] == str(first.resolve())


def test_no_open_document_of_that_name_reads_it_as_a_path(
    two: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = two
    monkeypatch.chdir(tmp_path)
    with using(_opened(first)):
        reply = _run("live.status", {"doc": "Other.docx"})
        assert reply["open_in"] is None
        assert reply["path"] == str((tmp_path / "Other.docx").resolve())
        with pytest.raises(DocControlError) as err:
            _run("live.save", {"doc": "Other.docx"})
        assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE


def test_a_path_is_never_matched_by_name(two: tuple[Path, Path], tmp_path: Path) -> None:
    first, _ = two
    elsewhere = tmp_path / "c" / "Novel.docx"
    with using(_opened(first)):
        reply = _run("live.status", {"doc": str(elsewhere)})
    assert reply["open_in"] is None


def _folds() -> bool:
    return os.path.normcase("A") == "a"


def test_a_file_of_that_name_in_the_working_directory_is_a_candidate(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = two
    monkeypatch.chdir(second.parent)  # Novel.docx here is not the one open
    fake = _opened(first)
    with using(fake):
        with pytest.raises(DocControlError) as err:
            _run("live.close", {"doc": "Novel.docx", "discard": True})
        assert err.value.code is ErrorCode.DOC_AMBIGUOUS
        assert set(err.value.details["candidates"]) == {
            str(first.resolve()),
            str(second.resolve()),
        }
        assert fake.docs  # nothing closed
    monkeypatch.chdir(first.parent)  # the one open is the one here
    with using(_opened(first)):
        assert _run("live.status", {"doc": "Novel.docx"})["path"] == str(first.resolve())


def test_an_app_that_cannot_list_is_not_read_as_nothing_open(two: tuple[Path, Path]) -> None:
    class Busy(FakeAdapter):
        def documents(self) -> list[Path]:
            raise LiveUnavailable("busy")

    with using(Busy()):
        status = _run("live.status", {})
        assert status["documents"] == []
        assert status["apps"][0]["reason"] == "busy"
        with pytest.raises(DocControlError) as err:
            _run("live.save", {"doc": "Novel.docx"})
        assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE
        assert err.value.details["reason"] == "fake: busy"
