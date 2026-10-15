"""An .odt reads as its .docx does (T57): LibreOffice converts every corpus kind to .odt,
and doc-control's read of each must match its read of the .docx.

Gate: ``odt_parity_mismatches == 0`` over every corpus kind: the non-empty paragraphs
(texts, heading levels, style names, centring) and the novel's chapters agree, and the
.odt's estimated page count is within one page of LibreOffice's own (``meta.xml``), as its
soft page breaks are LibreOffice's layout (T59). Digits are masked in
texts, because LibreOffice recalculates field results (page numbers, dates) on conversion;
empty paragraphs are left out, because a paragraph deleted as a tracked change stays an
empty paragraph in a .docx but is gone from an .odt.
"""

from __future__ import annotations

import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from tests.corpus.generator import KINDS, generate
from tests.corpus.validator import soffice_path
from tests.helpers import soffice_dir

_DIGITS = re.compile(r"\d")


_PAGES = re.compile(rb'meta:page-count="([0-9]+)"')


def _facts(path: Path) -> tuple[list[tuple[Any, ...]], list[str], int]:
    with api.open(path) as doc:
        rows = [
            (_DIGITS.sub("#", r["text"]), r["heading_level"], r["style_name"], r["centred"])
            for r in doc.paragraphs()
            if r["text"]
        ]
        chapters = [c["text"] for c in doc.run("doc.segment", {"unit": "chapters"})["chapters"]]
        pages = doc.run("page.estimate", {})["page_count"]
    return rows, chapters, pages


def _libreoffice_pages(path: Path) -> int:
    match = _PAGES.search(zipfile.ZipFile(path).read("meta.xml"))
    assert match is not None
    return int(match.group(1))


def test_odt_parity_gate(tmp_path: Path) -> None:
    soffice = soffice_path()
    if soffice is None:
        pytest.skip("LibreOffice (soffice) is needed to convert the corpus to .odt")
    with soffice_dir(tmp_path) as work:
        _gate(soffice, work)


def _gate(soffice: str, tmp_path: Path) -> None:
    for kind in KINDS:
        (tmp_path / f"{kind}.docx").write_bytes(generate(kind, 1))
    subprocess.run(
        [
            soffice,
            f"-env:UserInstallation={(tmp_path / 'profile').resolve().as_uri()}",
            "--headless",
            "--norestore",
            "--convert-to",
            "odt",
            "--outdir",
            str(tmp_path),
            *(str(tmp_path / f"{kind}.docx") for kind in KINDS),
        ],
        check=True,
        capture_output=True,
        timeout=900,
    )
    mismatches = checks = 0
    for kind in KINDS:
        docx_rows, docx_chapters, _ = _facts(tmp_path / f"{kind}.docx")
        odt_rows, odt_chapters, odt_pages = _facts(tmp_path / f"{kind}.odt")
        checks += len(docx_rows) + 2
        mismatches += abs(odt_pages - _libreoffice_pages(tmp_path / f"{kind}.odt")) > 1
        mismatches += abs(len(docx_rows) - len(odt_rows))
        mismatches += sum(a != b for a, b in zip(docx_rows, odt_rows, strict=False))
        mismatches += docx_chapters != odt_chapters
    print(f"odt_parity_mismatches == {mismatches} (of {checks} checks)")
    assert mismatches == 0
