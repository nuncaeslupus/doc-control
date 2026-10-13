"""Small builders for test documents (the full corpus generator is T5)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def document_xml(*paragraphs: str) -> str:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )


def make_docx(path: Path, *paragraphs: str, extra: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("_rels/.rels", RELS, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("word/document.xml", document_xml(*paragraphs), zipfile.ZIP_DEFLATED)
        for name, data in (extra or {}).items():
            zf.writestr(name, data, compress_type=zipfile.ZIP_STORED)
    return path


def symlink_or_skip(link: Path, target: Path) -> None:
    """Point ``link`` at ``target``; on Windows, skip the test if the account may not."""
    try:
        link.symlink_to(target)
    except OSError:
        if sys.platform != "win32":
            raise
        pytest.skip("symlinks not available")
