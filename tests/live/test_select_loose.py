"""``live.select`` match levels (FIND T5): the ladder over the joined text, exact first.

Gate: ``exact_tiebreak_violations == 0``. One verbatim paragraph beats any number of looser
ones; two hits at the first level that has any are ambiguous; the reply names the level.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator
from itertools import accumulate
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import using
from doc_control.live.protocol import FakeAdapter
from doc_control.model.address import Address, fingerprint
from tests.corpus.generator import KINDS, generate
from tests.helpers import make_docx

QUESTION = "-¿Estás ahí? -dijo."


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    return make_docx(tmp_path / "novel.docx", "One.")


def _select(doc: Path, texts: list[str], **ask: Any) -> dict[str, Any]:
    with using(FakeAdapter({str(doc): texts})):
        api.run("live.open", {"doc": str(doc)})
        result: dict[str, Any] = api.run(
            "live.select", {"doc": str(doc), "front": False, **ask}
        ).result
        return result


def _refused(doc: Path, texts: list[str], **ask: Any) -> DocControlError:
    with pytest.raises(DocControlError) as err:
        _select(doc, texts, **ask)
    return err.value


def _at(texts: list[str], n: int) -> str:
    return str(Address(n + 1, fingerprint(texts[n])))


def test_select_prefers_verbatim_paragraph(doc: Path) -> None:
    texts = [QUESTION, "Estas ahi, le dijo.", "¿ESTAS AHI?"]
    got = _select(doc, texts, find_phrase="Estas ahi")
    assert got == {"text": texts[1], "at": _at(texts, 1), "match_level": "exact"}
    # The verbatim paragraph wins wherever it sits.
    texts = ["«Estás ahí»", "Estas ahi", QUESTION]
    assert _select(doc, texts, find_phrase="Estas ahi")["at"] == _at(texts, 1)


def test_select_reports_folded_level_and_matched_text(doc: Path) -> None:
    texts = ["Lead.", QUESTION]
    got = _select(doc, texts, find_phrase="Estas ahi")
    assert got == {
        "text": QUESTION,
        "at": _at(texts, 1),
        "match_level": "default",
        "matched_text": "Estás ahí",
    }
    got = _select(doc, texts, find_phrase="ESTÁS AHÍ")
    assert (got["match_level"], got["matched_text"]) == ("case", "Estás ahí")


def test_select_same_level_twice_is_ambiguous(doc: Path) -> None:
    texts = [QUESTION, "Lead.", "¿Estas ahí?"]
    err = _refused(doc, texts, find_phrase="Estas ahi")
    assert err.code is ErrorCode.ANCHOR_AMBIGUOUS
    assert err.details == {
        "match_level": "default",
        "candidates": [
            {"at": _at(texts, 0), "matched_text": "Estás ahí"},
            {"at": _at(texts, 2), "matched_text": "Estas ahí"},
        ],
    }  # tier_ambiguity_misses == 0
    # At most five candidates are listed.
    err = _refused(doc, ["¿Estas ahi?"] * 7, find_phrase="estas ahi")
    assert err.details["match_level"] == "case" and len(err.details["candidates"]) == 5


def test_select_reports_retry_level(doc: Path) -> None:
    texts = ["Lead.", "Cuesta 5$ dólares."]
    for level in ("default", "loose"):
        got = _select(doc, texts, find_phrase="5 dolares", match_level=level)
        assert (got["at"], got["match_level"]) == (_at(texts, 1), "loose")
        assert got["matched_text"] == "5$ dólares"  # fallback_level_misreports == 0
    err = _refused(doc, texts, find_phrase="unicornio")
    assert err.code is ErrorCode.ANCHOR_NOT_FOUND
    assert err.details == {"tried": ["exact", "case", "default", "loose"]}
    err = _refused(doc, texts, find_phrase="5 dolares", match_level="exact")
    assert err.code is ErrorCode.ANCHOR_NOT_FOUND and err.details == {"tried": ["exact"]}


def test_select_folded_phrase_across_marks(doc: Path) -> None:
    texts = ["Fin.", QUESTION, "Otra."]
    assert _select(doc, texts, find_phrase="fin^pestas ahi")["at"] == _at(texts, 0)
    assert _select(doc, texts, find_phrase="—dijo\rotra")["at"] == _at(texts, 1)
    got = _select(doc, texts, find_phrase="dijo\rOtra", find_phrase_across="previous")
    assert (got["at"], got["match_level"]) == (_at(texts, 2), "default")
    assert _select(doc, texts, find_phrase="otra", paragraphs_after=0)["at"] == _at(texts, 2)


def test_select_tries_a_caret_phrase_raw_first(doc: Path) -> None:
    texts = ["x^2 grows.", "x\u00a02 grows."]
    got = _select(doc, texts, find_phrase="x^2")
    assert (got["at"], got["match_level"]) == (_at(texts, 0), "exact")
    got = _select(doc, texts, find_phrase="x^s2")
    assert (got["at"], got["match_level"]) == (_at(texts, 1), "exact")


def test_select_phrase_of_punctuation_only_is_exact_only(doc: Path) -> None:
    assert _select(doc, ["Lead.", "--Hola"], find_phrase="--")["match_level"] == "exact"
    err = _refused(doc, ["Lead.", "—Hola"], find_phrase="--")
    assert err.code is ErrorCode.SCHEMA_INVALID


def _today(texts: list[str], phrase: str, across: str | None) -> int | ErrorCode:
    """``live.select``'s ``_found`` before match levels, as a reference."""
    if across and "\r" not in phrase:
        return ErrorCode.SCHEMA_INVALID
    folded = [text.casefold() for text in texts]
    joined, needle = "\r".join(folded) + "\r", phrase.casefold()
    at = joined.find(needle)
    if at < 0:
        return ErrorCode.ANCHOR_NOT_FOUND
    if joined.find(needle, at + 1) >= 0:
        return ErrorCode.ANCHOR_AMBIGUOUS
    starts = list(accumulate((len(text) + 1 for text in folded), initial=0))
    return bisect_right(starts, at) - 1 + (across in ("previous", "both") or needle[0] == "\r")


