"""LibreOffice edits (LIVE T5): what the live engine writes, the file engine reads back."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.lo_uno import LibreOffice
from tests.corpus.generator import generate
from tests.helpers import make_docx

AUTHOR = "Rev Iewer"
TEXTS = ["First paragraph here.", "Second paragraph stays.", "Third one to change.", "Last."]


def _run(op: str, request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = api.run(op, request).result
    return result


def _read(doc: Path) -> list[dict[str, Any]]:
    with api.open(doc) as handle:
        paragraphs: list[dict[str, Any]] = handle.paragraphs()
    return paragraphs


def test_live_roundtrip_matches_file_engine(live: LibreOffice, lo_home: Path) -> None:
    doc = make_docx(lo_home / "roundtrip.docx", *TEXTS)
    base = {"doc": str(doc)}
    at = [p["at"] for p in _read(doc)]
    _run("live.open", base)
    profile = live._config("/org.openoffice.UserProfile/Data")
    name = (profile.givenname, profile.sn)
    try:
        ops = [
            {"op": "comment.add", "at": f"{at[0]}:0-5", "text": "Why first?"},
            {"op": "para.insert", "after": at[1], "text": "Inserted line."},
            {"op": "text.replace", "at": f"{at[2]}:0-5", "text": "3rd"},
            {"op": "para.insert", "before": at[0], "text": "Title."},
        ]
        done = _run("doc.batch", {**base, "engine": "live", "author": AUTHOR, "ops": ops})
        assert done["applied"] == 4 and done["author"] == AUTHOR
        # The app's own settings are as the writer left them, also after a failure.
        assert (profile.givenname, profile.sn) == name
        assert live._doc(doc).RecordChanges is False
        with pytest.raises(RuntimeError), live._as(live._doc(doc), AUTHOR):
            raise RuntimeError("mid-edit")
        assert (profile.givenname, profile.sn) == name
        assert live._doc(doc).RecordChanges is False
        last = at[3].replace("p4#", "p6#")  # two paragraphs were inserted above it
        refused: list[tuple[dict[str, Any], ErrorCode]] = [
            ({"ops": [{"op": "text.replace", "at": at[2], "text": "x"}]}, ErrorCode.STALE_ADDRESS),
            ({"ops": [{"op": "para.delete", "at": last}]}, ErrorCode.ENGINE_UNAVAILABLE),
            ({"ops": [], "dry_run": True}, ErrorCode.ENGINE_UNAVAILABLE),
            (
                {
                    "ops": [
                        {"op": "text.replace", "at": f"{last}:0-3", "text": "x"},
                        {"op": "comment.add", "at": f"{last}:2-4", "text": "y"},
                    ]
                },
                ErrorCode.BATCH_CONFLICT,
            ),
        ]
        for bad, code in refused:
            with pytest.raises(DocControlError) as err:
                _run("doc.batch", {**base, "engine": "live", **bad})
            assert err.value.code is code, bad
        _run("live.save", base)
    finally:
        _run("live.close", {**base, "discard": True})

    failures = []  # live_roundtrip_failures
    want = ["Title.", *TEXTS[:2], "Inserted line.", "3rd one to change.", TEXTS[3]]
    if [p["text"] for p in _read(doc)] != want:  # unintended_text_changes
        failures.append(("text", [p["text"] for p in _read(doc)]))
    comments = _run("comment.list", base)["comments"]
    if [(c["author"], c["text"]) for c in comments] != [(AUTHOR, "Why first?")]:
        failures.append(("comments", comments))
    revisions = _run("revision.list", base)["revisions"]
    if {r["author"] for r in revisions} != {AUTHOR} or not {"insert", "delete"} <= {
        r["type"] for r in revisions
    }:
        failures.append(("revisions", revisions))
    history = _run("history.list", base)["entries"]
    if [e.get("engine") for e in history[:4]] != ["live"] * 4:
        failures.append(("history", history))
    assert failures == []


def test_a_batch_the_app_cannot_take_changes_nothing(live: LibreOffice, lo_home: Path) -> None:
    """Refusals come before the first edit: the open document stays as it was."""
    doc = lo_home / "refused.docx"
    doc.write_bytes(generate("tables", 1))
    at = [p["at"] for p in _read(doc)]
    base = {"doc": str(doc), "engine": "live"}
    comment = {"op": "comment.add", "at": at[-1], "text": "Sure?"}
    insert = {"op": "para.insert", "after": at[0], "text": "New."}

    def across(a: int, b: int) -> dict[str, Any]:
        return {"op": "text.replace", "at": f"{at[a]}:2..{at[b]}:1", "text": "X"}

    _run("live.open", base)
    try:
        refused: list[tuple[dict[str, Any], ErrorCode]] = [
            # p6 is the heading before the table, p7 and p8 its first two cells.
            ({"ops": [comment, across(5, 6)]}, ErrorCode.UNSUPPORTED_CONTENT),
            ({"ops": [comment, across(6, 7)]}, ErrorCode.UNSUPPORTED_CONTENT),
            ({"ops": [comment, insert, insert]}, ErrorCode.BATCH_CONFLICT),
            ({"ops": [comment], "expect": {"sha256": "0" * 64}}, ErrorCode.ENGINE_UNAVAILABLE),
        ]
        for request, code in refused:
            with pytest.raises(DocControlError) as err:
                _run("doc.batch", {**base, **request})
            assert err.value.code is code, request
            assert _run("live.status", base)["dirty"] is False
    finally:
        _run("live.close", {**base, "discard": True})
