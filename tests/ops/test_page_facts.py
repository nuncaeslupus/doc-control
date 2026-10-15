"""Page facts for bulk readers (T48).

Gate: ``page_fact_mismatches == 0``: a section break gives the next paragraph
``page_break_before`` exactly when the section it opens starts a page (its own closing
``w:sectPr`` says so, the body's for the last section), and the bulk read's page header
equals ``page.estimate``'s.
"""

from __future__ import annotations

from pathlib import Path

from doc_control import api
from tests.ops.test_page import MARK, build, para, sect, words


def _breaks(path: Path) -> list[bool]:
    with api.open(path) as doc:
        return [row["page_break_before"] for row in doc.paragraphs()]


def test_bulk_page_header_and_section_breaks(tmp_path: Path) -> None:
    mismatches = 0
    cases = [
        # (body, final sectPr, expected page_break_before per paragraph)
        (para("a", sect("continuous")) + para("b"), sect(), [False, True]),
        (para("a", sect()) + para("b"), sect("continuous"), [False, False]),
        (
            para("a", sect("continuous")) + para("b", sect("oddPage")) + para("c"),
            sect(),
            [
                False,
                True,
                True,
            ],
        ),
        (
            para("a", sect("nextPage")) + para("b", sect("continuous")) + para("c"),
            sect(),
            [
                False,
                False,
                True,
            ],
        ),
    ]
    # A real body sectPr carries r:id header and footer references and may hold a tracked
    # sectPrChange with the old type.
    refs = (
        '<w:headerReference w:type="default" r:id="rId8" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
    )
    change = (
        '<w:sectPrChange w:id="1"><w:sectPr><w:type w:val="nextPage"/></w:sectPr></w:sectPrChange>'
    )
    cases.append(
        (
            para("a", sect()) + para("b"),
            f'<w:sectPr>{refs}<w:type w:val="continuous"/>{change}</w:sectPr>',
            [False, False],
        )
    )
    for n, (body, final, expected) in enumerate(cases):
        mismatches += _breaks(build(tmp_path, body, final=final, name=f"s{n}")) != expected
    doc = build(tmp_path, para(words(50)) + para(words(50), before=MARK), name="marks")
    with api.open(doc) as d:
        bulk = d.run("para.list", {"pages": True})
        estimate = d.run("page.estimate")
    mismatches += (bulk["page_basis"], bulk["rendered_marks"], bulk["page_count"]) != (
        estimate["basis"],
        estimate["rendered_marks"],
        estimate["page_count"],
    )
    print(f"page_fact_mismatches == {mismatches} (of {len(cases) + 1} checks)")
    assert mismatches == 0
