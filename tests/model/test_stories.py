"""Stories outside the body (STORIES T3): each note, header and footer indexed on its own."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex
from doc_control.model.stories import find_story, list_stories, story_index
from doc_control.ooxml.zipio import Package
from tests.corpus.generator import KINDS, generate, generate_parts


def _package(tmp_path: Path, kind: str) -> Package:
    path = tmp_path / f"{kind}.docx"
    path.write_bytes(generate(kind, 0))
    return Package(path)


def _note_texts(data: bytes, kind: str) -> dict[str, list[str]]:
    """Expected paragraph texts per note, read independently with ElementTree."""
    import xml.etree.ElementTree as ET

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(data)
    out = {}
    for note in root.iter(w + kind):
        if note.get(w + "type", "normal") == "normal":
            prefix = "fn" if kind == "footnote" else "en"
            out[prefix + note.get(w + "id", "")] = [
                "".join(t.text or "" for t in p.iter(w + "t")) for p in note.iter(w + "p")
            ]
    return out


def test_story_index_numbers_each_note_and_header(tmp_path: Path) -> None:
    pkg = _package(tmp_path, "notes")
    parts = generate_parts("notes", 0)
    want = _note_texts(parts["word/footnotes.xml"], "footnote")
    want |= _note_texts(parts["word/endnotes.xml"], "endnote")
    notes = list_stories(pkg, ("notes",))
    assert [s.key for s in notes] == ["fn1", "fn2", "fn3", "fn4", "en1", "en2"]
    for story in notes:
        index = story_index(pkg, story)
        assert [p.text for p in index.paragraphs] == want[story.key]
        assert [p.number for p in index.paragraphs] == list(range(1, len(want[story.key]) + 1))
        for para in index.paragraphs:
            assert str(para.address).startswith(f"{story.key}/p{para.number}#")
            assert index.resolve_address(str(para.address)) == para

    pkg = _package(tmp_path, "headers")
    pages = {s.key: s for s in list_stories(pkg, ("headers",))}
    assert set(pages) == {
        "hdr1.default", "hdr1.first", "ftr1.default",
        "hdr2.default", "hdr2.even", "ftr2.default", "ftr2.even",
    }  # fmt: skip
    # Section 3 defines nothing: it inherits section 2's headers and footers.
    assert pages["hdr2.default"].used_by_sections == (2, 3)
    assert pages["hdr1.default"].used_by_sections == (1,)
    assert pages["hdr1.first"].used_by_sections == (1, 2, 3)
    texts = {k: [p.text for p in story_index(pkg, s).paragraphs] for k, s in pages.items()}
    assert texts["hdr2.default"] == ["Chapter header, odd pages"]
    assert texts["hdr1.first"] == [""]
    assert texts["ftr1.default"] == ["Page 1"]  # the PAGE field's cached result
    assert pages["ftr2.default"].part == pages["ftr2.even"].part  # one part, two kinds


def test_body_index_unchanged_and_addresses_stay_in_their_story(tmp_path: Path) -> None:
    for kind in KINDS:
        data = generate_parts(kind, 0)["word/document.xml"]
        pkg = _package(tmp_path, kind)
        body = story_index(pkg, find_story(pkg, ""))
        assert [str(p.address) for p in body.paragraphs] == [
            str(p.address) for p in DocumentIndex.from_package(pkg).paragraphs
        ]
        assert body.spans.data == data
    pkg = _package(tmp_path, "notes")
    note = story_index(pkg, find_story(pkg, "fn2"))
    body = DocumentIndex.from_package(pkg)
    with pytest.raises(DocControlError) as err:
        note.resolve_address(str(body.paragraphs[0].address))
    assert err.value.code is ErrorCode.ADDRESS_INVALID
    with pytest.raises(DocControlError) as err:
        body.resolve_address(str(note.paragraphs[0].address))
    assert err.value.code is ErrorCode.ADDRESS_INVALID
    with pytest.raises(DocControlError) as err:
        find_story(pkg, "fn99")
    assert err.value.code is ErrorCode.ANCHOR_NOT_FOUND


def test_note_container_picks_its_own_note_only() -> None:
    from doc_control.ooxml.splice import Container, scan

    data = generate_parts("notes", 0)["word/footnotes.xml"]
    spans = scan(data, Container("footnotes", "footnote", "2"), "word/footnotes.xml")
    assert len(spans.paragraphs) == 3  # the note and its two further paragraphs
    with pytest.raises(DocControlError) as err:
        scan(data, Container("endnotes", "endnote", "2"), "word/footnotes.xml")
    assert err.value.code is ErrorCode.NOT_A_DOCX


def _with_document(tmp_path: Path, kind: str, edit: dict[str, tuple[bytes, bytes]]) -> Package:
    import zipfile

    parts = generate_parts(kind, 0)
    for name, (old, new) in edit.items():
        assert old in parts[name]
        parts[name] = parts[name].replace(old, new, 1)
    path = tmp_path / f"{kind}-edited.docx"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return Package(path)


def test_tracked_section_change_is_not_a_section(tmp_path: Path) -> None:
    # A tracked change of section 1's properties holds the old ones in a nested w:sectPr.
    old = b'<w:pgNumType w:fmt="lowerRoman"/><w:titlePg/>'
    change = (
        old + b'<w:sectPrChange w:id="900" w:author="A" w:date="2026-01-01T00:00:00Z">'
        b'<w:sectPr><w:pgNumType w:fmt="decimal"/></w:sectPr></w:sectPrChange>'
    )
    pkg = _with_document(tmp_path, "headers", {"word/document.xml": (old, change)})
    from doc_control.model.stories import section_properties

    assert len(section_properties(pkg)) == 3
    pages = {s.key: s for s in list_stories(pkg, ("headers",))}
    assert pages["hdr1.default"].used_by_sections == (1,)
    assert pages["hdr2.default"].used_by_sections == (2, 3)


def test_repeated_note_id_reads_the_first_note_only(tmp_path: Path) -> None:
    pkg = _with_document(
        tmp_path,
        "notes",
        {"word/footnotes.xml": (b'<w:footnote w:id="3">', b'<w:footnote w:id="1">')},
    )
    keys = [s.key for s in list_stories(pkg, ("notes",))]
    assert keys.count("fn1") == 1 and "fn3" not in keys
    index = story_index(pkg, find_story(pkg, "fn1"))
    assert len(index.paragraphs) == 1
