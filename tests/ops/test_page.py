"""`page.estimate`: explicit breaks, rendered marks, the word-count model, and the flag."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.model.document import Document
from doc_control.ops.paging import RESPONSE_BUDGET
from tests.corpus.generator import KINDS
from tests.helpers import CONTENT_TYPES, RELS
from tests.ops.bigdoc import write_big_doc

Run = Callable[..., tuple[int, dict[str, Any], int]]
CorpusWriter = Callable[..., Path]

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DOC_REL = (
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/styles" Target="styles.xml"/>'
)
BREAK = '<w:r><w:br w:type="page"/></w:r>'
MARK = "<w:r><w:lastRenderedPageBreak/></w:r>"


def para(text: str = "", ppr: str = "", before: str = "") -> str:
    run = f"<w:r><w:t>{text}</w:t></w:r>" if text else ""
    return f"<w:p><w:pPr>{ppr}</w:pPr>{before}{run}</w:p>"


def words(n: int) -> str:
    return " ".join(["word"] * n)


def sect(kind: str | None = None, extra: str = "") -> str:
    typ = f'<w:type w:val="{kind}"/>' if kind else ""
    return (
        f'<w:sectPr>{typ}<w:pgSz w:w="12240" w:h="15840"/>'
        f'<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:gutter="0"/>'
        f"{extra}</w:sectPr>"
    )


def build(
    tmp: Path, body: str, final: str | None = None, styles: str | None = None, name: str = "d"
) -> Path:
    path = tmp / f"{name}.docx"
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {NS}>'
        f"<w:body>{body}{final if final is not None else sect()}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", document)
        if styles is not None:
            zf.writestr("word/styles.xml", f"<w:styles {NS}>{styles}</w:styles>")
            zf.writestr(
                "word/_rels/document.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                f'relationships">{DOC_REL}</Relationships>',
            )
    return path


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def call(op: str, **request: Any) -> tuple[int, dict[str, Any], int]:
        code = main([op, "--json", json.dumps(request)])
        out = capsys.readouterr().out
        return code, json.loads(out), len(out.rstrip("\n").encode("utf-8"))

    return call


def estimate(run: Run, path: Path, **request: Any) -> dict[str, Any]:
    code, env, _ = run("page.estimate", doc=str(path), **request)
    assert code == 0, env
    result: dict[str, Any] = env["result"]
    return result


def pages_of(run: Run, path: Path) -> list[int]:
    return [p["page"] for p in estimate(run, path)["paragraphs"]]


def follow(run: Run, path: Path, **request: Any) -> tuple[list[dict[str, Any]], int]:
    """Every paragraph row, following `next`; also the number of responses."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    calls = 0
    while True:
        extra = {"cursor": cursor} if cursor else {}
        code, env, nbytes = run("page.estimate", doc=str(path), **request, **extra)
        assert code == 0, env
        assert nbytes <= RESPONSE_BUDGET
        rows += env["result"]["paragraphs"]
        calls += 1
        cursor = env["next"]
        if cursor is None:
            return rows, calls


