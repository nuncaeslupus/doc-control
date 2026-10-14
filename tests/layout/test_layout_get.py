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
