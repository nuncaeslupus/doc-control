"""`history.search` with `match_level` (FIND T8): folded hits, exact offsets, each tagged.

Without ``match_level`` a hit whose folding changed the text's length (``ß`` casefolds to
``ss``) has no reliable offset, so its ``text`` starts at the paragraph's start. With it,
each hit is cut from its own place: ``start`` and ``end`` index the searched text, and
``text`` shows the hit and what surrounds it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import build_index
from doc_control.ooxml.zipio import Package
from doc_control.ops import history as history_ops
from doc_control.ops.batch import batch
from tests.helpers import make_docx

LONG = "Straße " * 20 + "-¿Estás ahí? -dijo María, y Estas ahi otra vez."

search = cast("Callable[[dict[str, Any]], Reply]", history_ops.history_search)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_CONTROL_STORE", raising=False)
    monkeypatch.delenv("DOC_CONTROL_HOME", raising=False)


def logged(tmp_path: Path) -> Path:
    """A document whose one logged batch replaced ``LONG`` with something else."""
    path = make_docx(tmp_path / "d.docx", LONG, "Otro párrafo.")
    first = str(build_index(Package(path).read("word/document.xml")).paragraphs[0].address)
    reply = cast("Callable[[dict[str, Any]], Reply]", batch)(
        {"doc": str(path), "ops": [{"op": "text.replace", "at": first, "text": "Nuevo."}]}
    )
    assert reply.result["op_id"] == "h-000001"
    return path


def before_hits(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in result["matches"] if m["in"] == "change 0 before"]


def test_history_search_folded_hit_context(tmp_path: Path) -> None:
    path = logged(tmp_path)
    found = search({"doc": str(path), "query": "estas ahi", "match_level": "default"}).result
    hits = before_hits(found)
    assert [(LONG[h["start"] : h["end"]], h["match_level"]) for h in hits] == [
        ("Estás ahí", "default"),
        ("Estas ahi", "case"),
    ]
    for hit in hits:
        assert LONG[hit["start"] : hit["end"]] in hit["text"]  # the hit's own context
        assert hit["text"] == LONG[max(0, hit["start"] - 40) : max(0, hit["start"] - 40) + 80]
    assert found["matches_by_level"] == {
        level: sum(m["match_level"] == level for m in found["matches"])
        for level in ("exact", "case", "default", "loose")
        if any(m["match_level"] == level for m in found["matches"])
    }
    assert sum(found["matches_by_level"].values()) == found["total"]
    # Today's search, without match_level: the folded text is longer, so no offset.
    plain = search(
        {"doc": str(path), "query": "estás ahí", "ignore_case": True, "ignore_accents": True}
    ).result
    (old,) = before_hits(plain)
    assert old["text"] == LONG[:80] and set(old) == {"op_id", "in", "text"}
    assert "matches_by_level" not in plain


def test_history_search_exact_level_keeps_flags(tmp_path: Path) -> None:
    path = logged(tmp_path)
    exact = search({"doc": str(path), "query": "Estás ahí", "match_level": "exact"}).result
    assert [h["match_level"] for h in before_hits(exact)] == ["exact"]
    cased = search(
        {"doc": str(path), "query": "estas ahi", "match_level": "exact", "ignore_case": True}
    ).result
    assert [LONG[h["start"] : h["end"]] for h in before_hits(cased)] == ["Estas ahi"]
    assert [h["match_level"] for h in before_hits(cased)] == ["case"]


def test_history_search_limit_and_total_with_levels(tmp_path: Path) -> None:
    path = logged(tmp_path)
    page = search(
        {"doc": str(path), "query": "estas ahi", "match_level": "default", "limit": 1}
    ).result
    assert len(page["matches"]) == 1 and page["total"] >= 2
    assert sum(page["matches_by_level"].values()) == page["total"]


@pytest.mark.parametrize("flag", ["ignore_case", "ignore_accents"])
def test_history_search_contradicting_flags_refused(tmp_path: Path, flag: str) -> None:
    path = logged(tmp_path)
    with pytest.raises(DocControlError) as err:
        search({"doc": str(path), "query": "Estas", "match_level": "default", flag: False})
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert err.value.details["path"] == f"$.{flag}"
