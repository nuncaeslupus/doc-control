"""`table.get` (STORIES T9): the table grid with merges, as LibreOffice reads it."""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from tests.corpus.generator import generate
from tests.corpus.validator import soffice_path
from tests.helpers import CONTENT_TYPES, RELS, soffice_dir

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"

# 4 grid columns: a horizontal merge, a vertical merge, and both at once.
SPEC = [
    [("A", 2, None), ("B", 1, None), ("C", 1, "restart")],
    [("D", 1, None), ("E", 1, None), ("F", 1, None), ("", 1, "continue")],
    [("G", 2, "restart"), ("H", 1, None), ("I", 1, "continue")],
    [("", 2, "continue"), ("J", 1, None), ("K", 1, None)],
]


def _tc(text: str, span: int, merge: str | None) -> str:
    props = f'<w:gridSpan w:val="{span}"/>' if span > 1 else ""
    props += "<w:vMerge/>" if merge == "continue" else ""
    props += '<w:vMerge w:val="restart"/>' if merge == "restart" else ""
    run = f"<w:r><w:t>{text}</w:t></w:r>" if text else ""
    return f"<w:tc><w:tcPr>{props}</w:tcPr><w:p>{run}</w:p></w:tc>"


def _merged_table() -> str:
    grid = '<w:gridCol w:w="2000"/>' * 4
    rows = "".join("<w:tr>" + "".join(_tc(*c) for c in row) + "</w:tr>" for row in SPEC)
    return f"<w:tbl><w:tblPr/><w:tblGrid>{grid}</w:tblGrid>{rows}</w:tbl>"


def _write(path: Path, body: str) -> Path:
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", xml)
    return path


def _grid(doc: Path, table: int) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = api.run(
        "table.get", {"doc": str(doc), "table": table}
    ).result["rows"]
    return rows


def _columns(rows: list[list[dict[str, Any]]]) -> list[list[tuple[Any, ...]]]:
    """Per row, the tokens of LibreOffice's cells: a cell (column span, row span, paragraph
    count) then one covered token per further column it spans; a vertical-merge
    continuation is covered."""
    out: list[list[tuple[Any, ...]]] = []
    for r, row in enumerate(rows):
        tokens: list[tuple[Any, ...]] = []
        for cell in row:
            if cell["vmerge"] == "continue":
                tokens += [("covered",)] * cell["span"]
                continue
            down = 1
            while r + down < len(rows) and any(
                c["col"] == cell["col"] and c["vmerge"] == "continue" for c in rows[r + down]
            ):
                down += 1
            tokens.append(("cell", cell["span"], down, len(cell["paragraphs"])))
            tokens += [("covered",)] * (cell["span"] - 1)
        out.append(tokens)
    return out


def _lo_tables(content: ET.Element) -> list[list[list[tuple[Any, ...]]]]:
    def rows_of(table: ET.Element) -> list[ET.Element]:
        found: list[ET.Element] = []
        for child in table:
            if child.tag == TABLE + "table-row":
                found.append(child)
            elif child.tag in (TABLE + "table-header-rows", TABLE + "table-rows"):
                found += [r for r in child if r.tag == TABLE + "table-row"]
        return found

    out = []
    for table in content.iter(TABLE + "table"):  # document order, a nested table after its host
        rows = []
        for row in rows_of(table):
            tokens: list[tuple[Any, ...]] = []
            for cell in row:
                if cell.tag == TABLE + "covered-table-cell":
                    tokens.append(("covered",))
                elif cell.tag == TABLE + "table-cell":
                    tokens.append(
                        (
                            "cell",
                            int(cell.get(TABLE + "number-columns-spanned", "1")),
                            int(cell.get(TABLE + "number-rows-spanned", "1")),
                            sum(1 for p in cell if p.tag == TEXT + "p"),
                        )
                    )
            rows.append(tokens)
        out.append(rows)
    return out


@pytest.mark.skipif(soffice_path() is None, reason="LibreOffice not installed")
def test_table_grid_matches_libreoffice(tmp_path: Path) -> None:
    soffice = soffice_path()
    assert soffice is not None
    mismatches: list[str] = []
    checked = 0
    with soffice_dir(tmp_path) as work:
        docs = {
            "tables": work / "tables.docx",
            "merged": _write(work / "merged.docx", _merged_table() + "<w:p/>"),
        }
        docs["tables"].write_bytes(generate("tables", 0))
        for name, doc in docs.items():
            subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={(work / 'profile').resolve().as_uri()}",
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    "odt",
                    "--outdir",
                    str(work),
                    str(doc),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
            with zipfile.ZipFile(work / f"{name}.odt") as zf:
                theirs = _lo_tables(ET.fromstring(zf.read("content.xml")))
            for number, expected in enumerate(theirs, start=1):
                checked += 1
                got = _columns(_grid(doc, number))
                if got != expected:
                    mismatches.append(f"{name} table {number}: {got} != {expected}")
            with pytest.raises(DocControlError) as err:
                _grid(doc, len(theirs) + 1)
            assert err.value.code == ErrorCode.ANCHOR_NOT_FOUND
    assert checked == 4  # three corpus tables (one nested) and the merged one
    print(f"table_grid_mismatches == {len(mismatches)}")
    assert mismatches == [], "\n".join(mismatches)


