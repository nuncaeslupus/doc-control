"""Formatting operations: ``style.apply`` and ``format.apply`` as tracked property changes.

The gate ``content_edits_without_revision_markup == 0``: every style or formatting change
a batch writes is a ``w:pPrChange`` / ``w:rPrChange`` (holding the previous properties)
and nothing else about the text changes; rejecting them gives the prior properties back.
"""

from __future__ import annotations

import random
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import build_index
from doc_control.ooxml.revisions import reject_revisions
from doc_control.ooxml.zipio import Package
from doc_control.ops.batch import batch
from tests.conftest import CorpusWriter
from tests.corpus.generator import KINDS
from tests.corpus.structural import validate_structure
from tests.corpus.validator import require_soffice, soffice_path, validate_documents

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{NS}}}"
KINDS_NO_NOVEL = tuple(k for k in KINDS if k != "novel")
CLEAN = {ErrorCode.UNSUPPORTED_CONTENT, ErrorCode.BATCH_CONFLICT, ErrorCode.SCHEMA_INVALID}


def document(path: Path) -> bytes:
    return Package(path).read("word/document.xml")


def run(path: Path, ops: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    reply = batch({"doc": str(path), "ops": ops, **extra})
    return dict(reply.result)  # type: ignore[union-attr]


def paragraphs(data: bytes) -> list[ET.Element]:
    return list(ET.fromstring(data).iter(W + "p"))


def style_of(p: ET.Element) -> str | None:
    style = p.find(f"{W}pPr/{W}pStyle")
    return None if style is None else style.get(W + "val")


def ppr_children(p: ET.Element) -> list[bytes]:
    ppr = p.find(W + "pPr")
    return [ET.tostring(c) for c in (ppr if ppr is not None else [])]


def reject_format(data: bytes, ids: list[int]) -> list[ET.Element]:
    """Reject the call's revisions in every paragraph (the model the tests compare against)."""
    root = ET.fromstring(data)
    wanted = {str(i) for i in ids}
    for p in root.iter(W + "p"):
        reject_revisions(p, wanted)
    return list(root.iter(W + "p"))


def test_style_apply_tracked_records_ppr_change(corpus_doc: CorpusWriter) -> None:
    missing = 0
    edits = 0
    for kind in KINDS_NO_NOVEL:
        for seed in range(2):
            path = corpus_doc(kind, seed)
            before = paragraphs(document(path))
            index = build_index(document(path))
            picks = [
                p.number
                for p in index.paragraphs
                if p.text.strip() and style_of(before[p.number - 1]) is None
            ]
            if not picks:
                continue
            rng = random.Random(f"{kind}:{seed}")
            picks = sorted(rng.sample(picks, min(3, len(picks))))
            ops: list[dict[str, Any]] = [
                {"op": "style.apply", "at": str(index.paragraph(n).address), "style": "Quote"}
                for n in picks
            ]
            try:
                out = run(path, ops, author="Ana", tag="fmt")
            except DocControlError as err:
                assert err.code in CLEAN
                continue
            after = paragraphs(document(path))
            assert len(after) == len(before)
            for n in picks:
                edits += 1
                change = after[n - 1].find(f"{W}pPr/{W}pPrChange")
                ok = (
                    change is not None
                    and style_of(after[n - 1]) == "Quote"
                    and change.get(W + "author") == "Ana (fmt)"
                    and change.find(f"{W}pPr/{W}pStyle") is None  # previously the default style
                )
                missing += 0 if ok else 1
            restored = reject_format(document(path), out["revision_ids"])
            for n in picks:
                assert style_of(restored[n - 1]) is None
    print(f"content_edits_without_revision_markup={missing} edits={edits}")
    assert edits >= 10
    assert missing == 0


def test_previous_style_is_kept_in_the_change(corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("headings", 0)
    index = build_index(document(path))
    heading = next(
        i + 1 for i, p in enumerate(paragraphs(document(path))) if style_of(p) == "Heading1"
    )
    at = str(index.paragraph(heading).address)
    out = run(path, [{"op": "style.apply", "at": at, "style": "Quote"}])
    p = paragraphs(document(path))[heading - 1]
    assert style_of(p) == "Quote"
    old = p.find(f"{W}pPr/{W}pPrChange/{W}pPr/{W}pStyle")
    assert old is not None and old.get(W + "val") == "Heading1"
    restored = reject_format(document(path), out["revision_ids"])
    assert style_of(restored[heading - 1]) == "Heading1"


def test_format_apply_paragraph_and_run_properties(corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("plain", 0)
    index = build_index(document(path))
    target = next(p for p in index.paragraphs if len(p.text) > 12)
    texts = [p.text for p in index.paragraphs]
    old_ppr = ppr_children(paragraphs(document(path))[target.number - 1])
    at = str(target.address)
    out = run(
        path,
        [
            {"op": "format.apply", "at": at, "align": "center", "indent_first_line_cm": 0.6},
            {"op": "format.apply", "at": f"{at}:2-8", "bold": True, "font": "Arial", "size_pt": 14},
        ],
    )
    assert [p.text for p in build_index(document(path)).paragraphs] == texts
    p = paragraphs(document(path))[target.number - 1]
    jc, ind = p.find(f"{W}pPr/{W}jc"), p.find(f"{W}pPr/{W}ind")
    assert jc is not None and jc.get(W + "val") == "center"
    assert ind is not None and ind.get(W + "firstLine") == "340"
    assert list(p.iter(W + "rPrChange"))
    bold = [r for r in p.iter(W + "r") if r.find(f"{W}rPr/{W}b") is not None]
    assert "".join("".join(r.itertext()) for r in bold) == target.text[2:8]
    restored = reject_format(document(path), out["revision_ids"])[target.number - 1]
    assert ppr_children(restored) == old_ppr
    assert not list(restored.iter(W + "rPrChange"))
    assert all(r.find(f"{W}rPr/{W}b") is None for r in restored.iter(W + "r"))


def test_refusals_and_noops(corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("plain", 0)
    a, b = [p for p in build_index(document(path)).paragraphs if p.text][:2]
    original = path.read_bytes()

    def refused(ops: list[dict[str, Any]]) -> ErrorCode:
        with pytest.raises(DocControlError) as err:
            run(path, ops)
        return err.value.code

    assert (
        refused([{"op": "style.apply", "at": str(a.address), "style": "Nope"}])
        is ErrorCode.STYLE_NOT_FOUND
    )
    assert (
        refused([{"op": "style.apply", "at": f"{a.address}:0-3", "style": "Quote"}])
        is ErrorCode.SCHEMA_INVALID
    )
    two: list[dict[str, Any]] = [
        {"op": "format.apply", "at": str(a.address), "align": "left"},
        {"op": "format.apply", "at": str(a.address), "align": "right"},
    ]
    assert refused(two) is ErrorCode.BATCH_CONFLICT
    clash: list[dict[str, Any]] = [
        {"op": "text.replace", "at": f"{a.address}:0-4", "text": "X"},
        {"op": "format.apply", "at": f"{a.address}:2-6", "bold": True},
    ]
    assert refused(clash) is ErrorCode.BATCH_CONFLICT
    assert refused([{"op": "format.apply", "at": str(a.address)}]) is ErrorCode.SCHEMA_INVALID
    assert path.read_bytes() == original
    # Setting what is already set (or an unresolved change) is refused, never doubled.
    run(path, [{"op": "format.apply", "at": str(b.address), "align": "right"}])
    now = build_index(document(path)).paragraph(b.number).address
    code = refused([{"op": "format.apply", "at": str(now), "align": "right"}])
    assert code in {ErrorCode.SCHEMA_INVALID, ErrorCode.UNSUPPORTED_CONTENT}


def test_format_beside_text_edit_and_dry_run(corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("plain", 1)
    a = next(p for p in build_index(document(path)).paragraphs if len(p.text) > 30)
    original = path.read_bytes()
    at = str(a.address)
    ops: list[dict[str, Any]] = [
        {"op": "text.replace", "at": f"{at}:20-24", "text": "ZZ"},
        {"op": "format.apply", "at": f"{at}:2-8", "italic": True},
        {"op": "format.apply", "at": at, "spacing_after_pt": 6},
    ]
    preview = run(path, ops, dry_run=True)
    assert path.read_bytes() == original and len(preview["preview"]) == 3
    out = run(path, ops)
    assert validate_structure(path.read_bytes()) == []
    restored = reject_format(document(path), out["revision_ids"])
    assert "".join(restored[a.number - 1].itertext()) == a.text


def test_formatting_survives_stacked_rounds_and_lint(corpus_doc: CorpusWriter) -> None:
    for kind in KINDS_NO_NOVEL:
        path = corpus_doc(kind, 3)
        rng = random.Random(kind)
        for round_no in range(2):
            texts = [p for p in build_index(document(path)).paragraphs if len(p.text) > 6]
            if not texts:
                break
            p = rng.choice(texts)
            ops: list[dict[str, Any]] = [
                {"op": "format.apply", "at": f"{p.address}:1-5", "bold": True, "size_pt": 12},
                {
                    "op": "format.apply",
                    "at": str(p.address),
                    "align": rng.choice(["left", "justify"]),
                },
            ]
            try:
                run(path, ops, author=f"R{round_no}")
            except DocControlError as err:
                assert err.code in CLEAN
        assert validate_structure(path.read_bytes()) == [], kind


@pytest.mark.skipif(soffice_path() is None, reason="LibreOffice (soffice) not found")
def test_libreoffice_opens_formatted_documents(tmp_path: Path, corpus_doc: CorpusWriter) -> None:
    require_soffice()
    docs: dict[str, bytes] = {}
    for kind in KINDS_NO_NOVEL:
        path = corpus_doc(kind, 0)
        texts = [p for p in build_index(document(path)).paragraphs if len(p.text) > 6]
        if not texts:
            continue
        at = str(texts[0].address)
        ops: list[dict[str, Any]] = [
            {"op": "style.apply", "at": at, "style": "Quote"},
            {"op": "format.apply", "at": f"{at}:1-5", "italic": True, "font": "Arial"},
        ]
        try:
            run(path, ops)
        except DocControlError:
            continue
        docs[kind] = path.read_bytes()
    assert len(docs) >= 5
    results = validate_documents(docs, tmp_path)
    assert {name: problems for name, problems in results.items() if problems} == {}