def test_page_estimate_is_flagged_estimated(
    corpus_doc: CorpusWriter, run: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every `page.estimate` response (every corpus kind, every mode) says `estimated: true`."""
    paths = [corpus_doc(kind, 1) for kind in KINDS]
    paths.append(build(tmp_path, para("a") + BREAK + para("b") + MARK, name="marks"))
    responses: list[dict[str, Any]] = []
    for path in paths:
        first = Document(str(path)).index.paragraphs[0]
        for extra in ({}, {"by": "page"}, {"at": str(first.address)}, {"limit": 3}):
            code, env, _ = run("page.estimate", doc=str(path), **extra)
            assert code == 0, env
            responses.append(env["result"])
    unflagged = sum(1 for r in responses if r.get("estimated") is not True)
    with capsys.disabled():
        print(f"\npage_estimates_without_estimated_flag={unflagged} (of {len(responses)})")
    assert len(responses) == 4 * len(paths)
    assert unflagged == 0


def test_explicit_page_break_moves_the_following_paragraphs(run: Run, tmp_path: Path) -> None:
    body = para("one") + para("two") + f"<w:p>{BREAK}</w:p>" + para("three") + para("four")
    path = build(tmp_path, body)
    result = estimate(run, path)
    assert [r["page"] for r in result["paragraphs"]] == [1, 1, 1, 2, 2]
    assert result["page_count"] == 2
    assert result["basis"] == "model"
    assert {r["source"] for r in result["paragraphs"]} == {"model", "break"}
    assert result["paragraphs"][3]["source"] == "break"


def test_page_break_inside_a_paragraph_splits_it(run: Run, tmp_path: Path) -> None:
    body = para("before") + f"<w:p><w:r><w:t>a</w:t></w:r>{BREAK}<w:r><w:t>b</w:t></w:r></w:p>"
    body += f"<w:p><w:r><w:t>c</w:t></w:r>{BREAK}</w:p>" + para("d")
    rows = estimate(run, build(tmp_path, body))["paragraphs"]
    assert [r["page"] for r in rows] == [1, 1, 2, 3]
    assert rows[1]["end_page"] == 2  # starts before the break, ends after it
    assert rows[2]["page"] == 2
    assert rows[2]["end_page"] == 3  # the break ends it: its paragraph mark is on the next page


def test_page_break_before_direct_and_through_styles(run: Run, tmp_path: Path) -> None:
    styles = (
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="Chapter"><w:name w:val="Chapter"/>'
        '<w:basedOn w:val="Normal"/><w:pPr><w:pageBreakBefore/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="SubChapter"><w:name w:val="SubChapter"/>'
        '<w:basedOn w:val="Chapter"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Flow"><w:name w:val="Flow"/>'
        '<w:basedOn w:val="Chapter"/><w:pPr><w:pageBreakBefore w:val="0"/></w:pPr></w:style>'
    )
    style = lambda s: f'<w:pStyle w:val="{s}"/>'  # noqa: E731
    body = (
        para("first", style("Chapter"))  # first on its page: no empty page before it
        + para("body")
        + para("second", style("SubChapter"))  # inherited
        + para("flows", style("Flow"))  # switched off
        + para("direct", "<w:pageBreakBefore/>")
    )
    path = build(tmp_path, body, styles=styles)
    assert pages_of(run, path) == [1, 1, 2, 2, 3]


def test_section_breaks_next_page_continuous_and_parity(run: Run, tmp_path: Path) -> None:
    def closing(kind: str | None) -> str:
        return para("end", sect(kind))

    # The type on a sectPr says how *that* section starts.
    body = para("s1") + closing(None) + para("s2") + closing("continuous") + para("s3")
    body += closing("evenPage") + para("s4")
    rows = estimate(run, build(tmp_path, body, final=sect("oddPage")))["paragraphs"]
    # s1 | s2 continues on page 1 | s3 on a new even page 2 | s4 on the next odd page 3
    assert [r["page"] for r in rows] == [1, 1, 1, 1, 2, 2, 3]
    assert [r["source"] for r in rows][4] == "break"
    rows = estimate(run, build(tmp_path, body, final=sect("evenPage"), name="even"))["paragraphs"]
    assert rows[-1]["page"] == 4  # even page after page 2: page 3 is skipped


def test_rendered_page_marks_are_preferred_and_flagged(run: Run, tmp_path: Path) -> None:
    # Two short paragraphs the model keeps on one page; Word's marks say otherwise.
    body = para("one") + para("two", before=MARK) + para("three") + para("four", before=MARK)
    result = estimate(run, build(tmp_path, body))
    assert result["basis"] == "rendered"
    assert [r["page"] for r in result["paragraphs"]] == [1, 2, 2, 3]
    assert {r["source"] for r in result["paragraphs"]} == {"rendered"}
    assert result["page_count"] == 3


def test_rendered_marks_next_to_explicit_breaks_are_not_counted_twice(
    run: Run, tmp_path: Path
) -> None:
    # Word writes the hard break, then a mark where the next page starts.
    body = para("one") + f"<w:p>{BREAK}</w:p>" + para("two", before=MARK) + para("three")
    body += para("four", "<w:pageBreakBefore/>", before=MARK)
    result = estimate(run, build(tmp_path, body))
    assert [r["page"] for r in result["paragraphs"]] == [1, 1, 2, 2, 3]
    assert result["page_count"] == 3


def test_rendered_mark_inside_a_paragraph_ends_it_on_the_next_page(
    run: Run, tmp_path: Path
) -> None:
    body = para("a") + f"<w:p><w:r><w:t>x</w:t></w:r>{MARK}<w:r><w:t>y</w:t></w:r></w:p>"
    rows = estimate(run, build(tmp_path, body))["paragraphs"]
    assert (rows[1]["page"], rows[1]["end_page"]) == (1, 2)


def test_word_count_model_fills_pages(run: Run, tmp_path: Path) -> None:
    # US Letter, 1 in margins, 10 pt: 468 pt wide holds 15.6 words a line -> 100 words = 7 lines
    # of 11.5 pt; 648 pt high holds 56.3 lines (879 words) -> 8 such paragraphs a page.
    body = "".join(para(words(100)) for _ in range(24))
    path = build(tmp_path, body)
    result = estimate(run, path)
    got = [r["page"] for r in result["paragraphs"]]
    assert got == [1] * 8 + [2] * 8 + [3] * 8
    assert result["page_count"] == 3
    assert result["words_per_page"] == 879


def test_model_follows_font_size_margins_and_spacing(run: Run, tmp_path: Path) -> None:
    body = "".join(para(words(100)) for _ in range(24))
    base = estimate(run, build(tmp_path, body, name="base"))["page_count"]
    big_font = estimate(
        run,
        build(
            tmp_path,
            body,
            name="font",
            styles='<w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="28"/></w:rPr>'
            "</w:rPrDefault></w:docDefaults>",
        ),
    )["page_count"]
    wide_margins = (
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="2880" w:right="2880" '
        'w:bottom="2880" w:left="2880" w:gutter="0"/></w:sectPr>'
    )
    margins = estimate(run, build(tmp_path, body, final=wide_margins, name="margin"))
    spaced = estimate(
        run,
        build(
            tmp_path,
            "".join(
                para(words(100), '<w:spacing w:after="400" w:line="480" w:lineRule="auto"/>')
                for _ in range(24)
            ),
            name="spaced",
        ),
    )["page_count"]
    assert base == 3
    assert big_font > base and spaced > base and margins["page_count"] > base
    assert margins["words_per_page"] < 879


def test_long_paragraph_spans_pages(run: Run, tmp_path: Path) -> None:
    body = para("short") + para(words(2000)) + para("after")
    rows = estimate(run, build(tmp_path, body))["paragraphs"]
    assert rows[1]["page"] == 1 and rows[1]["end_page"] >= 3
    assert rows[2]["page"] == rows[1]["end_page"]


def test_at_returns_one_paragraph_and_by_page_groups(run: Run, tmp_path: Path) -> None:
    body = para("one") + BREAK.join(["<w:p>", "</w:p>"]) + para("two")
    path = build(tmp_path, body)
    listing = estimate(run, path)["paragraphs"]
    one = estimate(run, path, at=listing[2]["at"])
    assert (one["at"], one["page"], one["source"], one["estimated"]) == (
        listing[2]["at"],
        2,
        "break" if listing[2]["source"] == "break" else "model",
        True,
    )
    assert "paragraphs" not in one
    grouped = estimate(run, path, by="page")
    assert grouped["pages"] == [
        {"page": 1, "from": 1, "to": 2, "paragraphs": 2},
        {"page": 2, "from": 3, "to": 3, "paragraphs": 1},
    ]


def test_large_document_pages_with_cursors_and_plausible_count(run: Run, tmp_path: Path) -> None:
    path = write_big_doc(tmp_path / "big.docx")
    doc = Document(str(path))
    total_words = sum(len(p.text.split()) for p in doc.index.paragraphs)
    rows, calls = follow(run, path)
    assert calls > 1 and len(rows) == len(doc.index)
    assert [r["at"] for r in rows[:2]] == [str(p.address) for p in doc.index.paragraphs[:2]]
    seq = [r["page"] for r in rows]
    assert seq == sorted(seq) and seq[0] == 1
    head = estimate(run, path, limit=1)
    # Within a factor of 2 of words / words-per-page: sane order of magnitude.
    ideal = total_words / head["words_per_page"]
    assert 0.5 * ideal <= head["page_count"] <= 2 * ideal
    assert seq[-1] <= head["page_count"]


def test_page_estimate_cursor_is_tied_to_the_request(run: Run, tmp_path: Path) -> None:
    path = build(tmp_path, "".join(para("p") for _ in range(5)))
    code, env, _ = run("page.estimate", doc=str(path), limit=2)
    assert code == 0 and env["next"]
    code, env2, _ = run("page.estimate", doc=str(path), by="page", cursor=env["next"])
    assert code != 0 and env2["error"]["code"] == "SCHEMA_INVALID"