def test_table_get_columns_addresses_and_numbering(tmp_path: Path) -> None:
    text_box = (
        "<w:p><w:r><w:pict><w:txbxContent><w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>"
        "</w:txbxContent></w:pict></w:r></w:p>"
    )
    drawing = (
        '<w:p><w:r><w:drawing><a:tbl xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<a:tr><a:tc/></a:tr></a:tbl></w:drawing></w:r></w:p>"
    )
    last = "<w:p><w:r><w:t>z</w:t></w:r></w:p>"
    doc = _write(tmp_path / "t.docx", text_box + drawing + _merged_table() + last)
    rows = _grid(doc, 1)  # neither the text-box table nor the drawing's counts
    assert [[(c["col"], c["span"], c["vmerge"]) for c in row] for row in rows] == [
        [(1, 2, None), (3, 1, None), (4, 1, "restart")],
        [(1, 1, None), (2, 1, None), (3, 1, None), (4, 1, "continue")],
        [(1, 2, "restart"), (3, 1, None), (4, 1, "continue")],
        [(1, 2, "continue"), (3, 1, None), (4, 1, None)],
    ]
    listed = api.run("para.list", {"doc": str(doc)}).result["paragraphs"]
    by_text = {r["text"]: r for r in listed if "cell" in r}
    assert {r["cell"]["table"] for r in by_text.values()} == {1}
    assert rows[0][1]["paragraphs"] == [by_text["B"]["at"]]
    assert len(rows[3][0]["paragraphs"]) == 1
    via_address = api.run("table.get", {"doc": str(doc), "at": by_text["K"]["at"]}).result
    assert via_address == {"table": 1, "rows": rows}
    with pytest.raises(DocControlError) as err:
        api.run("table.get", {"doc": str(doc), "at": listed[-1]["at"]})  # "z" is in no table
    assert err.value.code == ErrorCode.ANCHOR_NOT_FOUND


def _write_raw(path: Path, document: str) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", document)
    return path


def test_table_get_reads_default_namespace_tags(tmp_path: Path) -> None:
    """`<tbl>` with WordprocessingML as the default namespace is a table, not zero tables."""
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?><document xmlns="{W_NS}" xmlns:w="{W_NS}"><body>'
        '<tbl><tblPr/><tblGrid><gridCol w:w="1"/><gridCol w:w="1"/></tblGrid>'
        '<tr><tc><tcPr><gridSpan w:val="2"/></tcPr><p><r><t>A</t></r></p></tc></tr>'
        '<tr><tc><tcPr><vMerge w:val="restart"/></tcPr><p><r><t>B</t></r></p></tc>'
        "<tc><p><r><t>C</t></r></p></tc></tr></tbl>"
        "<p><r><t>z</t></r></p></body></document>"
    )
    doc = _write_raw(tmp_path / "d.docx", document)
    rows = _grid(doc, 1)
    assert [
        [(c["col"], c["span"], c["vmerge"], len(c["paragraphs"])) for c in r] for r in rows
    ] == [
        [(1, 2, None, 1)],
        [(1, 1, "restart", 1), (2, 1, None, 1)],
    ]
    listed = api.run("para.list", {"doc": str(doc)}).result["paragraphs"]
    assert [r.get("cell", {}).get("table") for r in listed] == [1, 1, 1, None]


def test_table_get_reads_gridbefore_after_tblprex_and_single_quotes(tmp_path: Path) -> None:
    """A row's `w:tblPrEx` before its `w:trPr` does not hide `gridBefore`; single-quoted
    attribute values are read like double-quoted ones."""
    row = (
        "<w:tr><w:tblPrEx><w:tblW w:w='0' w:type='auto'/></w:tblPrEx>"
        "<w:trPr><w:gridBefore w:val='1'/></w:trPr>"
        "<w:tc><w:tcPr><w:gridSpan w:val='2'/></w:tcPr><w:p/></w:tc>"
        "<w:tc><w:tcPr><w:vMerge w:val='restart'/></w:tcPr><w:p/></w:tc></w:tr>"
        "<w:tr><w:trPr><w:gridBefore w:val='3'/></w:trPr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/>"
        "</w:tc></w:tr>"
    )
    doc = _write(tmp_path / "q.docx", f"<w:tbl><w:tblPr/><w:tblGrid/>{row}</w:tbl><w:p/>")
    rows = _grid(doc, 1)
    assert [[(c["col"], c["span"], c["vmerge"]) for c in r] for r in rows] == [
        [(2, 2, None), (4, 1, "restart")],
        [(4, 1, "continue")],
    ]


def test_empty_table_takes_a_number_and_has_no_rows(tmp_path: Path) -> None:
    """An empty `w:tbl` is still a table of the numbering: later tables keep their numbers."""
    body = (
        "<w:tbl/><w:tbl></w:tbl>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>x</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p/>"
    )
    doc = _write(tmp_path / "e.docx", body)
    assert _grid(doc, 1) == []
    assert _grid(doc, 2) == []
    assert [len(r) for r in _grid(doc, 3)] == [1]
    listed = api.run("para.list", {"doc": str(doc)}).result["paragraphs"]
    assert listed[0]["cell"]["table"] == 3
    with pytest.raises(DocControlError) as err:
        _grid(doc, 4)
    assert err.value.code == ErrorCode.ANCHOR_NOT_FOUND
