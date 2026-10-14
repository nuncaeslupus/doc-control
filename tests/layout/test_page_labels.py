"""LAYOUT T1: the page-label checker, run against hand-made scenarios."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.corpus.validator import REQUIRE_ENV, soffice_path
from tests.layout.label_fixtures import (
    Scenario,
    Sec,
    build_docx,
    export_pdfs,
    label_failures,
)

TWO_SIDED_HEAD = {"header": "Book", "header_even": "Author"}

SCENARIOS = [
    Scenario(
        "front_matter_unnumbered_then_1",
        [Sec(2, shown=False), Sec(3, start=1)],
        ["-", "-", "1", "2", "3"],
    ),
    Scenario(
        "roman_then_arabic",
        [Sec(4, fmt="lowerRoman", start=1), Sec(2, start=1)],
        ["i", "ii", "iii", "iv", "1", "2"],
    ),
    Scenario(
        "counted_unnumbered_part_page",
        [Sec(2, start=1), Sec(1, shown=False), Sec(2)],
        ["1", "2", "-", "4", "5"],
    ),
    Scenario(
        "restart_per_part",
        [Sec(2, start=1), Sec(2, start=1), Sec(1, start=1)],
        ["1", "2", "1", "2", "1"],
    ),
    Scenario(
        "recto_chapter_starts",
        [Sec(1, start=1), Sec(2, brk="oddPage"), Sec(1, brk="oddPage")],
        ["1", "", "3", "4", "5"],
    ),
    Scenario(
        "two_sided_mirrored_headers",
        [Sec(2, start=1, **TWO_SIDED_HEAD), Sec(2, **TWO_SIDED_HEAD)],
        ["1", "2", "3", "4"],
        two_sided=True,
        expected_headers=["Book", "Author", "Book", "Author"],
    ),
]


def _require_tools() -> None:
    missing = []
    if soffice_path() is None:
        missing.append("soffice")
    if shutil.which("pdftotext") is None:
        missing.append("pdftotext")
    if not missing:
        return
    message = f"{' and '.join(missing)} not available"
    if os.environ.get(REQUIRE_ENV):
        pytest.fail(f"{REQUIRE_ENV} is set but {message}")
    pytest.skip(message)


def test_page_labels_of_handmade_scenarios(tmp_path: Path) -> None:
    _require_tools()
    # A deliberately mislabelled plan: the document restarts at 5 but the plan says 1.
    wrong = Scenario("mislabelled", [Sec(2, start=5)], ["1", "2"])
    scenarios = [*SCENARIOS, wrong]
    docs = [build_docx(s, tmp_path / f"{s.name}.docx") for s in scenarios]
    pdfs = export_pdfs(docs, tmp_path / "out")

    failures = []
    for scenario in SCENARIOS:
        failures += label_failures(scenario, pdfs[scenario.name])
    assert failures == [], failures

    reported = label_failures(wrong, pdfs["mislabelled"])
    assert len(reported) == 1 and "['5', '6']" in reported[0], reported
    # The checker is only trustworthy if the same document passes with the right plan.
    assert label_failures(replace(wrong, expected=["5", "6"]), pdfs["mislabelled"]) == []
