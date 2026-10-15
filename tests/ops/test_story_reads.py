"""Reads across stories (STORIES T4): notes, headers and footers read as LibreOffice reads them."""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from tests.corpus.generator import STORY_KINDS, generate
from tests.corpus.validator import soffice_path
from tests.helpers import soffice_dir

TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
STYLE = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
ALL = ["body", "notes", "headers"]
PAGE_AREAS = ("header", "header-left", "header-first", "footer", "footer-left", "footer-first")


def _rows(doc: Path) -> list[dict[str, Any]]:
    result: dict[str, Any] = api.run("para.list", {"doc": str(doc), "stories": ALL}).result
    rows: list[dict[str, Any]] = result["paragraphs"]
    return rows


def _text(elem: ET.Element) -> str:
    """A paragraph's text as LibreOffice wrote it, note citations and bodies left out."""
    out = [elem.text or ""]
    for child in elem:
        if child.tag == TEXT + "s":
            out.append(" " * int(child.get(TEXT + "c", "1")))
        elif child.tag == TEXT + "tab":
            out.append("\t")
        elif child.tag != TEXT + "note":
            out.append(_text(child))
        out.append(child.tail or "")
    return "".join(out)


def _masked(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text)).strip()


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


@pytest.mark.skipif(soffice_path() is None, reason="LibreOffice not installed")
def test_story_reads_match_libreoffice(tmp_path: Path) -> None:
    mismatches: list[str] = []
    with soffice_dir(tmp_path) as work:
        for kind in STORY_KINDS:
            doc = work / f"{kind}.docx"
            doc.write_bytes(generate(kind, 0))
            rows = _rows(doc)
            content, styles = _odt(doc, work)
            body = [r for r in rows if "story" not in r]
            # Notes: in mark order, with the number Word shows and the paragraph they follow.
            lo_notes = [
                (
                    n.get(TEXT + "note-class"),
                    (n.findtext(TEXT + "note-citation") or "").strip(),
                    [_text(p).strip() for p in n.iter(TEXT + "p")],
                )
                for n in content.iter(TEXT + "note")
            ]
            ours: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                if row.get("story") in ("footnote", "endnote"):
                    ours.setdefault(row["at"].split("/")[0], []).append(row)
            got = [
                (
                    r[0]["story"],
                    r[0]["note"]["number"],
                    [x["text"].strip() for x in r],
                )
                for r in ours.values()
            ]
            if kind == "notes":
                # The kind restarts footnotes per section (numRestart eachSect): Word shows
                # the second section's first footnote as 1, LibreOffice (no per-section
                # restart) as 3. Word's number is the one asserted.
                assert got[4][:2] == ("footnote", "1") and lo_notes[4][:2] == ("footnote", "3")
                lo_notes[4] = ("footnote", "1", lo_notes[4][2])
            if got != lo_notes:
                mismatches.append(f"{kind}: notes {got} != {lo_notes}")
            for note_rows in ours.values():
                of = note_rows[0]["note"]["of"]
                holder = rows.index(next(r for r in body if r["at"] == of))
                if rows[holder + 1]["at"] != note_rows[0]["at"]:
                    mismatches.append(f"{kind}: {note_rows[0]['at']} not after {of}")
            # Headers and footers: every one we read is a page style's in LibreOffice.
            # Digits masked: a PAGE field's result is the layout's, cached differently by each.
            lo_pages = {
                _masked(_text(p))
                for tag in PAGE_AREAS
                for area in styles.iter(STYLE + tag)
                for p in area.iter(TEXT + "p")
            }
            for row in rows:
                if row.get("story") in ("header", "footer"):
                    text = _masked(row["text"])
                    if text not in lo_pages:
                        mismatches.append(f"{kind}: {row['at']} {text!r} not in {lo_pages}")
    # story_read_mismatches == 0
    assert mismatches == [], "\n".join(mismatches)


