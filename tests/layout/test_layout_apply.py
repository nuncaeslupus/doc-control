"""`layout.apply` (LAYOUT T4): breaks and numbering reach the plan, and LibreOffice prints it.

The gate ``page_label_mismatches == 0``: each numbering scenario is applied to a plain
document (one section, a page per paragraph, a footer printing ``pg:<PAGE>``), and the page
numbers in LibreOffice's PDF equal the plan.
"""

from __future__ import annotations

import copy
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.ops import layout
from doc_control.ops.sections import section_rows
from tests.layout.label_fixtures import Scenario, Sec, build_docx, export_pdfs, label_failures
from tests.layout.test_page_labels import _require_tools

NUMBERED = [
    Scenario("roman_then_arabic", [Sec(4), Sec(2)], ["i", "ii", "iii", "iv", "1", "2"]),
    Scenario("restart_per_part", [Sec(2), Sec(2), Sec(1)], ["1", "2", "1", "2", "1"]),
    Scenario(
        "recto_chapter_starts",
        [Sec(1), Sec(2), Sec(1)],
        ["1", "", "3", "4", "5"],
    ),
    Scenario("start_at_nine_then_roman", [Sec(2), Sec(3)], ["9", "10", "i", "ii", "iii"]),
]
#: What each scenario asks of its sections: the break, the first page number and the format.
ASKED: dict[str, list[dict[str, Any]]] = {
    "roman_then_arabic": [
        {"numbering": {"start": 1, "format": "lowerRoman"}},
        {"numbering": {"start": 1, "format": "decimal"}},
    ],
    "restart_per_part": [{"numbering": {"start": 1}}] * 3,
    "recto_chapter_starts": [
        {"numbering": {"start": 1}},
        {"break": "oddPage"},
        {"break": "oddPage"},
    ],
    "start_at_nine_then_roman": [
        {"numbering": {"start": 9}},
        {"numbering": {"start": 1, "format": "lowerRoman"}},
    ],
}


def plain(tmp_path: Path, pages: int, name: str = "plain.docx") -> Path:
    """One section, ``pages`` paragraphs, each on its own page, a footer with the number."""
    return build_docx(Scenario(name, [Sec(pages, shown=True)], []), tmp_path / f"{name}")


def plan_for(path: Path, scenario: Scenario) -> dict[str, Any]:
    """The page plan the scenario describes, its sections starting at the page each begins."""
    doc = Document(str(path))
    sections = []
    first = 1
    for sec, asked in zip(scenario.sections, ASKED[scenario.name], strict=True):
        sections.append({"from": str(doc.index.paragraph(first).address), **asked})
        first += sec.pages
    return {"version": 1, "sections": sections}


def current(path: Path) -> dict[str, Any]:
    return dict(api.run("layout.get", {"doc": str(path)}).result)


def test_apply_numbering_scenarios_print_planned_labels(tmp_path: Path) -> None:
    _require_tools()
    docs, plans, first_texts = [], {}, {}
    for scenario in NUMBERED:
        path = plain(tmp_path, sum(s.pages for s in scenario.sections), f"{scenario.name}.docx")
        plans[scenario.name] = plan_for(path, scenario)
        doc = Document(str(path))
        first_texts[scenario.name] = [
            doc.index.resolve_address(s["from"]).text for s in plans[scenario.name]["sections"]
        ]
        out = api.run("layout.apply", {"doc": str(path), "plan": plans[scenario.name]}).result
        assert out["dry_run"] is False and out["changes"], scenario.name
        assert all(change["tracked"] for change in out["changes"] if change["kind"] != "settings")
        assert out["plan"] == current(path), scenario.name
        docs.append(path)
    pdfs = export_pdfs(docs, tmp_path / "pdf")

    failures = []
    for scenario, path in zip(NUMBERED, docs, strict=True):
        failures += label_failures(scenario, pdfs[path.stem])
        doc = Document(str(path))
        got = current(path)["sections"]
        starts = [doc.index.resolve_address(sect["from"]).text for sect in got]
        assert starts == first_texts[scenario.name]
    assert failures == [], failures


