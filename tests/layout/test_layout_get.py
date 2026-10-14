"""`layout.get` (LAYOUT T2): the page plan read from a document equals the one it was
written from."""

from __future__ import annotations

import copy
import zipfile
from pathlib import Path
from typing import Any

from doc_control import api
from doc_control.model.document import Document
from tests.layout.plans import SCENARIOS, fill_from, plan, section, write_plan


def _read(path: Path) -> dict[str, Any]:
    return dict(api.run("layout.get", {"doc": str(path)}).result)


def layout_get_mismatches(tmp_path: Path) -> list[str]:
    bad = []
    for i, (name, written) in enumerate(SCENARIOS.items()):
        path = write_plan(tmp_path / f"s{i}.docx", copy.deepcopy(written))
        expected = fill_from(copy.deepcopy(written), Document(str(path)))
        if _read(path) != expected:
            bad.append(name)
    return bad


def test_layout_get_reads_every_scenario(tmp_path: Path) -> None:
    assert layout_get_mismatches(tmp_path) == []


def test_layout_get_distinguishes_empty_from_inherited(tmp_path: Path) -> None:
    written = plan(section(header={"default": "A"}), section(header={"default": ""}), section())
    got = _read(write_plan(tmp_path / "d.docx", written))["sections"]
    assert [s["header"]["default"] for s in got] == ["A", "", None]
    assert got[0]["from"].startswith("p1#")


def test_on_off_values_read_as_off(tmp_path: Path) -> None:
    path = write_plan(
        tmp_path / "on.docx", plan(section(first_page_different=True), two_sided=True)
    )
    out = tmp_path / "off.docx"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/document.xml":
                data = data.replace(b"<w:titlePg/>", b'<w:titlePg w:val="0"/>')
            if item.filename == "word/settings.xml":
                data = data.replace(
                    b"<w:evenAndOddHeaders/>", b'<w:evenAndOddHeaders w:val="off"/>'
                )
            dst.writestr(item, data)
    rows = api.run("sections.list", {"doc": str(out)}).result["sections"]
    assert rows[0]["title_page"] is False and rows[0]["even_and_odd"] is False
    assert _read(out)["two_sided"] is False


def _with_footer(tmp_path: Path, inner: str) -> dict[str, Any]:
    """The plan read from a one-section document whose footer body is ``inner``."""
    from tests.layout.plans import NS

    path = write_plan(tmp_path / "f.docx", plan(section(footer={"default": "x"})))
    out = tmp_path / "f2.docx"
    mc = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/footer1_default.xml":
                data = f"<w:ftr {NS} {mc}>{inner}</w:ftr>".encode()
            dst.writestr(item, data)
    return _read(out)


def _page_field() -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def test_page_number_in_content_control_is_read(tmp_path: Path) -> None:
    got = _with_footer(
        tmp_path,
        f"<w:sdt><w:sdtPr><w:id w:val='1'/></w:sdtPr><w:sdtContent><w:p>{_page_field()}</w:p>"
        "</w:sdtContent></w:sdt>",
    )["sections"][0]
    assert got["footer"]["default"] == "{PAGE}"
    assert got["numbering"]["shown"] is True


def test_alternate_content_text_is_not_doubled(tmp_path: Path) -> None:
    def run(text: str) -> str:
        return f"<w:r><w:t>{text}</w:t></w:r>"

    inner = (
        "<w:p><mc:AlternateContent><mc:Choice Requires='wps'>"
        f"{run('Draft')}<w:r><w:txbxContent><w:p>{run('boxed')}</w:p></w:txbxContent></w:r>"
        f"</mc:Choice><mc:Fallback>{run('Draft')}</mc:Fallback></mc:AlternateContent></w:p>"
    )
    assert _with_footer(tmp_path, inner)["sections"][0]["footer"]["default"] == "Draft"


def test_styleref_variants_are_chapter(tmp_path: Path) -> None:
    def field(code: str) -> str:
        return (
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            f"<w:r><w:instrText> {code} </w:instrText></w:r>"
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>Intro</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        )

    codes = {
        'STYLEREF "heading 1"': "{chapter}",
        "STYLEREF 1 \\n": "{chapter}",
        'STYLEREF "Heading 1" \\* MERGEFORMAT': "{chapter}",
        'STYLEREF "Heading 2"': "Intro",
    }
    for code, want in codes.items():
        got = _with_footer(tmp_path, f"<w:p>{field(code)}</w:p>")["sections"][0]
        assert got["footer"]["default"] == want, code