def test_default_reads_stay_body_only(tmp_path: Path) -> None:
    doc = tmp_path / "notes.docx"
    doc.write_bytes(generate("notes", 0))
    plain = api.run("para.list", {"doc": str(doc)}).result["paragraphs"]
    assert all("story" not in r for r in plain)
    assert [r["at"] for r in plain] == [r["at"] for r in _rows(doc) if "story" not in r]
    found = api.run("text.find", {"doc": str(doc), "query": "Board"}).result
    assert found["total"] == 0  # "Board" only starts a footnote paragraph
    found = api.run("text.find", {"doc": str(doc), "query": "Board", "stories": ALL}).result
    assert [m["at"].split("/")[0] for m in found["matches"]] == ["fn2"]
    at = found["matches"][0]["at"].split(":")[0]
    got = api.run("para.get", {"doc": str(doc), "at": at}).result
    assert got["story"] == "footnote" and got["note"]["id"] == 2


def test_table_cells_and_story_aware_locate_outline_stats(tmp_path: Path) -> None:
    doc = tmp_path / "tables.docx"
    doc.write_bytes(generate("tables", 0))
    rows = api.run("para.list", {"doc": str(doc)}).result["paragraphs"]
    cells = [r for r in rows if "cell" in r]
    assert cells
    for r in cells:  # the corpus writes "R<row>C<col>" into each cell
        m = re.match(r"R(\d+)C(\d+)", r["text"])
        assert m is not None
        assert (r["cell"]["row"], r["cell"]["col"]) == (int(m[1]), int(m[2]))
    assert {r["cell"]["table"] for r in cells} == {1, 2, 3}  # nested tables count too

    notes = tmp_path / "notes.docx"
    notes.write_bytes(generate("notes", 0))
    every = _rows(notes)
    note = next(r for r in every if r.get("story") == "footnote")
    located = api.run("para.locate", {"doc": str(notes), "at": note["at"]}).result["paragraphs"]
    assert located[0]["at"] == note["at"]
    assert located[0]["note"] == note["note"]
    assert located[0]["mark"]["at"] == note["note"]["of"]  # where the mark sits

    headers = tmp_path / "headers.docx"
    headers.write_bytes(generate("headers", 0))
    plain = api.run("doc.outline", {"doc": str(headers)}).result["headings"]
    wide = api.run("doc.outline", {"doc": str(headers), "stories": ALL}).result["headings"]
    assert plain == wide  # the headers kind has headings in the body only

    body_only = api.run("doc.stats", {"doc": str(notes)}).result
    counted = api.run("doc.stats", {"doc": str(notes), "stories": ALL}).result
    assert "stories" not in body_only
    assert {k: v for k, v in counted.items() if k != "stories"} == body_only
    assert counted["stories"]["notes"]["stories"] == 6
    assert counted["stories"]["notes"]["paragraphs"] == sum(
        1 for r in every if r.get("story") in ("footnote", "endnote")
    )


def test_find_cursor_across_stories(tmp_path: Path) -> None:
    doc = tmp_path / "notes.docx"
    doc.write_bytes(generate("notes", 0))
    query = {"doc": str(doc), "query": "the", "ignore_case": True, "limit": 3}
    whole = api.run("text.find", {**query, "limit": 200, "stories": ALL}).result["matches"]
    pages: list[str] = []
    cursor = None
    while True:
        request = {**query, "stories": ALL} | ({"cursor": cursor} if cursor else {})
        reply = api.run("text.find", request)
        pages += [m["at"] for m in reply.result["matches"]]
        cursor = reply.next_cursor
        if cursor is None:
            break
    assert pages == [m["at"] for m in whole]
    assert any(at.startswith("fn") for at in pages)
    # The same stories in another order resume the same search.
    first = api.run("text.find", {**query, "stories": ALL})
    again = {**query, "stories": ["headers", "notes", "body"], "cursor": first.next_cursor}
    assert api.run("text.find", again).result["matches"]
