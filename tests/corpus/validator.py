"""The corpus validator: structural lint, plus a LibreOffice round trip when available.

The .NET Open XML SDK validator is not used here (it arrives with T27). When
``soffice`` is on PATH each document is also converted to plain text with
LibreOffice headless; a document passes that step only if the conversion
produced output containing the text of its first non-empty body paragraph.
LibreOffice is more lenient than Word, so this catches gross breakage only.

Documents are identified by caller-supplied ids (short sha256 prefixes for
private documents), never by their original file names.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from tests.corpus.structural import W, validate_structure


def soffice_path() -> str | None:
    if os.environ.get("DOC_CONTROL_NO_SOFFICE"):
        return None
    return shutil.which("soffice") or shutil.which("libreoffice")


def validator_name() -> str:
    if soffice_path():
        return "structural lint + LibreOffice headless round trip"
    return "structural lint only (LibreOffice not found)"


def first_paragraph_text(data: bytes) -> str:
    """Visible text (``w:t`` only) of the first non-empty body paragraph."""
    with zipfile.ZipFile(BytesIO(data)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    for p in root.iter(W + "p"):
        text = " ".join("".join(t.text or "" for t in p.iter(W + "t")).split())
        if text:
            return text
    return ""


def _normalise(text: str) -> str:
    return " ".join(text.split())


def libreoffice_round_trip(docs: Mapping[str, bytes], workdir: Path) -> dict[str, list[str]]:
    """Convert every document to text in one soffice run; return problems per id."""
    soffice = soffice_path()
    if soffice is None:
        return {doc_id: [] for doc_id in docs}
    src = workdir / "lo-in"
    out = workdir / "lo-out"
    profile = workdir / "lo-profile"
    src.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for doc_id, data in docs.items():
        path = src / f"{doc_id}.docx"
        path.write_bytes(data)
        paths.append(str(path))
    subprocess.run(
        [
            soffice,
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless",
            "--norestore",
            "--convert-to",
            "txt:Text (encoded):UTF8",
            "--outdir",
            str(out),
            *paths,
        ],
        check=False,
        capture_output=True,
        timeout=900,
    )
    results: dict[str, list[str]] = {}
    for doc_id, data in docs.items():
        txt = out / f"{doc_id}.txt"
        if not txt.is_file():
            results[doc_id] = ["LibreOffice could not load the document"]
            continue
        expected = first_paragraph_text(data)[:40]
        got = _normalise(txt.read_text(encoding="utf-8", errors="replace"))
        if expected and _normalise(expected) not in got:
            results[doc_id] = ["LibreOffice output lacks the first paragraph's text"]
        else:
            results[doc_id] = []
    return results


def validate_documents(docs: Mapping[str, bytes], workdir: Path) -> dict[str, list[str]]:
    """Run the full validator over ``docs``; return ``{id: problems}``."""
    check_ids(docs)
    results = {doc_id: validate_structure(data) for doc_id, data in docs.items()}
    for doc_id, problems in libreoffice_round_trip(docs, workdir).items():
        results[doc_id] += problems
    return results


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def check_ids(docs: Mapping[str, bytes]) -> None:
    for doc_id in docs:
        if not _SAFE_ID.match(doc_id):
            raise ValueError(f"unsafe document id {doc_id!r}")
