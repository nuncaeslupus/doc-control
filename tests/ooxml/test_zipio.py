"""Zip I/O: untouched members stay byte-identical; writes are atomic."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.zipio import Package, write_package
from tests.helpers import document_xml, make_docx


def _members(path: Path) -> list[tuple[str, int, tuple[int, ...], bytes]]:
    with zipfile.ZipFile(path) as zf:
        return [(i.filename, i.compress_type, i.date_time, zf.read(i)) for i in zf.infolist()]


def test_rewrite_one_part_keeps_other_members_byte_identical(tmp_path: Path) -> None:
    """After rewriting document.xml every other member's bytes are unchanged."""
    src = make_docx(tmp_path / "a.docx", "uno", extra={"word/media/img.bin": bytes(range(256))})
    before = _members(src)
    pkg = Package(src)
    write_package(pkg, {"word/document.xml": document_xml("dos").encode()}, src)
    after = _members(src)
    assert [m[0] for m in after] == [m[0] for m in before]
    for old, new in zip(before, after, strict=True):
        if old[0] == "word/document.xml":
            assert new[3] != old[3]
            assert new[1:3] == old[1:3]
        else:
            assert new == old


def test_new_part_is_appended_after_existing_members(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno")
    pkg = Package(src)
    write_package(pkg, {"word/comments.xml": b"<c/>"}, src)
    assert Package(src).names[-1] == "word/comments.xml"


def test_failed_write_leaves_document_and_folder_unchanged(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno")
    raw = src.read_bytes()
    pkg = Package(src)
    with pytest.raises(TypeError):
        write_package(pkg, {"word/document.xml": "not bytes"}, src)  # type: ignore[dict-item]
    assert src.read_bytes() == raw
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.docx"]


def test_missing_file_returns_doc_not_found(tmp_path: Path) -> None:
    with pytest.raises(DocControlError) as err:
        Package(tmp_path / "nope.docx")
    assert err.value.code is ErrorCode.DOC_NOT_FOUND


@pytest.mark.parametrize("content", [b"plain text", b""])
def test_non_zip_returns_not_a_docx(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "x.docx"
    path.write_bytes(content)
    with pytest.raises(DocControlError) as err:
        Package(path)
    assert err.value.code is ErrorCode.NOT_A_DOCX


def test_zip_without_document_part_returns_not_a_docx(tmp_path: Path) -> None:
    path = tmp_path / "x.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("hello.txt", "hi")
    with pytest.raises(DocControlError) as err:
        Package(path)
    assert err.value.details["missing"] == ["[Content_Types].xml", "word/document.xml"]
