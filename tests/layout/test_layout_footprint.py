"""The layout footprint family accepts section and settings changes and nothing else."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.zipio import Package, build_package
from doc_control.safety.layout import layout_footprint
from doc_control.safety.verify import Footprint, verify_footprint, write_verified
from tests.layout.plans import plan, section, write_plan

DOCUMENT = "word/document.xml"
SETTINGS = "word/settings.xml"
TITLE = b"<w:titlePg/>"


def _built(tmp_path: Path) -> tuple[Path, bytes, bytes]:
    path = write_plan(tmp_path / "d.docx", plan(section(), section(fmt="lowerRoman")))
    pkg = Package(path)
    return path, pkg.read(DOCUMENT), pkg.read(SETTINGS)


def _problems(path: Path, replacements: dict[str, bytes], footprint: Footprint) -> list[str]:
    original = Package(path)
    out = path.with_name("out.docx")
    out.write_bytes(build_package(original, replacements))
    return verify_footprint(original, Package(out), footprint)


def test_a_section_property_change_in_a_declared_paragraph_passes(tmp_path: Path) -> None:
    path, doc, _ = _built(tmp_path)
    changed = doc.replace(b"</w:sectPr>", TITLE + b"</w:sectPr>", 1)
    assert _problems(path, {DOCUMENT: changed}, layout_footprint(replaced=[2])) == []


def test_text_changed_beside_the_section_break_fails(tmp_path: Path) -> None:
    path, doc, _ = _built(tmp_path)
    changed = doc.replace(b"Section 1 end", b"Section 1 END", 1).replace(
        b"</w:sectPr>", TITLE + b"</w:sectPr>", 1
    )
    problems = _problems(path, {DOCUMENT: changed}, layout_footprint(replaced=[2]))
    assert any("more than its section break changed" in p for p in problems)


def test_the_body_section_needs_its_own_allowance(tmp_path: Path) -> None:
    path, doc, _ = _built(tmp_path)
    last = doc.rindex(b"</w:sectPr>")
    changed = doc[:last] + TITLE + doc[last:]
    assert _problems(path, {DOCUMENT: changed}, layout_footprint(replaced=[2]))
    assert _problems(path, {DOCUMENT: changed}, layout_footprint(body_section=True)) == []


def test_an_inserted_break_must_be_empty(tmp_path: Path) -> None:
    path, doc, _ = _built(tmp_path)
    empty = b'<w:p><w:pPr><w:sectPr><w:type w:val="nextPage"/></w:sectPr></w:pPr></w:p>'
    texted = b"<w:p><w:pPr><w:sectPr/></w:pPr><w:r><w:t>x</w:t></w:r></w:p>"
    at = doc.index(b"Section 1 paragraph 1")
    end = doc.index(b"</w:p>", at) + len(b"</w:p>")
    footprint = layout_footprint(inserted_after=[0])
    assert _problems(path, {DOCUMENT: doc[:end] + empty + doc[end:]}, footprint) == []
    assert _problems(path, {DOCUMENT: doc[:end] + texted + doc[end:]}, footprint)


def test_only_the_named_settings_may_change(tmp_path: Path) -> None:
    path, _, settings = _built(tmp_path)
    flagged = settings.replace(b"</w:settings>", b"<w:mirrorMargins/></w:settings>")
    other = settings.replace(b"</w:settings>", b'<w:zoom w:percent="90"/></w:settings>')
    allow = layout_footprint(settings=["mirrorMargins"])
    assert _problems(path, {SETTINGS: flagged}, allow) == []
    assert _problems(path, {SETTINGS: other}, allow)
    assert _problems(path, {SETTINGS: flagged}, layout_footprint())  # not named at all
    with pytest.raises(ValueError):
        layout_footprint(settings=["zoom"])


def test_a_failed_check_restores_the_original(tmp_path: Path) -> None:
    path, doc, _ = _built(tmp_path)
    original = path.read_bytes()
    changed = doc.replace(b"Section 1 end", b"Section 1 END", 1)
    with pytest.raises(DocControlError) as raised:
        write_verified(path, {DOCUMENT: changed}, layout_footprint(replaced=[2]), env={})
    assert raised.value.code is ErrorCode.VERIFY_FAILED_RESTORED
    assert path.read_bytes() == original
