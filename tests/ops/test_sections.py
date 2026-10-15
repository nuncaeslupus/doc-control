"""`section.list` (STORIES T8): each section's page setup, as LibreOffice reads it."""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from tests.corpus.generator import generate
from tests.corpus.validator import soffice_path
from tests.helpers import soffice_dir

TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
STYLE = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
NUM_FORMATS = {
    "1": "decimal",
    "i": "lowerRoman",
    "I": "upperRoman",
    "a": "lowerLetter",
    "A": "upperLetter",
}
USAGE_BREAKS = {"right": "oddPage", "left": "evenPage"}
AREAS = {
    ("header", "default"): "header",
    ("header", "even"): "header-left",
    ("header", "first"): "header-first",
    ("footer", "default"): "footer",
    ("footer", "even"): "footer-left",
    ("footer", "first"): "footer-first",
}


def _masked(paragraphs: list[str]) -> str:
    """Digits masked: a PAGE field's result is cached differently by each."""
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", " ".join(paragraphs))).strip()


def _area(master: ET.Element, tag: str) -> str:
    area = master.find(STYLE + tag)
    return "" if area is None else _masked(["".join(p.itertext()) for p in area.iter(TEXT + "p")])


def _odt(doc: Path, work: Path) -> tuple[ET.Element, ET.Element]:
    soffice = soffice_path()
    assert soffice is not None
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
    with zipfile.ZipFile(work / f"{doc.stem}.odt") as zf:
        return ET.fromstring(zf.read("content.xml")), ET.fromstring(zf.read("styles.xml"))


def _lo_sections(content: ET.Element, styles: ET.Element) -> list[dict[str, Any]]:
    """One row per section: LibreOffice starts a page style at each section that changes it."""
    masters = {m.get(STYLE + "name"): m for m in styles.iter(STYLE + "master-page")}
    layouts = {p.get(STYLE + "name"): p for p in styles.iter(STYLE + "page-layout")}
    starts: dict[str, str | None] = {}  # paragraph style -> its page-number restart
    masters_of: dict[str, str] = {}
    for style in content.iter(STYLE + "style"):
        if style.get(STYLE + "master-page-name"):
            props = style.find(STYLE + "paragraph-properties")
            number = None if props is None else props.get(STYLE + "page-number")
            starts[style.get(STYLE + "name", "")] = number
            masters_of[style.get(STYLE + "name", "")] = style.get(STYLE + "master-page-name", "")
    names = [next(iter(masters))]
    numbers: list[str | None] = [None]
    for para in content.iter():
        if para.tag not in (TEXT + "p", TEXT + "h"):
            continue
        pstyle = para.get(TEXT + "style-name", "")
        if pstyle in starts:
            names.append(masters_of[pstyle])
            numbers.append(starts[pstyle])
    out = []
    for name, number in zip(names, numbers, strict=True):
        master = masters[name]
        layout = layouts[master.get(STYLE + "page-layout-name")]
        props = layout.find(STYLE + "page-layout-properties")
        assert props is not None
        out.append(
            {
                "break": USAGE_BREAKS.get(layout.get(STYLE + "page-usage", ""), "nextPage"),
                "title_page": master.find(STYLE + "header-first") is not None
                or master.find(STYLE + "footer-first") is not None,
                "even_and_odd": master.find(STYLE + "header-left") is not None,
                "start": int(number) if number and number.isdigit() else None,
                "format": NUM_FORMATS[props.get(STYLE + "num-format", "1")],
                "areas": {key: _area(master, tag) for key, tag in AREAS.items()},
            }
        )
    return out


@pytest.mark.skipif(soffice_path() is None, reason="LibreOffice not installed")
def test_sections_list_matches_libreoffice(tmp_path: Path) -> None:
    mismatches: list[str] = []
    counts = {}
    with soffice_dir(tmp_path) as work:
        for kind in ("headers", "novel"):
            doc = work / f"{kind}.docx"
            doc.write_bytes(generate(kind, 0))
            ours = api.run("section.list", {"doc": str(doc)}).result["sections"]
            rows = api.run("para.list", {"doc": str(doc), "stories": ["headers"]})
            texts: dict[str, list[str]] = {}
            for row in rows.result["paragraphs"]:
                if row.get("story") in ("header", "footer"):
                    texts.setdefault(row["at"].split("/")[0], []).append(row["text"])
            content, styles = _odt(doc, work)
            theirs = _lo_sections(content, styles)
            counts[kind] = len(ours)
            if len(ours) != len(theirs):
                mismatches.append(f"{kind}: {len(ours)} sections != LibreOffice's {len(theirs)}")
                continue
            for sect, lo in zip(ours, theirs, strict=True):
                got: dict[str, Any] = {
                    "break": sect["break"],
                    "title_page": sect["title_page"],
                    "even_and_odd": sect["even_and_odd"],
                    "start": sect["page_numbers"]["start"],
                    "format": sect["page_numbers"]["format"],
                }
                want = {k: v for k, v in lo.items() if k != "areas"}
                if got != want:
                    mismatches.append(f"{kind} section {sect['section']}: {got} != {want}")
                for (area, which), expected in lo["areas"].items():
                    if which == "first" and not sect["title_page"]:
                        continue  # never shown, so LibreOffice drops the area
                    address = sect[f"{area}s"][which]
                    text = _masked(texts.get(address, [])) if address else ""
                    if text != expected:
                        mismatches.append(
                            f"{kind} section {sect['section']} {area}/{which}: "
                            f"{address} {text!r} != {expected!r}"
                        )
    assert counts == {"headers": 3, "novel": 1}
    # section_read_mismatches == 0
    assert mismatches == [], "\n".join(mismatches)
