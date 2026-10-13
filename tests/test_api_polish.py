"""Consumer polish (T45), from literary-critic's integration.

Gate: ``api_polish_failures == 0``: in-process ``text.normalized`` gives one entry per
paragraph (on the CLI a long paragraph's windows say ``part`` k of ``parts``), ``find`` gives
the total match count, ``help`` describes the kinds ``batch`` takes, and the facts schema
text documents ``heading_level: null``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from doc_control import api
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import make_docx

LONG = " ".join(f"palabra{n}" for n in range(3000))


def _normalized_unsplit(tmp: Path) -> None:
    doc = make_docx(tmp / "n.docx", "Corto.", LONG, "Fin.")
    with api.open(doc) as d:
        entries = d.run("text.normalized")["paragraphs"]
    assert len(entries) == 3 and entries[1]["text"] == LONG
    capped = REGISTRY["text.normalized"].run({"doc": str(doc)})
    parts = [e for e in capped.result["paragraphs"] if "parts" in e]  # type: ignore[union-attr]
    assert parts and [e["part"] for e in parts] == list(range(1, len(parts) + 1))
    assert all(e["parts"] == parts[0]["parts"] for e in parts)


def _find_total(tmp: Path) -> None:
    doc = make_docx(tmp / "f.docx", *(["uno dos uno"] * 30))
    first = REGISTRY["find"].run({"doc": str(doc), "query": "uno", "limit": 5}).result  # type: ignore[union-attr]
    assert len(first["matches"]) == 5 and first["total"] == 60
    page = REGISTRY["find"].run({"doc": str(doc), "query": "uno", "limit": 5})
    request = {"doc": str(doc), "query": "uno", "limit": 5, "cursor": page.next_cursor}  # type: ignore[union-attr]
    later = REGISTRY["find"].run(request).result  # type: ignore[union-attr]
    assert "total" not in later and len(later["matches"]) == 5


def _help_batch_kinds(tmp: Path) -> None:
    one = REGISTRY["help"].run({"op": "comment.add"})
    assert isinstance(one, dict) and "comment.add" in one["batch_operations"]
    every = REGISTRY["help"].run({})
    assert isinstance(every, dict)
    kinds = set(every["batch_operations"])
    assert {"text.replace", "comment.add", "para.delete", "revision.accept"} <= kinds


def _heading_level_documented(tmp: Path) -> None:
    assert "heading_level is null" in REGISTRY["para.get"].summary
    assert "heading_level is null" in REGISTRY["paragraphs.list"].summary


CHECKS: list[Callable[[Path], None]] = [
    _normalized_unsplit,
    _find_total,
    _help_batch_kinds,
    _heading_level_documented,
]


def test_consumer_polish(tmp_path: Path) -> None:
    load_operations()
    failures = []
    for check in CHECKS:
        try:
            check(tmp_path)
        except AssertionError as err:
            failures.append(f"{check.__name__}: {err!r}")
    print(f"api_polish_failures == {len(failures)} (of {len(CHECKS)} checks)")
    assert failures == []
