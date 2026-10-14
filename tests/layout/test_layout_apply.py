"""`layout.apply` (LAYOUT T4): breaks and numbering reach the plan, and LibreOffice prints it.

The gate ``page_label_mismatches == 0``: each numbering scenario is applied to a plain
document (one section, a page per paragraph, a footer printing ``pg:<PAGE>``), and the page
numbers in LibreOffice's PDF equal the plan.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
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
