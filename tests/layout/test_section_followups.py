"""Follow-ups to LAYOUT T3 (issue #231): ``section.unbreak`` before a table, and accepting a
deleted section-closer mark that the section writer did not track."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from tests.layout.test_section_writer import (
    _with_table_after,
    address,
    edit,
    make,
    paragraph_count,
    resolve,
    revisions,
    sections,
)

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def test_unbreak_before_table_refuses(tmp_path: Path) -> None:
    path = _with_table_after(make(tmp_path), "Section 1 end")
    before = path.read_bytes()
    with pytest.raises(DocControlError, match="before a table") as raised:
        edit(path, {"kind": "section.unbreak", "at": address(path, 3)})
    assert raised.value.code is ErrorCode.UNSUPPORTED_CONTENT
    assert path.read_bytes() == before


def _word_file(path: Path) -> Path:
    """A document as Word writes one: a reviewer deleted the mark that ends section 1."""
    closer = (
        '<w:p><w:pPr><w:rPr><w:del w:id="7" w:author="Jane Reviewer" '
        'w:date="2025-03-01T10:00:00Z"/></w:rPr><w:sectPr><w:type w:val="nextPage"/>'
        "</w:sectPr></w:pPr><w:r><w:t>Chapter one ends</w:t></w:r></w:p>"
    )
    body = (
        "<w:p><w:r><w:t>Chapter one begins</w:t></w:r></w:p>"
        + closer
        + "<w:p><w:r><w:t>Chapter two begins</w:t></w:r></w:p>"
        + '<w:sectPr><w:type w:val="oddPage"/></w:sectPr>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {W_NS}><w:body>{body}</w:body></w:document>"
    )
    from tests.helpers import CONTENT_TYPES, RELS

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", document)
    return path


def test_accept_deleted_closer_in_word_file_keeps_paragraphs(tmp_path: Path) -> None:
    path = _word_file(tmp_path / "word.docx")
    assert [r["type"] for r in revisions(path)] == ["mark_delete"]
    out = api.run("batch", {"doc": str(path), "ops": [{"op": "revision.accept", "all": True}]})
    assert revisions(path) == []
    texts = [p.text for p in Document(str(path)).index.paragraphs]
    assert texts == ["Chapter one begins", "Chapter one ends", "Chapter two begins"]
    assert len(sections(path)) == 2  # the break stays
    warnings = out.result.get("warnings") or []
    assert any("keeps its paragraph mark: it ends a section" in w for w in warnings)
    assert not any("container or a section" in w for w in warnings)


def test_the_writers_own_unbreak_still_joins(tmp_path: Path) -> None:
    path = make(tmp_path)
    count = paragraph_count(path)
    edit(path, {"kind": "section.unbreak", "at": address(path, 3)})
    resolve(path, "accept", all=True)
    assert len(sections(path)) == 2 and paragraph_count(path) == count - 1