def asked_plan(
    path: Path, *sections: dict[str, Any], first: list[int] | None = None
) -> dict[str, Any]:
    """A plan whose sections start at the paragraphs ``first`` (default 1, 3, 5...)."""
    doc = Document(str(path))
    firsts = first or [1 + 2 * i for i in range(len(sections))]
    return {
        "version": 1,
        "sections": [
            {"from": str(doc.index.paragraph(n).address), **sect}
            for n, sect in zip(firsts, sections, strict=True)
        ],
    }


ROMAN_THEN_ARABIC = (
    {"numbering": {"start": 1, "format": "lowerRoman"}},
    {"numbering": {"start": 1}},
)


def refused(path: Path, plan: dict[str, Any], code: ErrorCode) -> DocControlError:
    before = path.read_bytes()
    with pytest.raises(DocControlError) as caught:
        api.run("layout.apply", {"doc": str(path), "plan": plan})
    assert caught.value.code is code, plan
    assert path.read_bytes() == before
    return caught.value


def test_dry_run_lists_the_changes_and_leaves_the_file_identical(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    before = path.read_bytes()
    plan = asked_plan(path, *ROMAN_THEN_ARABIC)

    out = api.run("layout.apply", {"doc": str(path), "plan": plan, "dry_run": True}).result

    assert path.read_bytes() == before
    assert out["dry_run"] is True
    assert sorted(c["kind"] for c in out["changes"]) == ["section.break", "section.props"]
    assert out["plan"]["sections"][0]["numbering"] == {
        "shown": True,
        "start": 1,
        "format": "lowerRoman",
    }
    # The preview is the plan the real run then produces.
    real = api.run("layout.apply", {"doc": str(path), "plan": plan}).result
    assert real["plan"] == out["plan"] and path.read_bytes() != before


def test_a_plan_the_document_cannot_reach_writes_nothing(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    good = asked_plan(path, *ROMAN_THEN_ARABIC)
    # The breaks and numbering alone would be fine; the rest is refused, and so is the lot.
    for change in (
        {"footer": {"default": "Page {PAGE} of {NUMPAGES}"}},
        {"numbering": {"start": 1, "shown": False}},
    ):
        plan = copy.deepcopy(good)
        plan["sections"][1].update(change)
        assert refused(path, plan, ErrorCode.UNSUPPORTED_CONTENT).details["differences"]
    refused(path, {**good, "two_sided": True}, ErrorCode.UNSUPPORTED_CONTENT)


def test_bad_plans_are_refused(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    good = asked_plan(path, *ROMAN_THEN_ARABIC)
    stale = copy.deepcopy(good)
    stale["sections"][1]["from"] = "p3#00000000"
    late = copy.deepcopy(good)
    late["sections"][0]["from"] = good["sections"][1]["from"]
    bad_format = copy.deepcopy(good)
    bad_format["sections"][0]["numbering"]["format"] = "roman"
    bad_break = copy.deepcopy(good)
    bad_break["sections"][1]["break"] = "nextColumn"
    refused(path, stale, ErrorCode.STALE_ADDRESS)
    for plan in (
        {**good, "version": 2},
        {"version": 1, "sections": []},
        late,
        bad_format,
        bad_break,
    ):
        refused(path, plan, ErrorCode.SCHEMA_INVALID)


def test_existing_section_breaks_are_kept(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    api.run("layout.apply", {"doc": str(path), "plan": asked_plan(path, *ROMAN_THEN_ARABIC)})
    one = {"version": 1, "sections": current(path)["sections"][:1]}
    refused(path, one, ErrorCode.UNSUPPORTED_CONTENT)


def test_a_plan_the_document_already_has_changes_nothing(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    api.run("layout.apply", {"doc": str(path), "plan": asked_plan(path, *ROMAN_THEN_ARABIC)})
    before = path.read_bytes()
    again = api.run("layout.apply", {"doc": str(path), "plan": current(path)}).result
    assert again["changes"] == [] and path.read_bytes() == before


def test_undo_restores_the_bytes_and_reject_restores_the_document(tmp_path: Path) -> None:
    path = plain(tmp_path, 6)
    original = path.read_bytes()
    plan = asked_plan(
        path,
        {"numbering": {"start": 1, "format": "lowerRoman"}},
        {"break": "oddPage", "numbering": {"start": 1}},
        {"numbering": {"start": 1, "format": "upperLetter"}, "first_page_different": True},
    )
    out = api.run("layout.apply", {"doc": str(path), "plan": plan}).result
    assert out["op_id"] and out["changes"]
    assert [s["break"] for s in out["plan"]["sections"]] == ["nextPage", "oddPage", "nextPage"]
    assert out["plan"]["sections"][2]["first_page_different"] is True
    assert path.read_bytes() != original

    saved = path.read_bytes()
    api.run("undo", {"doc": str(path)})
    assert path.read_bytes() == original

    path.write_bytes(saved)
    api.run("batch", {"doc": str(path), "ops": [{"op": "revision.reject", "all": True}]})
    assert current(path) == current(plain(tmp_path, 6, "o.docx"))


# --- headers, footers, two-sided (LAYOUT T5) -----------------------------------------------

#: Front matter in lower roman with a title page, then the text from 1; printed two-sided, so
#: odd and even pages carry their own header and footer, with page numbers and the count.
TWO_SIDED_SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "numbering": {"start": 1, "format": "lowerRoman"},
        "first_page_different": True,
        "header": {"default": "hd:front-{PAGE}", "first": "hd:title", "even": "hd:verso-{PAGE}"},
        "footer": {"default": "pg:{PAGE}", "first": "", "even": "pg:{PAGE}"},
    },
    {
        "numbering": {"start": 1, "format": "decimal"},
        "header": {"default": "hd:odd-{PAGE}", "even": "hd:even-{PAGE}"},
        "footer": {"default": "pg:{PAGE}/{NUMPAGES}", "even": "pg:{PAGE}"},
    },
)
TWO_SIDED = Scenario(
    "two_sided_headers",
    [Sec(2), Sec(4)],
    ["", "ii", "1/6", "2", "3/6", "4"],
    expected_headers=["title", "verso-ii", "odd-1", "even-2", "odd-3", "even-4"],
)


def two_sided_plan(path: Path) -> dict[str, Any]:
    plan = asked_plan(path, *copy.deepcopy(TWO_SIDED_SECTIONS))
    return {**plan, "two_sided": True, "mirror_margins": True}


def accepted_copy(path: Path, to: Path) -> Path:
    """``path`` with every revision accepted: the pages as the writer sees them once the
    changes are taken (LibreOffice prints deleted text struck through otherwise)."""
    to.write_bytes(path.read_bytes())
    api.run("batch", {"doc": str(to), "ops": [{"op": "revision.accept", "all": True}]})
    return to


def test_apply_two_sided_headers_print_planned_text(tmp_path: Path) -> None:
    _require_tools()
    path = plain(tmp_path, 6, "applied.docx")
    plan = two_sided_plan(path)

    out = api.run("layout.apply", {"doc": str(path), "plan": plan, "untracked": True}).result

    kinds = {change["kind"] for change in out["changes"]}
    assert {"section.break", "header.text", "settings"} <= kinds
    assert all(c["tracked"] for c in out["changes"] if c["kind"] == "header.text")
    got = current(path)
    assert out["plan"] == got
    assert got["two_sided"] is True and got["mirror_margins"] is True
    for asked, have in zip(plan["sections"], got["sections"], strict=True):
        for group in ("header", "footer"):
            assert {k: have[group][k] for k in asked[group]} == asked[group]
        assert have["numbering"]["shown"] is True
        assert have["first_page_different"] is asked.get("first_page_different", False)

    printed_doc = accepted_copy(path, tmp_path / f"{TWO_SIDED.name}.docx")
    assert current(printed_doc)["sections"][1]["header"] == got["sections"][1]["header"]
    pdfs = export_pdfs([printed_doc], tmp_path / "pdf")
    assert label_failures(TWO_SIDED, pdfs[printed_doc.stem]) == []


def test_a_part_only_one_section_uses_is_rewritten_as_tracked_changes(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    before = path.read_bytes()
    plan = asked_plan(path, {"footer": {"default": "Page {PAGE} of {NUMPAGES}"}})

    preview = api.run("layout.apply", {"doc": str(path), "plan": plan, "dry_run": True}).result
    assert path.read_bytes() == before
    out = api.run("layout.apply", {"doc": str(path), "plan": plan}).result

    assert [c["kind"] for c in out["changes"]] == ["header.text"]
    assert out["changes"][0]["tracked"] and out["plan"] == preview["plan"] == current(path)
    assert current(path)["sections"][0]["footer"]["default"] == "Page {PAGE} of {NUMPAGES}"
    api.run("batch", {"doc": str(path), "ops": [{"op": "revision.reject", "all": True}]})
    assert current(path)["sections"][0]["footer"]["default"] == "pg:{PAGE}"


def test_untracked_changes_need_the_flag_and_null_drops_a_reference(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    header = current(path)["sections"][0]["header"]["default"]
    # Both sections share the original parts after the split: another text for the second
    # needs a part of its own, which Word cannot track.
    plan = asked_plan(path, {}, {"header": {"default": "hd:other"}})
    error = refused(path, plan, ErrorCode.UNSUPPORTED_CONTENT)
    assert error.details["needs"] == ["sections[1].header.default: a new header part"]

    out = api.run("layout.apply", {"doc": str(path), "plan": plan, "untracked": True}).result
    assert "header.new" in {c["kind"] for c in out["changes"]}
    got = current(path)["sections"]
    assert got[0]["header"]["default"] == header and got[1]["header"]["default"] == "hd:other"

    dropped = current(path)
    dropped["sections"][1]["header"]["default"] = None
    api.run("layout.apply", {"doc": str(path), "plan": dropped, "untracked": True})
    assert current(path)["sections"][1]["header"]["default"] is None
    # Without its own reference the section prints the previous section's header.
    assert printed_headers(path) == [header, header]


def printed_headers(path: Path, kind: str = "default") -> list[str | None]:
    """The header text each section prints, inheritance resolved: the text of the section
    whose reference it uses (sections.list gives its key, hdr<N>.<kind>)."""
    plan = current(path)["sections"]
    out = []
    for row in section_rows(Document(str(path))):
        key = row["headers"][kind]
        out.append(None if key is None else plan[int(key[3:].split(".")[0]) - 1]["header"][kind])
    return out


def three_sections_inheriting(tmp_path: Path) -> tuple[Path, str]:
    """Three sections; the first owns the default header, the other two inherit it."""
    path = plain(tmp_path, 6)
    api.run("layout.apply", {"doc": str(path), "plan": asked_plan(path, {}, {}, {})})
    header = current(path)["sections"][0]["header"]["default"]
    plan = current(path)
    for sect in plan["sections"][1:]:
        sect["header"]["default"] = None
    api.run("layout.apply", {"doc": str(path), "plan": plan, "untracked": True})
    assert printed_headers(path) == [header] * 3
    assert [s["header"]["default"] for s in current(path)["sections"]] == [header, None, None]
    return path, header


def refused_whole(path: Path, plan: dict[str, Any], section: int) -> None:
    """``plan`` is refused because it would alter inheriting ``section``; the file is kept."""
    before = path.read_bytes()
    for flags in ({}, {"untracked": True}, {"untracked": True, "dry_run": True}):
        with pytest.raises(DocControlError) as caught:
            api.run("layout.apply", {"doc": str(path), "plan": plan, **flags})
        assert caught.value.code is ErrorCode.UNSUPPORTED_CONTENT
        assert f"would alter section {section} " in str(caught.value)
        assert "not supported yet" in str(caught.value)
        assert path.read_bytes() == before


def test_a_new_part_a_later_section_inherits_is_refused(tmp_path: Path) -> None:
    path, header = three_sections_inheriting(tmp_path)
    plan = current(path)
    plan["sections"][1]["header"]["default"] = "B"
    refused_whole(path, plan, 3)
    assert printed_headers(path) == [header] * 3


def test_dropping_a_reference_a_later_section_inherits_is_refused(tmp_path: Path) -> None:
    path, header = three_sections_inheriting(tmp_path)
    plan = current(path)
    plan["sections"][0]["header"]["default"] = None
    refused_whole(path, plan, 2)
    assert printed_headers(path) == [header] * 3


def test_a_new_part_no_section_inherits_applies_and_reapplies_as_a_no_op(
    tmp_path: Path,
) -> None:
    path, header = three_sections_inheriting(tmp_path)
    plan = current(path)
    plan["sections"][2]["header"]["default"] = "B"
    plan["sections"][2]["footer"]["default"] = "F-{PAGE}"
    out = api.run("layout.apply", {"doc": str(path), "plan": plan, "untracked": True}).result
    assert out["plan"] == current(path)
    assert [s["header"]["default"] for s in out["plan"]["sections"]] == [header, None, "B"]
    assert out["plan"]["sections"][2]["footer"]["default"] == "F-{PAGE}"
    assert printed_headers(path) == [header, header, "B"]

    before = path.read_bytes()
    again = api.run(
        "layout.apply", {"doc": str(path), "plan": out["plan"], "untracked": True}
    ).result
    assert again["changes"] == [] and path.read_bytes() == before


def test_asking_the_inherited_text_changes_nothing(tmp_path: Path) -> None:
    path, header = three_sections_inheriting(tmp_path)
    before = path.read_bytes()
    plan = current(path)
    plan["sections"][2]["header"]["default"] = header
    out = api.run("layout.apply", {"doc": str(path), "plan": plan}).result
    assert out["changes"] == [] and path.read_bytes() == before
    assert out["plan"]["sections"][2]["header"]["default"] is None  # it keeps inheriting


def test_a_split_part_keeps_the_number_format_of_its_section(tmp_path: Path) -> None:
    path = build_docx(Scenario("roman", [Sec(4, fmt="lowerRoman")], []), tmp_path / "roman.docx")
    # The plan splits the section and states no format: both parts keep lowerRoman.
    out = api.run("layout.apply", {"doc": str(path), "plan": asked_plan(path, {}, {})}).result
    sections = out["plan"]["sections"]
    assert [s["numbering"]["format"] for s in sections] == ["lowerRoman", "lowerRoman"]
    assert [s["numbering"]["start"] for s in sections] == [None, None]


def test_the_write_is_pinned_to_the_bytes_the_dry_run_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = plain(tmp_path, 4)
    real_write = layout.apply_layout_edits

    def changed_in_between(doc_path: Path, edits: Any, **kwargs: Any) -> Any:
        if not kwargs.get("dry_run"):
            # The same document, other bytes: saved elsewhere between the check and the write.
            with zipfile.ZipFile(doc_path, "a") as z:
                z.comment = b"saved elsewhere"
        return real_write(doc_path, edits, **kwargs)

    monkeypatch.setattr(layout, "apply_layout_edits", changed_in_between)
    with pytest.raises(DocControlError) as caught:
        api.run("layout.apply", {"doc": str(path), "plan": asked_plan(path, *ROMAN_THEN_ARABIC)})
    assert caught.value.code is ErrorCode.DOC_CHANGED_ON_DISK
    assert len(section_rows(Document(str(path)))) == 1


def test_expect_sha256_is_checked_when_the_plan_needs_no_edits(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    request = {"doc": str(path), "plan": current(path), "expect": {"sha256": "0" * 64}}
    with pytest.raises(DocControlError) as caught:
        api.run("layout.apply", request)
    assert caught.value.code is ErrorCode.DOC_CHANGED_ON_DISK
    sha = Document(str(path)).sha256
    assert api.run("layout.apply", {**request, "expect": {"sha256": sha}}).result["changes"] == []


def test_an_unknown_top_level_plan_key_is_a_schema_error(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    plan = {**asked_plan(path, *ROMAN_THEN_ARABIC), "margins": {"top": 720}}
    assert "margins" in str(refused(path, plan, ErrorCode.SCHEMA_INVALID))


def test_a_second_apply_is_refused_while_a_change_is_unaccepted(tmp_path: Path) -> None:
    path = plain(tmp_path, 4)
    api.run("layout.apply", {"doc": str(path), "plan": asked_plan(path, *ROMAN_THEN_ARABIC)})
    plan = current(path)
    # The section after the new break holds a tracked change of its setup (w:sectPrChange).
    plan["sections"][1]["numbering"]["format"] = "upperRoman"
    assert "unresolved" in str(refused(path, plan, ErrorCode.UNSUPPORTED_CONTENT))
