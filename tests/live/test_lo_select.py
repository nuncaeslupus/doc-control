"""LibreOffice select (LIVE T4): by address and by find phrase, against the file engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.lo_uno import LibreOffice
from tests.corpus.generator import KINDS, generate
from tests.helpers import make_docx

SAMPLES = 8


def _run(op: str, request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = api.run(op, request).result
    return result


def _rows(doc: Path) -> list[dict[str, Any]]:
    """The ``locate`` row and file-engine text of a sample of paragraphs."""
    with api.open(doc) as handle:
        paragraphs = handle.paragraphs()

    def locate(at: str) -> dict[str, Any]:
        row: dict[str, Any] = _run("locate", {"doc": str(doc), "at": at})["paragraphs"][0]
        return row

    rows = []
    # LibreOffice recomputes a date field on load, so that paragraph reads differently in
    # the app than in the file: live.select answers STALE_ADDRESS for it, rightly.
    paragraphs = [p for p in paragraphs if not p["text"].startswith("Dated ")]
    step = max(1, len(paragraphs) // SAMPLES)
    for paragraph in [*paragraphs[::step], paragraphs[-1]]:
        row = {**locate(paragraph["at"]), "text": paragraph["text"]}
        if "context" in row:
            row["context"]["across"] = locate(row["context"]["at"]).get("find_phrase_across")
        rows.append(row)
    return rows


@pytest.mark.parametrize("kind", KINDS)
def test_select_matches_file_engine_text(live: LibreOffice, lo_home: Path, kind: str) -> None:
    doc = lo_home / f"select-{kind}.docx"
    doc.write_bytes(generate(kind, 0))
    rows = _rows(doc)
    base = {"doc": str(doc)}
    _run("live.open", base)
    mismatches: list[tuple[str, str]] = []
    try:
        for row in rows:
            want = {"text": row["text"], "at": row["at"]}
            asks: dict[str, dict[str, Any]] = {"at": {"at": row["at"]}}
            if row.get("find_phrase"):
                asks["find"] = {"find_phrase": row["find_phrase"]}
                if "find_phrase_across" in row:
                    asks["find"]["find_phrase_across"] = row["find_phrase_across"]
            elif "context" in row:
                context = row["context"]
                asks["context"] = {
                    "find_phrase": context["after"],
                    "paragraphs_after": context["paragraphs_after"],
                }
                if context["across"]:
                    asks["context"]["find_phrase_across"] = context["across"]
            for how, ask in asks.items():
                try:
                    got = _run("live.select", {**base, **ask})
                    seen = _run("live.selection", base)
                except DocControlError as err:
                    mismatches.append((f"{how} {row['at']}", err.code.value))
                    continue
                if got != want or seen != {"text": want["text"], "paragraph": want}:
                    mismatches.append((f"{how} {row['at']}", got["text"][:40]))
    finally:
        _run("live.close", {**base, "discard": True})
    assert mismatches == []  # live_select_mismatches


def test_stale_address_and_missing_phrase_are_refused(live: LibreOffice, lo_home: Path) -> None:
    doc = make_docx(lo_home / "refuse.docx", "One day.", "Twice.", "Twice.", "The end.")
    base = {"doc": str(doc)}
    with api.open(doc) as handle:
        first = handle.paragraphs()[0]["at"]
    _run("live.open", base)
    try:
        assert (
            _run("live.select", {**base, "at": first, "paragraphs_after": 3})["text"] == "The end."
        )
        body = live._doc(doc).Text
        body.insertString(body.getStart(), "Now: ", False)  # the writer types
        for ask, code in [
            ({"at": first}, ErrorCode.STALE_ADDRESS),
            ({"at": "p9#00000000"}, ErrorCode.STALE_ADDRESS),
            ({"find_phrase": "nowhere"}, ErrorCode.ANCHOR_NOT_FOUND),
            ({"find_phrase": "twice."}, ErrorCode.ANCHOR_AMBIGUOUS),
            ({"find_phrase": "the END.", "paragraphs_after": 1}, ErrorCode.ANCHOR_NOT_FOUND),
            ({}, ErrorCode.SCHEMA_INVALID),
        ]:
            with pytest.raises(DocControlError) as err:
                _run("live.select", {**base, **ask})
            assert err.value.code is code, ask
        # A phrase across the mark between the two repeated paragraphs names the second.
        got = _run("live.select", {**base, "find_phrase": "Twice.\rTwice.\rThe"})
        assert got["at"].startswith("p2#")
        got = _run(
            "live.select",
            {**base, "find_phrase": "Twice.\rThe", "find_phrase_across": "previous"},
        )
        assert got["at"].startswith("p4#")
    finally:
        _run("live.close", {**base, "discard": True})
