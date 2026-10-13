"""Rendered page marks win where they cover the document (T42).

Gate: ``rendered_page_mismatches == 0``. On generated documents whose paragraphs vary from a
line to several pages' worth of words, with ``w:lastRenderedPageBreak`` marks at paragraph
starts and inside paragraphs, every paragraph up to the last mark starts on the page the
marks say and the page count is the number of marks plus one; the word-count model never
adds a page there however much text it thinks a page holds.
"""

from __future__ import annotations

import random
from pathlib import Path

from doc_control.model.document import Document
from doc_control.model.pages import estimate_pages
from tests.ops.test_page import MARK, build, para, words


def _doc(rng: random.Random, tmp: Path, name: str) -> tuple[Path, list[int], int, int]:
    """A document, the page each paragraph should start on, the number of marks and the
    index of the paragraph holding the last one."""
    body = ""
    expected: list[int] = []
    page = 1
    last = 0
    for k in range(rng.randint(20, 80)):
        size = rng.choice([5, 40, 120, 250])  # Word marks every page it lays out
        where = rng.random()
        if k and where < 0.25:  # Word started a page at this paragraph
            page += 1
            body += para(words(size), before=MARK)
            expected.append(page)
            last = k
        elif k and where < 0.35 and size > 10:  # a page starts inside it
            half = words(size // 2)
            body += f"<w:p><w:r><w:t>{half} </w:t></w:r>{MARK}<w:r><w:t>{half}</w:t></w:r></w:p>"
            expected.append(page)
            page += 1
            last = k
        else:
            body += para(words(size))
            expected.append(page)
    return build(tmp, body, name=name), expected, page - 1, last


def test_rendered_marks_win_where_they_cover(tmp_path: Path) -> None:
    rng = random.Random(42)
    mismatches = checked = 0
    for n in range(60):
        path, expected, marks, last = _doc(rng, tmp_path, f"d{n}")
        est = estimate_pages(Document(str(path)))
        got = [r.page for r in est.rows]
        mismatches += sum(
            g != e for g, e in zip(got[: last + 1], expected[: last + 1], strict=True)
        )
        checked += last + 1
        mismatches += est.pages < marks + 1
        mismatches += est.marks != marks
    print(f"rendered_page_mismatches == {mismatches} (of {checked} paragraphs)")
    assert mismatches == 0


def test_page_count_is_marks_plus_one_when_marks_cover(tmp_path: Path) -> None:
    body = "".join(para(words(1500), before=MARK if k else "") for k in range(5))
    est = estimate_pages(Document(str(build(tmp_path, body))))
    # each paragraph is longer than the model's page, but Word put each on its own page
    assert [r.page for r in est.rows] == [1, 2, 3, 4, 5]


def test_a_stray_mark_only_resyncs_the_model(tmp_path: Path) -> None:
    body = "".join(para(words(300)) for _ in range(150)) + para(words(5), before=MARK)
    est = estimate_pages(Document(str(build(tmp_path, body))))
    assert est.rows[149].page > 10  # the model still pages the long text before it
    assert est.rows[150].page == est.rows[149].end_page + 1
