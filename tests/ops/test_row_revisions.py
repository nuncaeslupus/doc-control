"""`revision.accept` and `revision.reject` resolve table row revisions (w:trPr/w:ins, w:del)."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.annotations import read_revisions
from doc_control.model.document import Document
from doc_control.model.index import build_index
from doc_control.ops.revision import ResolveRevisions, apply_resolutions
from tests.corpus.structural import validate_structure
from tests.helpers import CONTENT_TYPES, RELS

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
Mark = tuple[int, str]  # revision id, author


def _mark(rid: int, author: str) -> str:
    return f'w:id="{rid}" w:author="{author}" w:date="2026-01-01T00:00:00Z"'


def _row(text: str, *, ins: Mark | None = None, dele: Mark | None = None) -> str:
    """A one-cell row; ``ins``/``dele`` make it a tracked row insertion/deletion."""
    props = ppr = ""
    if ins:
        props = f"<w:trPr><w:ins {_mark(*ins)}/></w:trPr>"
        ppr = f"<w:pPr><w:rPr><w:ins {_mark(ins[0] + 1, ins[1])}/></w:rPr></w:pPr>"
        run = f"<w:ins {_mark(ins[0] + 2, ins[1])}><w:r><w:t>{text}</w:t></w:r></w:ins>"
    elif dele:
        props = f"<w:trPr><w:del {_mark(*dele)}/></w:trPr>"
        ppr = f"<w:pPr><w:rPr><w:del {_mark(dele[0] + 1, dele[1])}/></w:rPr></w:pPr>"
        run = (
            f"<w:del {_mark(dele[0] + 2, dele[1])}><w:r><w:delText>{text}</w:delText></w:r></w:del>"
        )
    else:
        run = f"<w:r><w:t>{text}</w:t></w:r>"
    cell = f"<w:tc><w:tcPr><w:tcW w:w='1000' w:type='dxa'/></w:tcPr><w:p>{ppr}{run}</w:p></w:tc>"
    return f"<w:tr>{props}{cell}</w:tr>"


def _docx(path: Path, *rows: str) -> Path:
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}"><w:body>'
        "<w:p><w:r><w:t>before</w:t></w:r></w:p>"
        f'<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="1000"/></w:tblGrid>{"".join(rows)}</w:tbl>'
        "<w:p><w:r><w:t>after</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", xml)
    return path


def _xml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml").decode()


def _rows(path: Path) -> list[str]:
    """Row texts, in order."""
    return [
        "".join(re.findall(r"<w:(?:t|delText)[^>]*>([^<]*)<", row))
        for row in re.findall(r"<w:tr[ >].*?</w:tr>", _xml(path), re.DOTALL)
    ]


def _types(path: Path) -> set[str]:
    return {r.type for r in read_revisions(Document(str(path)))}


def _resolve(path: Path, action: str, **sel: object) -> None:
    apply_resolutions(path, [ResolveRevisions(action, **sel)], env={})  # type: ignore[arg-type]


def _standard(path: Path) -> Path:
    return _docx(
        path,
        _row("head"),
        _row("new", ins=(10, "Ana")),
        _row("old", dele=(20, "Ben")),
        _row("tail"),
    )


def _clean(path: Path) -> None:
    data = _xml(path)
    assert not re.search(r"<w:trPr[ >/]", data), data
    assert _types(path) == set()
    assert validate_structure(path.read_bytes()) == []
    assert "" not in [p.text for p in build_index(data.encode()).paragraphs]  # no emptied row


def test_accept_reject_all_resolve_row_revisions(tmp_path: Path) -> None:
    accept = _standard(tmp_path / "accept.docx")
    reject = _standard(tmp_path / "reject.docx")

    _resolve(accept, "accept", all=True)
    assert _rows(accept) == ["head", "new", "tail"]
    _clean(accept)

    _resolve(reject, "reject", all=True)
    assert _rows(reject) == ["head", "old", "tail"]
    _clean(reject)


def test_row_revisions_follow_the_author_and_id_filters(tmp_path: Path) -> None:
    doc = _standard(tmp_path / "a.docx")
    _resolve(doc, "accept", author="Ben")  # the deletion goes, Ana's insertion stays pending
    assert _rows(doc) == ["head", "new", "tail"]
    assert _types(doc) == {"row_insert", "insert", "mark_insert"}
    _resolve(doc, "reject", ids=["10"])  # the row, by its own id
    assert _rows(doc) == ["head", "tail"]

    only_marker = _standard(tmp_path / "b.docx")
    _resolve(only_marker, "accept", ids=["10"])  # the marker goes, the text stays tracked
    assert _rows(only_marker) == ["head", "new", "old", "tail"]
    assert _types(only_marker) == {"row_delete", "delete", "mark_delete", "insert", "mark_insert"}


def test_a_selection_that_empties_a_table_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    doc = _docx(tmp_path / "t.docx", _row("a", dele=(1, "Ana")), _row("b", dele=(5, "Ana")))
    before = doc.read_bytes()
    with pytest.raises(DocControlError) as err:
        _resolve(doc, "accept", all=True)
    assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT
    assert doc.read_bytes() == before
    _resolve(doc, "reject", all=True)  # rejecting keeps both rows
    assert _rows(doc) == ["a", "b"]
    _clean(doc)
