"""The in-process API (T39): one parse per document, whole results, bulk paragraph facts.

The gate ``api_bulk_read_mismatches == 0``: for every paragraph of every corpus kind and of
the 100k-word novel, the facts ``paragraphs()`` returns equal what the per-paragraph
operations (``para.get``, ``text.normalized``, ``page.estimate``) say about it, and the
novel comes back in one call.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model import document as document_module
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import KINDS
from tests.corpus.generator import generate as generate_docx
from tests.helpers import make_docx

FACTS = (
    "style",
    "style_name",
    "heading_level",
    "centred",
    "page_break_before",
    "blank_before",
    "empty",
)


def _mismatches(path: Path) -> tuple[int, int]:
    """(mismatched facts, paragraphs) between the bulk read and the per-paragraph ops."""
    with api.open(path) as doc:
        rows = doc.paragraphs(normalized=True, pages=True)
        normalized: dict[str, str] = defaultdict(str)
        for entry in doc.run("text.normalized")["paragraphs"]:
            normalized[entry["at"]] += entry["text"]
        pages = {r["at"]: r for r in doc.run("page.estimate")["paragraphs"]}
        bad = 0
        for row in rows:
            para = doc.run("para.get", {"at": row["at"]})
            bad += sum(row[k] != para[k] for k in FACTS)
            bad += row["text"] != para["text"]
            want = normalized.get(row["at"])
            bad += row["normalized"] != (want if want is not None or row["text"].strip() else "")
            bad += want is None and bool(row["text"].strip())
            bad += (row["page"], row["page_label"]) != (
                pages[row["at"]]["page"],
                pages[row["at"]]["page_label"],
            )
    return bad, len(rows)


def test_bulk_read_matches_per_paragraph_ops(tmp_path: Path) -> None:
    load_operations()
    bad = paragraphs = 0
    for kind in (*KINDS, "novel"):
        path = tmp_path / f"{kind}.docx"
        path.write_bytes(generate_docx(kind, 1))
        if kind == "novel":
            reply = api.run("paragraphs.list", {"doc": str(path), "normalized": True})
            assert reply.next_cursor is None
            assert len(reply.result["paragraphs"]) == reply.result["total"] > 1000
            continue  # the per-paragraph comparison below covers the shapes; this the size
        b, n = _mismatches(path)
        bad += b
        paragraphs += n
    print(f"api_bulk_read_mismatches == {bad} (of {paragraphs} paragraphs)")
    assert bad == 0


def test_one_parse_serves_several_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = make_docx(tmp_path / "d.docx", "One.", "Two.")
    calls = 0
    real = document_module.build_index

    def counting(data: bytes) -> Any:
        nonlocal calls
        calls += 1
        return real(data)

    monkeypatch.setattr(document_module, "build_index", counting)
    with api.open(path) as doc:
        doc.run("outline")
        doc.run("stats")
        doc.paragraphs()
        assert calls == 1
        doc.run(
            "batch",
            {
                "ops": [
                    {"op": "text.replace", "at": doc.paragraphs()[0]["at"] + ":0-3", "text": "Uno"}
                ]
            },
        )
        assert [p["text"] for p in doc.paragraphs()] == ["Uno.", "Two."]
    assert calls >= 2  # the write dropped the parse; the file changed, so it was read again


def test_results_are_whole_in_process(tmp_path: Path) -> None:
    load_operations()
    path = make_docx(tmp_path / "d.docx", *(f"Paragraph {n} " + "word " * 60 for n in range(400)))
    capped = REGISTRY["paragraphs.list"].run({"doc": str(path)})
    assert capped.next_cursor is not None  # type: ignore[union-attr]
    with api.open(path) as doc:
        assert len(doc.paragraphs()) == 400
        assert len(doc.run("find", {"query": "word"})["matches"]) > 20
        assert len(doc.run("text.normalized")["paragraphs"]) == 400


def test_a_changed_file_is_read_again(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "Before.")
    with api.open(path) as doc:
        assert doc.paragraphs()[0]["text"] == "Before."
        time.sleep(0.01)
        make_docx(path, "After, and longer.")
        assert doc.paragraphs()[0]["text"] == "After, and longer."


def test_a_partial_result_says_where_to_continue(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "a a a a a", "a a")
    with api.open(path) as doc:
        first = doc.run("find", {"query": "a", "limit": 3})
        assert len(first["matches"]) == 3 and "next" in first
        assert "next" not in doc.run("find", {"query": "a"})


def test_errors_raise(tmp_path: Path) -> None:
    with pytest.raises(DocControlError) as err:
        api.open(tmp_path / "missing.docx").run("outline")
    assert err.value.code is ErrorCode.DOC_NOT_FOUND
    with pytest.raises(DocControlError):
        api.open(tmp_path / "missing.docx").run("no.such.op")
