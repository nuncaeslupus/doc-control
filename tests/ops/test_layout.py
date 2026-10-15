"""Layout facts in `para.get` and `doc.outline` (T31), on documents with style chains."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.model.document import Document
from tests.helpers import CONTENT_TYPES, RELS

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
)
Run = Callable[..., dict[str, Any]]


def _style(sid: str, name: str, based: str | None = None, ppr: str = "") -> str:
    base = f'<w:basedOn w:val="{based}"/>' if based else ""
    return (
        f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>{base}'
        f"<w:pPr>{ppr}</w:pPr></w:style>"
    )


_NORMAL = (
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/></w:style>'
)
STYLES = (
    f"<w:styles {NS}>"
    + _NORMAL
    + _style("Chapter", "Chapter", "Normal", '<w:outlineLvl w:val="0"/><w:jc w:val="center"/>')
    + _style("ChapterBreak", "Chapter break", "Chapter", "<w:pageBreakBefore/>")
    + _style("Part", "Part", "ChapterBreak", '<w:jc w:val="left"/>')
    + _style("Sub", "Subtitle plain", "Chapter", '<w:outlineLvl w:val="1"/>')
    + _style("Heading2", "heading 2", "Normal")
    + _style("Body", "Body", "Normal", '<w:outlineLvl w:val="9"/>')
    + "</w:styles>"
)


def p(text: str = "", ppr: str = "", pre: str = "") -> str:
    run = f"<w:r>{pre}<w:t>{text}</w:t></w:r>" if (text or pre) else ""
    return f"<w:p><w:pPr>{ppr}</w:pPr>{run}</w:p>"


def style(sid: str) -> str:
    return f'<w:pStyle w:val="{sid}"/>'


BREAK_ONLY = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
TRAILING_BREAK = '<w:p><w:r><w:t>last.</w:t><w:br w:type="page"/></w:r></w:p>'
IMAGE_ONLY = "<w:p><w:r><w:drawing><wp:inline/></w:drawing></w:r></w:p>"

# (paragraph xml, expected facts); facts not listed are not checked.
CASES: list[tuple[str, dict[str, Any]]] = [
    (p("Opening"), {"empty": False, "blank_before": 0, "heading_level": None, "centred": False}),
    (p(""), {"empty": True, "blank_before": 0}),
    (p("  \u00a0 "), {"empty": True, "blank_before": 1}),
    (
        p("A title", style("Chapter")),
        {"blank_before": 2, "heading_level": 1, "centred": True, "page_break_before": False},
    ),
    (p("Next", style("ChapterBreak")), {"heading_level": 1, "page_break_before": True}),
    (p("Part", style("Part")), {"heading_level": 1, "centred": False, "page_break_before": True}),
    (p("Sub", style("Sub")), {"heading_level": 2, "centred": True, "page_break_before": False}),
    (p("H2", style("Heading2")), {"heading_level": 2, "style": "Heading2"}),
    (p("Direct", style("Body") + '<w:outlineLvl w:val="2"/>'), {"heading_level": 3}),
    (p("Body", style("Body")), {"heading_level": None, "blank_before": 0}),
    (
        p("Off", style("ChapterBreak") + '<w:pageBreakBefore w:val="0"/><w:jc w:val="right"/>'),
        {"page_break_before": False, "centred": False, "heading_level": 1},
    ),
    (p("Direct centre", '<w:jc w:val="center"/>'), {"centred": True, "heading_level": None}),
    (p("Direct on", "<w:pageBreakBefore/>"), {"page_break_before": True}),
    (p("Brk", pre='<w:br w:type="page"/>'), {"page_break_before": True, "empty": False}),
    (
        '<w:p><w:r><w:t>Text</w:t></w:r><w:r><w:br w:type="page"/></w:r></w:p>',
        {"page_break_before": False},
    ),
    (p("", "<w:sectPr><w:pgSz/></w:sectPr>"), {"empty": True, "page_break_before": True}),
    # A sectPr's type is how the section it closes starts: this paragraph's section is
    # closed by the continuous sectPr below, so it starts no page (T48).
    (p("After section"), {"page_break_before": False, "blank_before": 1}),
    (p("", '<w:sectPr><w:type w:val="continuous"/></w:sectPr>'), {"empty": True}),
    (p("After continuous"), {"page_break_before": False, "blank_before": 1}),
    (BREAK_ONLY, {"empty": True, "blank_before": 0, "page_break_before": True}),
    (p("Chapter Two", style("Chapter")), {"page_break_before": True, "blank_before": 0}),
    (TRAILING_BREAK, {"empty": False, "page_break_before": False}),
    (p("Chapter Three", style("Chapter")), {"page_break_before": True, "blank_before": 0}),
    (p("", '<w:sectPr><w:type w:val="nextColumn"/></w:sectPr>'), {"empty": True}),
    # The last section here has no closing sectPr (no body one): Word's default start, a
    # new page (T48). The nextColumn type above is how the section it closes starts.
    (p("After column"), {"page_break_before": True, "blank_before": 1}),
    (IMAGE_ONLY, {"empty": False, "blank_before": 0}),
    (p(""), {"empty": True, "blank_before": 0}),
    (IMAGE_ONLY, {"empty": False, "blank_before": 1}),
    (p("After image"), {"blank_before": 0, "page_break_before": False}),
    (BREAK_ONLY, {"empty": True, "blank_before": 0}),
    (p("Blank"), {"empty": False, "blank_before": 0, "page_break_before": True}),
]


def make_doc(path: Path, blocks: list[str], styles: str = STYLES) -> Path:
    document = f"<w:document {NS}><w:body>{''.join(blocks)}</w:body></w:document>"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles)
    return path


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def call(op: str, **request: Any) -> dict[str, Any]:
        code = main([op, "--json", json.dumps(request)])
        env = json.loads(capsys.readouterr().out)
        assert code == 0, env
        result: dict[str, Any] = env["result"]
        return result

    return call


def _addresses(doc: Path) -> list[str]:
    return [str(para.address) for para in Document(str(doc)).index.paragraphs]


def test_para_get_reports_blank_before_and_heading_level(
    tmp_path: Path, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = make_doc(tmp_path / "layout.docx", [c[0] for c in CASES])
    mismatches = 0
    checked = 0
    for (_, expected), at in zip(CASES, _addresses(doc), strict=True):
        got = run("para.get", doc=str(doc), at=at)
        for key, value in expected.items():
            checked += 1
            if got[key] != value:
                mismatches += 1
                print(f"MISMATCH {at} {key}: expected {value!r}, got {got[key]!r}")
    with capsys.disabled():
        print(f"\nlayout_fact_mismatches={mismatches} (of {checked} facts)")
    assert mismatches == 0


def test_heading_after_two_empty_paragraphs(tmp_path: Path, run: Run) -> None:
    doc = make_doc(tmp_path / "h.docx", [p("x"), p(), p(), p("Chapter One", style("Sub"))])
    result = run("para.get", doc=str(doc), at=_addresses(doc)[3])
    assert result["blank_before"] == 2
    assert result["heading_level"] == 2
    assert result["style"] == "Sub"
    assert result["style_name"] == "Subtitle plain"


def test_outline_reports_layout_facts(tmp_path: Path, run: Run) -> None:
    doc = make_doc(tmp_path / "o.docx", [p("x"), p(), p("Chapter One", style("ChapterBreak"))])
    (entry,) = run("doc.outline", doc=str(doc))["headings"]
    assert entry["level"] == 1
    assert entry["style"] == "ChapterBreak"
    assert entry["style_name"] == "Chapter break"
    assert entry["blank_before"] == 1
    assert entry["centred"] is True
    assert entry["page_break_before"] is True


def test_style_cycle_does_not_hang(tmp_path: Path, run: Run) -> None:
    cyclic = STYLES.replace(
        "</w:styles>", _style("A", "A", "B") + _style("B", "B", "A") + "</w:styles>"
    )
    doc = make_doc(tmp_path / "c.docx", [p("x", style("A"))], cyclic)
    result = run("para.get", doc=str(doc), at=_addresses(doc)[0])
    assert result["centred"] is False
    assert result["heading_level"] is None


def test_blank_before_stops_at_table_edges_and_cells(tmp_path: Path, run: Run) -> None:
    cell = "<w:tbl><w:tr><w:tc>{}</w:tc></w:tr></w:tbl>"
    doc = make_doc(
        tmp_path / "t.docx",
        [p(), cell.format(p() + p("x") + p()), p("after table"), cell.format(p("y"))],
    )
    got = [run("para.get", doc=str(doc), at=at)["blank_before"] for at in _addresses(doc)]
    # empty, cell: empty / x / empty, after table, cell: y
    assert got == [0, 0, 1, 0, 0, 0]


def test_alignment_from_doc_defaults(tmp_path: Path, run: Run) -> None:
    styles = STYLES.replace(
        _NORMAL,
        '<w:docDefaults><w:pPrDefault><w:pPr><w:jc w:val="center"/></w:pPr></w:pPrDefault>'
        f"</w:docDefaults>{_NORMAL}",
    )
    doc = make_doc(tmp_path / "d.docx", [p("a"), p("b", '<w:jc w:val="both"/>')], styles)
    got = [run("para.get", doc=str(doc), at=at)["centred"] for at in _addresses(doc)]
    assert got == [True, False]
