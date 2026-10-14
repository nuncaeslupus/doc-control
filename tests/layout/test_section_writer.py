"""LAYOUT T3: the tracked section and settings writer.

The gate ``layout_untracked_changes == 0``: every section property change is a
``w:sectPrChange`` that ``revisions.list`` reports and ``revision.reject`` undoes, byte for
byte. The writer is :func:`doc_control.ops.sectionwrite.apply_layout_edits`.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.ops.sectionwrite import LayoutWriteResult, apply_layout_edits
from doc_control.safety.layout import untracked_section_changes
from tests.layout.plans import SCENARIOS, plan, section, write_plan

DOCUMENT = "word/document.xml"
SETTINGS = "word/settings.xml"
THREE = plan(section(), section("oddPage", start=1), section(fmt="lowerRoman"))
MARGINS = dict(top=1440, right=1200, bottom=1440, left=1200, header=708, footer=708, gutter=0)


def part(path: Path | bytes, name: str = DOCUMENT) -> bytes:
    source = io.BytesIO(path) if isinstance(path, bytes) else path
    with zipfile.ZipFile(source) as zf:
        return zf.read(name)


def make(tmp_path: Path, made: dict[str, Any] = THREE, name: str = "d.docx") -> Path:
    return write_plan(tmp_path / name, made)


def edit(path: Path, *edits: dict[str, Any], **kw: Any) -> LayoutWriteResult:
    return apply_layout_edits(path, list(edits), **kw)


def props(number: int, **values: Any) -> dict[str, Any]:
    return {"kind": "section.props", "section": number, "props": values}


def revisions(path: Path) -> list[dict[str, Any]]:
    return list(api.run("revisions.list", {"doc": str(path)}).result["revisions"])


def sections(path: Path) -> list[dict[str, Any]]:
    return list(api.run("sections.list", {"doc": str(path)}).result["sections"])


def resolve(path: Path, action: str, **selection: Any) -> None:
    api.run("batch", {"doc": str(path), "ops": [{"op": f"revision.{action}", **selection}]})


def paragraph_count(path: Path) -> int:
    return len(Document(str(path)).index)


def address(path: Path, number: int) -> str:
    return str(Document(str(path)).index.paragraph(number).address)


CHANGES: dict[str, dict[str, Any]] = {
    "break type": {"break": "oddPage"},
    "page number start": {"page_number_start": 5},
    "page number format": {"page_number_format": "upperRoman"},
    "different first page": {"title_page": True},
    "margins": {"margins": MARGINS},
    "everything at once": {
        "break": "continuous",
        "page_number_start": 3,
        "page_number_format": "lowerLetter",
        "title_page": True,
        "margins": MARGINS,
    },
}
# Section 1 is closed by a paragraph's w:sectPr, section 3 by the body's own.
WHERE = {"paragraph-level": 1, "body-level": 3}


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("change", CHANGES)
def test_section_props_change_is_tracked_and_rejectable(
    tmp_path: Path, change: str, where: str
) -> None:
    number = WHERE[where]
    path = make(tmp_path)
    original = part(path)
    before = sections(path)[number - 1]

    out = edit(path, props(number, **CHANGES[change]))

    [done] = out.changes
    assert done["kind"] == "section.props" and done["tracked"] is True
    assert part(path) != original
    assert (sections(path)[number - 1] != before) == (change != "margins")  # not in sections.list
    listed = [r for r in revisions(path) if r["type"] == "section_format"]
    assert [r["id"] for r in listed] == done["revision_ids"] == out.revision_ids
    assert listed[0]["author"].startswith("Claude")
    assert untracked_section_changes(original, part(path)) == []

    saved = path.read_bytes()
    resolve(path, "reject", ids=[listed[0]["id"]])
    assert part(path) == original
    assert sections(path)[number - 1] == before
    assert revisions(path) == []

    path.write_bytes(saved)
    resolve(path, "reject", all=True)
    assert part(path) == original

    path.write_bytes(saved)
    resolve(path, "accept", all=True)
    assert b"sectPrChange" not in part(path) and part(path) != original


def test_page_number_start_can_be_removed_and_comes_back(tmp_path: Path) -> None:
    path = make(tmp_path)  # section 2 restarts at 1
    original = part(path)
    edit(path, props(2, page_number_start=None))
    assert sections(path)[1]["page_numbers"]["start"] is None
    resolve(path, "reject", all=True)
    assert part(path) == original and sections(path)[1]["page_numbers"]["start"] == 1


def test_every_section_changes_in_one_write(tmp_path: Path) -> None:
    path = make(tmp_path)
    original = part(path)
    out = edit(path, *(props(n, page_number_format="upperRoman") for n in (1, 2, 3)))
    assert [c["section"] for c in out.changes] == [1, 2, 3]
    listed = revisions(path)
    assert [r["type"] for r in listed] == ["section_format"] * 3
    # The body's own section is reported at the last paragraph.
    assert listed[-1]["at"] == address(path, paragraph_count(path))
    resolve(path, "reject", all=True)
    assert part(path) == original


def test_nothing_to_change_writes_nothing(tmp_path: Path) -> None:
    path = make(tmp_path)
    before = path.read_bytes()
    out = edit(path, props(2, **{"break": "oddPage"}, page_number_start=1, title_page=False))
    assert out.changes == [] and path.read_bytes() == before


def test_undo_restores_the_file_byte_for_byte(tmp_path: Path) -> None:
    path = make(tmp_path)
    original = path.read_bytes()
    out = edit(path, props(1, title_page=True), props(3, page_number_start=9))
    assert out.op_id is not None and path.read_bytes() != original
    api.run("undo", {"doc": str(path)})
    assert path.read_bytes() == original


def test_a_second_change_waits_for_the_first_to_be_resolved(tmp_path: Path) -> None:
    path = make(tmp_path)
    edit(path, props(1, title_page=True))
    with pytest.raises(DocControlError) as raised:
        edit(path, props(1, page_number_start=4))
    assert raised.value.code is ErrorCode.UNSUPPORTED_CONTENT


def test_bad_requests_are_refused_before_anything_is_written(tmp_path: Path) -> None:
    path = make(tmp_path)
    original = path.read_bytes()
    bad: list[tuple[dict[str, Any], ErrorCode]] = [
        (props(1, colour="red"), ErrorCode.SCHEMA_INVALID),
        (props(1, **{"break": "sideways"}), ErrorCode.SCHEMA_INVALID),
        (props(1, page_number_start=-1), ErrorCode.SCHEMA_INVALID),
        (props(9, title_page=True), ErrorCode.SCHEMA_INVALID),
        (props(1, margins={"top": 1}), ErrorCode.UNSUPPORTED_CONTENT),
        ({"kind": "section.sideways"}, ErrorCode.SCHEMA_INVALID),
    ]
    for bad_edit, code in bad:
        with pytest.raises(DocControlError) as raised:
            edit(path, bad_edit)
        assert raised.value.code is code, bad_edit
    assert path.read_bytes() == original


def test_header_references_need_untracked_and_stay_on_reject(tmp_path: Path) -> None:
    path = make(tmp_path, plan(section(header={"default": "One"}), section()))
    original = path.read_bytes()
    refs = {"header_refs": {"first": "rIdh1d"}}
    with pytest.raises(DocControlError) as raised:
        edit(path, props(1, **refs))
    assert raised.value.code is ErrorCode.UNSUPPORTED_CONTENT
    assert path.read_bytes() == original

    out = edit(path, props(1, title_page=True, **refs), untracked=True)
    assert out.changes[0]["tracked"] is True  # the title page is, the reference is not
    assert b'w:type="first"' in part(path)
    resolve(path, "reject", all=True)
    now = part(path)
    assert b"titlePg" not in now and b'w:type="first"' in now  # the reference stayed

    only_refs = edit(path, props(2, header_refs={"default": "rIdh1d"}), untracked=True)
    assert only_refs.changes[0]["tracked"] is False and only_refs.revision_ids == []


def test_insert_a_section_break_is_tracked_and_rejectable(tmp_path: Path) -> None:
    path = make(tmp_path)
    original = part(path)
    count = paragraph_count(path)
    out = edit(path, {"kind": "section.break", "after": address(path, 2), "type": "evenPage"})
    assert sorted(c["kind"] for c in out.changes) == ["section.break", "section.props"]
    assert paragraph_count(path) == count + 1
    rows = sections(path)
    assert [r["break"] for r in rows] == ["nextPage", "evenPage", "oddPage", "nextPage"]
    assert sorted(r["type"] for r in revisions(path)) == ["mark_insert", "section_format"]
    assert untracked_section_changes(original, part(path), [1]) == []

    saved = path.read_bytes()
    resolve(path, "reject", all=True)
    assert part(path) == original
    assert paragraph_count(path) == count and len(sections(path)) == 3

    path.write_bytes(saved)
    resolve(path, "accept", all=True)
    assert revisions(path) == [] and len(sections(path)) == 4


def test_a_break_keeps_each_half_as_the_whole_was(tmp_path: Path) -> None:
    path = make(tmp_path, plan(section(start=1, first_page_different=True), section()))
    edit(path, {"kind": "section.break", "after": address(path, 1)})
    rows = sections(path)
    assert [r["page_numbers"]["start"] for r in rows] == [1, None, None]
    assert [r["title_page"] for r in rows] == [True, False, False]


def test_remove_a_section_break_is_tracked_and_rejectable(tmp_path: Path) -> None:
    path = make(tmp_path)
    original = part(path)
    count = paragraph_count(path)
    out = edit(path, {"kind": "section.unbreak", "at": address(path, 3)})
    assert [c["kind"] for c in out.changes] == ["section.unbreak"]
    assert [r["type"] for r in revisions(path)] == ["mark_delete"]
    assert len(sections(path)) == 3  # tracked: it stays until accepted

    saved = path.read_bytes()
    resolve(path, "reject", all=True)
    assert part(path) == original

    path.write_bytes(saved)
    resolve(path, "accept", all=True)
    assert len(sections(path)) == 2 and paragraph_count(path) == count - 1


def test_break_refusals(tmp_path: Path) -> None:
    path = make(tmp_path)
    last = address(path, paragraph_count(path))
    cases = [
        ({"kind": "section.break", "after": address(path, 3)}, "already ends"),
        ({"kind": "section.break", "after": last}, "no content"),
        ({"kind": "section.unbreak", "at": address(path, 1)}, "does not end"),
    ]
    for bad_edit, why in cases:
        with pytest.raises(DocControlError, match=why) as raised:
            edit(path, bad_edit)
        assert raised.value.code is ErrorCode.UNSUPPORTED_CONTENT


def test_settings_flags_are_untracked_and_undoable(tmp_path: Path) -> None:
    path = make(tmp_path)
    original = path.read_bytes()
    with pytest.raises(DocControlError) as raised:
        edit(path, {"kind": "settings", "even_and_odd_headers": True})
    assert raised.value.code is ErrorCode.UNSUPPORTED_CONTENT

    both = {"kind": "settings", "even_and_odd_headers": True, "mirror_margins": True}
    out = edit(path, both, untracked=True)
    assert sorted((c["flag"], c["tracked"]) for c in out.changes) == [
        ("evenAndOddHeaders", False),
        ("mirrorMargins", False),
    ]
    settings = part(path, SETTINGS)
    assert b"<w:evenAndOddHeaders/>" in settings and b"<w:mirrorMargins/>" in settings
    assert all(row["even_and_odd"] for row in sections(path))
    assert part(path) == part(original)  # the body is not touched

    off = {"kind": "settings", "even_and_odd_headers": False, "mirror_margins": False}
    edit(path, off, untracked=True)
    assert part(path, SETTINGS) == part(original, SETTINGS)
    api.run("undo", {"doc": str(path)})
    api.run("undo", {"doc": str(path)})
    assert path.read_bytes() == original


def test_settings_flags_go_in_schema_order_and_keep_the_rest(tmp_path: Path) -> None:
    made = make(tmp_path, plan(section()))
    path = tmp_path / "t.docx"
    with zipfile.ZipFile(made) as src, zipfile.ZipFile(path, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == SETTINGS:
                data = data.replace(
                    b"</w:settings>",
                    b'<w:zoom w:percent="100"/><w:defaultTabStop w:val="720"/>'
                    b'<w:compat><w:compatSetting w:name="x" w:val="1"/></w:compat></w:settings>',
                )
            dst.writestr(item, data)
    old = part(path, SETTINGS)
    both = {"kind": "settings", "mirror_margins": True, "even_and_odd_headers": True}
    edit(path, both, untracked=True)
    new = part(path, SETTINGS)
    order = [m.decode() for m in re.findall(rb"<w:(\w+)", new)]
    assert order == [
        "settings",
        "zoom",
        "mirrorMargins",
        "defaultTabStop",
        "evenAndOddHeaders",
        "compat",
        "compatSetting",
    ]
    assert re.sub(rb"<w:(mirrorMargins|evenAndOddHeaders)/>", b"", new) == old


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_documents_take_tracked_changes_and_reject_them(tmp_path: Path, name: str) -> None:
    path = make(tmp_path, SCENARIOS[name])
    original = part(path)
    last = len(sections(path))
    edit(path, props(1, title_page=True), props(last, page_number_format="upperLetter"))
    assert {r["type"] for r in revisions(path)} <= {"section_format"}
    resolve(path, "reject", all=True)
    assert part(path) == original


def test_the_metric_flags_an_untracked_section_change(tmp_path: Path) -> None:
    path = make(tmp_path, plan(section(header={"default": "One"}), section()))
    original = part(path)
    edit(path, props(1, title_page=True), props(2, page_number_start=2))
    tracked = part(path)
    assert untracked_section_changes(original, tracked) == []
    stripped = re.sub(rb"<w:sectPrChange\b.*?</w:sectPrChange>", b"", tracked)
    problems = untracked_section_changes(original, stripped)
    assert len(problems) == 2 and all("w:sectPrChange" in p for p in problems)
    # A reference change cannot be tracked, so it counts unless the caller allowed it.
    swapped = original.replace(b'r:id="rIdh1d"', b'r:id="rIdZ"')
    assert swapped != original
    assert len(untracked_section_changes(original, swapped)) == 1
    assert untracked_section_changes(original, swapped, allow_refs=True) == []


def _with_table_after(path: Path, text: str) -> Path:
    """``path`` with a one-cell table right after the paragraph holding ``text``."""
    with zipfile.ZipFile(path) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    table = b"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    doc = members[DOCUMENT]
    at = doc.index(b"</w:p>", doc.index(text.encode())) + len(b"</w:p>")
    members[DOCUMENT] = doc[:at] + table + doc[at:]
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_a_break_before_a_table_is_refused_not_left_unrejectable(tmp_path: Path) -> None:
    path = _with_table_after(make(tmp_path), "Section 1 paragraph 2")
    before = path.read_bytes()
    with pytest.raises(DocControlError, match="before a table") as raised:
        edit(path, {"kind": "section.break", "after": address(path, 2)})
    assert raised.value.code is ErrorCode.UNSUPPORTED_CONTENT
    assert path.read_bytes() == before


def test_two_breaks_in_one_section_each_get_their_own_setup(tmp_path: Path) -> None:
    path = make(tmp_path, plan(section(), section(start=1, first_page_different=True)))
    original = part(path)
    out = edit(
        path,
        {"kind": "section.break", "after": address(path, 4), "type": "evenPage"},
        {"kind": "section.break", "after": address(path, 5), "type": "oddPage"},
    )
    assert sum(c["kind"] == "section.break" for c in out.changes) == 2
    rows = sections(path)
    assert [r["break"] for r in rows] == ["nextPage", "nextPage", "evenPage", "oddPage"]
    assert [r["page_numbers"]["start"] for r in rows] == [None, 1, None, None]
    assert [r["title_page"] for r in rows] == [False, True, False, False]

    resolve(path, "reject", all=True)
    assert part(path) == original


def test_set_flag_on_a_self_closing_root_and_a_doctype(tmp_path: Path) -> None:
    from doc_control.ooxml.settingsflags import read_flag, set_flag

    ns = b'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    out = set_flag(b"<w:settings " + ns + b"/>", "mirrorMargins", True)
    assert out == b"<w:settings " + ns + b"><w:mirrorMargins/></w:settings>"
    doctype = b'<?xml version="1.0"?><!DOCTYPE w:settings [<!ENTITY a "b">]>'
    full = doctype + b"<w:settings " + ns + b"><w:mirrorMargins/></w:settings>"
    assert read_flag(full, "mirrorMargins") is True
    assert set_flag(full, "mirrorMargins", False).endswith(b"<w:settings " + ns + b"></w:settings>")
