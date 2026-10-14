"""Verification and undo in parts other than the body (STORIES T5)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.stories import find_story
from doc_control.ooxml.splice import BODY, scan
from doc_control.ooxml.zipio import Package, sha256_file
from doc_control.safety.history import read_log, record, to_text, undo_entry
from doc_control.safety.verify import Footprint, StoryFootprint, write_verified
from tests.corpus.generator import generate

PART = "word/footnotes.xml"


def _doc(tmp_path: Path) -> Path:
    path = tmp_path / "notes.docx"
    path.write_bytes(generate("notes", 0))
    return path


def _note(path: Path, key: str) -> tuple[bytes, StoryFootprint, list[tuple[int, int]]]:
    pkg = Package(path)
    story = find_story(pkg, key)
    data = pkg.read(story.part)
    spans = scan(data, story.container, story.part).paragraphs
    return data, StoryFootprint(story.part, story.container), [(s.start, s.end) for s in spans]


def _edit(data: bytes, span: tuple[int, int], old: bytes, new: bytes) -> bytes:
    start, end = span
    assert old in data[start:end]
    return data[:start] + data[start:end].replace(old, new, 1) + data[end:]


def test_story_part_footprint_rejects_outside_change(tmp_path: Path) -> None:
    path = _doc(tmp_path)
    data, fn2, spans = _note(path, "fn2")
    edited = _edit(data, spans[1], b"</w:t>", b"x</w:t>")
    declared = Footprint(stories=(StoryFootprint(PART, fn2.container, replaced=frozenset({1})),))
    # Declared: fn2's paragraph 2 may change.
    write_verified(path, {PART: edited}, declared)
    assert Package(path).read(PART) == edited

    # Undeclared: the same note's paragraph 1 changes too; refused, the file is restored.
    before = sha256_file(path)
    data, _, spans = _note(path, "fn2")
    twice = _edit(data, spans[0], b"</w:t>", b"x</w:t>")
    with pytest.raises(DocControlError) as err:
        write_verified(path, {PART: twice}, declared)
    assert err.value.code is ErrorCode.VERIFY_FAILED_RESTORED
    assert sha256_file(path) == before

    # A story part changed with no story footprint at all is refused as before.
    with pytest.raises(DocControlError) as err:
        write_verified(path, {PART: twice}, Footprint())
    assert err.value.code is ErrorCode.VERIFY_FAILED_RESTORED

    # A story footprint may not name the body, nor overlap a whole-part allowance.
    body = Footprint(stories=(StoryFootprint("word/document.xml", BODY),))
    with pytest.raises(DocControlError):
        write_verified(path, {PART: twice}, body)
    both = Footprint(parts=frozenset({PART}), stories=declared.stories)
    with pytest.raises(DocControlError):
        write_verified(path, {PART: twice}, both)
    assert sha256_file(path) == before


def test_undo_restores_changes_in_two_parts_together(tmp_path: Path) -> None:
    path = _doc(tmp_path)
    original = path.read_bytes()
    pkg = Package(path)
    fn1, _, fn1_spans = _note(path, "fn1")
    en1 = find_story(pkg, "en1")
    en_data = pkg.read(en1.part)
    en_spans = scan(en_data, en1.container, en1.part).paragraphs
    body = pkg.read("word/document.xml")
    body_spans = scan(body, BODY).paragraphs

    new_fn = _edit(fn1, fn1_spans[0], b"</w:t>", b"x</w:t>")
    en_span = (en_spans[0].start, en_spans[0].end)
    new_en = _edit(en_data, en_span, b"</w:t>", b"x</w:t>")
    b0 = (body_spans[0].start, body_spans[0].end)
    new_body = _edit(body, b0, b"</w:t>", b"x</w:t>")
    footprint = Footprint(
        replaced=frozenset({0}),
        stories=(
            StoryFootprint(PART, find_story(pkg, "fn1").container, replaced=frozenset({0})),
            StoryFootprint(en1.part, en1.container, replaced=frozenset({0})),
        ),
    )
    written = write_verified(
        path, {PART: new_fn, en1.part: new_en, "word/document.xml": new_body}, footprint
    )

    def change(data: bytes, new: bytes, span: tuple[int, int], part: str, story: str) -> dict:
        after_end = span[1] + len(new) - len(data)
        out = {
            "kind": "replace",
            "before": to_text(data[span[0] : span[1]]),
            "after": to_text(new[span[0] : after_end]),
        }
        return out | ({"part": part, "story": story} if story else {})

    changes = [
        change(body, new_body, b0, "word/document.xml", ""),
        change(fn1, new_fn, fn1_spans[0], PART, "fn1"),
        change(en_data, new_en, en_span, en1.part, "en1"),
    ]
    record(
        path,
        "batch",
        before_sha256=written.before_sha256,
        after_sha256=written.after_sha256,
        backup=written.backup,
        changes=changes,
    )
    (entry,) = read_log(path.parent / ".doc-control" / path.name / "log.jsonl").entries
    outcome = undo_entry(path, entry)
    assert {r.get("story", "") for r in outcome.reverted} == {"", "fn1", "en1"}
    with zipfile.ZipFile(path) as now, zipfile.ZipFile(_write(tmp_path, original)) as was:
        for name in was.namelist():
            assert now.read(name) == was.read(name), name


def _write(tmp_path: Path, data: bytes) -> Path:
    out = tmp_path / "original.docx"
    out.write_bytes(data)
    return out