def _live_fixtures(tmp: Path) -> Iterator[tuple[list[str], str, str | None]]:
    """The documents and phrases of the LIVE select tests, and a sample of corpus rows."""
    refuse = ["Now: One day.", "Twice.", "Twice.", "The end."]
    for phrase in ["nowhere", "twice.", "the END.", "Twice.\rTwice.\rThe", "One DAY"]:
        yield refuse, phrase, None
    yield refuse, "Twice.\rThe", "previous"
    yield refuse, "Twice.", "previous"
    deleted = ["Alpha charlie.", "Other text here.", "Zed end.", "Quiet tail."]
    for phrase in ["alpha charlie", "Quiet tail", "hidden unicorn", "Other"]:
        yield deleted, phrase, None
    yield deleted, "Other", "previous"
    for kind in KINDS:
        path = tmp / f"{kind}.docx"
        path.write_bytes(generate(kind, 0))
        with api.open(path) as handle:
            texts = [p["text"] for p in handle.paragraphs()]
            rows = handle.run("locate", {})["paragraphs"]
        step = max(1, len(rows) // 8)
        for n, row in list(enumerate(rows))[::step]:
            if row.get("find_phrase"):
                yield texts, row["find_phrase"], row.get("find_phrase_across")
            if texts[n]:
                yield texts, texts[n][:12], None


def test_select_exact_level_is_todays_rule(doc: Path, tmp_path: Path) -> None:
    differences = []
    for texts, phrase, across in _live_fixtures(tmp_path):
        want = _today(texts, phrase, across)
        ask: dict[str, Any] = {"find_phrase": phrase, "match_level": "exact"}
        if across:
            ask["find_phrase_across"] = across
        try:
            got: int | ErrorCode = _select(doc, texts, **ask)["at"]
        except DocControlError as err:
            got = err.code
        if isinstance(want, int):
            want_at: int | ErrorCode | str = _at(texts, want)
        else:
            want_at = want
        if got != want_at:
            differences.append((phrase, want_at, got))
    assert differences == []
