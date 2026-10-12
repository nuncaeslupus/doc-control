"""Zip I/O: untouched members stay byte-identical; writes are atomic."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import struct
import sys
import warnings
import zipfile
from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml import zipio
from doc_control.ooxml.zipio import Package, write_package
from tests.helpers import CONTENT_TYPES, RELS, document_xml, make_docx

LOCAL = struct.Struct("<4s5H3L2H")
CENTRAL = struct.Struct("<4s4B4HL2L5H2L")


RawMember = tuple[str, tuple[object, ...], bytes, tuple[object, ...]]


def _raw_members(path: Path) -> list[RawMember]:
    """Per member: name, local header fields, raw compressed bytes, central fields."""
    raw = path.read_bytes()
    out: list[RawMember] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            off = info.header_offset
            fields = LOCAL.unpack_from(raw, off)
            nlen, xlen = fields[9], fields[10]
            start = off + LOCAL.size
            name, extra = raw[start : start + nlen], raw[start + nlen : start + nlen + xlen]
            data_at = start + nlen + xlen
            central = (
                info.create_system,
                info.create_version,
                info.extract_version,
                info.flag_bits,
                info.internal_attr,
                info.external_attr,
                info.comment,
                info.extra,
                info.date_time,
                info.CRC,
            )
            out.append(
                (
                    info.filename,
                    (*fields[:9], name, extra),
                    raw[data_at : data_at + info.compress_size],
                    central,
                )
            )
    return out


def _level1_docx(path: Path) -> Path:
    """A package whose members are deflated at level 1, with attributes and comments set."""
    with zipfile.ZipFile(path, "w", compresslevel=1) as zf:
        zf.comment = b"archive comment"
        for name, data, method in [
            ("[Content_Types].xml", CONTENT_TYPES.encode(), zipfile.ZIP_DEFLATED),
            ("_rels/.rels", RELS.encode(), zipfile.ZIP_DEFLATED),
            ("word/document.xml", document_xml("uno " * 200).encode(), zipfile.ZIP_DEFLATED),
            (
                "word/styles.xml",
                b"<w:styles>" + b"<w:style/>" * 500 + b"</w:styles>",
                zipfile.ZIP_DEFLATED,
            ),
            ("word/media/img.bin", bytes(range(256)) * 4, zipfile.ZIP_STORED),
        ]:
            info = zipfile.ZipInfo(name, (2001, 2, 3, 4, 5, 6))
            info.compress_type = method
            info.create_system = 0
            info.external_attr = 0x20
            info.internal_attr = 1
            info.comment = f"c:{name}".encode()
            zf.writestr(info, data, compresslevel=1)
    return path


def _patch(raw: bytearray, name: str, local_at: int, central_at: int, fmt: str, value: int) -> None:
    """Overwrite one field at the given offsets of ``name``'s local and central headers."""
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as zf:
        info = zf.getinfo(name)
    central = _central_offset(bytes(raw), name)
    if local_at >= 0:
        struct.pack_into(fmt, raw, info.header_offset + local_at, value)
    if central_at >= 0:
        struct.pack_into(fmt, raw, central + central_at, value)


def _central_offset(raw: bytes, name: str) -> int:
    eocd = raw.rfind(b"PK\x05\x06")
    pos: int = struct.unpack_from("<L", raw, eocd + 16)[0]
    while raw[pos : pos + 4] == b"PK\x01\x02":
        fields = CENTRAL.unpack_from(raw, pos)
        nlen, xlen, clen = fields[12], fields[13], fields[14]
        if raw[pos + CENTRAL.size : pos + CENTRAL.size + nlen] == name.encode():
            return pos
        pos = pos + CENTRAL.size + nlen + xlen + clen
    raise AssertionError(f"{name} not in central directory")


def _not_a_docx(path: Path) -> DocControlError:
    with pytest.raises(DocControlError) as err:
        Package(path)
    return err.value


def test_rewrite_one_part_keeps_other_members_byte_identical(tmp_path: Path) -> None:
    """After rewriting document.xml every other member's raw stored bytes are unchanged.

    Compared on the compressed bytes and local/central header fields, with members
    deflated at level 1, so a decompress-and-recompress copy fails this test.
    """
    src = _level1_docx(tmp_path / "a.docx")
    with zipfile.ZipFile(src) as zf:
        comment = zf.comment
    before = _raw_members(src)
    pkg = Package(src)
    write_package(pkg, {"word/document.xml": document_xml("dos").encode()}, src)
    after = _raw_members(src)
    assert [m[0] for m in after] == [m[0] for m in before]
    untouched = identical = 0
    for old, new in zip(before, after, strict=True):
        if old[0] == "word/document.xml":
            assert new[2] != old[2]
            assert new[1][2:5] == old[1][2:5]  # flags, method, time
            continue
        untouched += 1
        identical += new == old
    assert untouched == 4
    assert identical / untouched == 1.0  # untouched_parts_byte_identical_ratio
    with zipfile.ZipFile(src) as zf:
        assert zf.testzip() is None
        assert zf.comment == comment
        assert zf.read("word/document.xml") == document_xml("dos").encode()


def test_rewrite_keeps_make_docx_members_identical(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno", extra={"word/media/img.bin": bytes(range(256))})
    before = _raw_members(src)
    write_package(Package(src), {"word/document.xml": document_xml("dos").encode()}, src)
    after = _raw_members(src)
    for old, new in zip(before, after, strict=True):
        if old[0] != "word/document.xml":
            assert new == old


def test_replaced_stored_member_stays_stored(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno", extra={"word/media/img.bin": b"old"})
    write_package(Package(src), {"word/media/img.bin": b"new data"}, src)
    with zipfile.ZipFile(src) as zf:
        info = zf.getinfo("word/media/img.bin")
        assert info.compress_type == zipfile.ZIP_STORED
        assert zf.read(info) == b"new data"
        assert zf.testzip() is None


def test_new_part_is_appended_after_existing_members(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno")
    pkg = Package(src)
    write_package(pkg, {"word/comments.xml": b"<c/>"}, src)
    after = Package(src)
    assert after.names[-1] == "word/comments.xml"
    assert after.read("word/comments.xml") == b"<c/>"
    assert after.infos[-1].compress_type == zipfile.ZIP_DEFLATED


def test_returned_hash_matches_written_file(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno")
    digest = write_package(Package(src), {"word/document.xml": b"<x/>"}, src)
    assert Package(src).sha256 == digest


def test_package_hash_matches_bytes_read(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno")
    assert Package(src).sha256 == hashlib.sha256(src.read_bytes()).hexdigest()


def test_failed_write_leaves_document_and_folder_unchanged(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno")
    raw = src.read_bytes()
    pkg = Package(src)
    with pytest.raises(TypeError):
        write_package(pkg, {"word/document.xml": "not bytes"}, src)  # type: ignore[dict-item]
    assert src.read_bytes() == raw
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.docx"]


def test_failure_after_temp_file_exists_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure once the temp file is written leaves the document and folder unchanged."""
    src = make_docx(tmp_path / "a.docx", "uno")
    raw = src.read_bytes()
    pkg = Package(src)
    seen: list[list[str]] = []

    def boom(tmp: Path, dest: Path) -> None:
        seen.append(sorted(p.name for p in tmp_path.iterdir()))
        assert tmp.is_file()
        raise OSError("disk full")

    monkeypatch.setattr(zipio, "_replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_package(pkg, {"word/document.xml": b"<x/>"}, src)
    assert len(seen) == 1 and len(seen[0]) == 2  # the temp file did exist
    assert src.read_bytes() == raw
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.docx"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_write_preserves_document_permission_bits(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno")
    src.chmod(0o640)
    write_package(Package(src), {"word/document.xml": b"<x/>"}, src)
    assert stat.S_IMODE(src.stat().st_mode) == 0o640


def test_missing_file_returns_doc_not_found(tmp_path: Path) -> None:
    with pytest.raises(DocControlError) as err:
        Package(tmp_path / "nope.docx")
    assert err.value.code is ErrorCode.DOC_NOT_FOUND


@pytest.mark.parametrize("content", [b"plain text", b""])
def test_non_zip_returns_not_a_docx(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "x.docx"
    path.write_bytes(content)
    err = _not_a_docx(path)
    assert err.code is ErrorCode.NOT_A_DOCX
    assert err.details["path"] == str(path)


def test_zip_without_document_part_returns_not_a_docx(tmp_path: Path) -> None:
    path = tmp_path / "x.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("hello.txt", "hi")
    err = _not_a_docx(path)
    assert err.code is ErrorCode.NOT_A_DOCX
    assert err.details["missing"] == ["[Content_Types].xml", "word/document.xml"]
    assert err.details["path"] == str(path)


def test_bad_crc_returns_not_a_docx(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "x.docx", "uno")
    raw = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as zf:
        crc = zf.getinfo("word/document.xml").CRC
    _patch(raw, "word/document.xml", 14, 16, "<L", crc ^ 0xFFFF)
    path.write_bytes(raw)
    err = _not_a_docx(path)
    assert err.code is ErrorCode.NOT_A_DOCX
    assert "word/document.xml" in err.message


def test_corrupt_deflate_data_returns_not_a_docx(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "x.docx", "uno")
    raw = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo("word/document.xml")
    start = info.header_offset + 30 + len(b"word/document.xml")
    raw[start : start + info.compress_size] = b"\xff" * info.compress_size
    path.write_bytes(raw)
    assert _not_a_docx(path).code is ErrorCode.NOT_A_DOCX


def test_unknown_compression_method_returns_not_a_docx(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "x.docx", "uno")
    raw = bytearray(path.read_bytes())
    _patch(raw, "word/document.xml", 8, 10, "<H", 99)
    path.write_bytes(raw)
    err = _not_a_docx(path)
    assert err.code is ErrorCode.NOT_A_DOCX
    assert "compression" in err.message


def test_encrypted_member_returns_unsupported_content(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "x.docx", "uno")
    raw = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as zf:
        flags = zf.getinfo("word/document.xml").flag_bits
    _patch(raw, "word/document.xml", 6, 8, "<H", flags | 0x1)
    path.write_bytes(raw)
    err = _not_a_docx(path)
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT
    assert "encrypted" in err.message


def test_zip64_returns_unsupported_content(tmp_path: Path) -> None:
    path = tmp_path / "x.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        with zf.open("word/document.xml", "w", force_zip64=True) as fh:
            fh.write(document_xml("uno").encode())
    err = _not_a_docx(path)
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT
    assert "ZIP64" in err.message
    hint = err.to_dict()["hint"]
    assert "encrypted" in hint
    assert "ZIP64" in hint


def test_duplicate_member_names_return_not_a_docx(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "x.docx", "uno")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("word/document.xml", document_xml("dos"))
    err = _not_a_docx(path)
    assert err.code is ErrorCode.NOT_A_DOCX
    assert err.details["duplicates"] == ["word/document.xml"]


@pytest.mark.writes_invalid_docs  # stdlib zipfile cannot read it back, so the lint cannot either
def test_comment_containing_end_signature_is_not_zip64(tmp_path: Path) -> None:
    """An archive comment that looks like a ZIP64 locator plus end record is just a comment."""
    path = make_docx(tmp_path / "x.docx", "uno")
    fake = b"PK\x06\x07" + b"\x00" * 16 + b"PK\x05\x06" + b"\xff" * 18
    with zipfile.ZipFile(path, "a") as zf:
        zf.comment = fake
    pkg = Package(path)
    assert pkg.comment == fake
    write_package(pkg, {"word/document.xml": b"<x/>"}, path)
    assert Package(path).comment == fake


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_new_output_file_gets_default_mode(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "a.docx", "uno")
    dest = tmp_path / "b.docx"
    old = os.umask(0o022)
    try:
        write_package(Package(src), {}, dest)
    finally:
        os.umask(old)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644
