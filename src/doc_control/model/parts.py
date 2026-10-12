"""Finding and parsing the supporting parts of a package (read-only)."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.zipio import Package

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
MS_REL_2011 = "http://schemas.microsoft.com/office/2011/relationships/"
DOCUMENT_RELS = "word/_rels/document.xml.rels"

W = f"{{{W_NS}}}"
W14 = f"{{{W14_NS}}}"
W15 = f"{{{W15_NS}}}"


def parse_part(pkg: Package, name: str) -> ET.Element:
    """Parse an XML part; malformed XML is ``NOT_A_DOCX``."""
    try:
        return ET.fromstring(pkg.read(name))
    except ET.ParseError as exc:
        raise DocControlError(
            ErrorCode.NOT_A_DOCX, f"{name} is not well-formed XML: {exc}", part=name
        ) from exc


def related_part(pkg: Package, rel_type: str, default: str) -> str | None:
    """The part ``word/document.xml`` relates to as ``rel_type`` (a full type URI).

    Falls back to ``default`` when the relationship is missing but the part exists.
    """
    if DOCUMENT_RELS in pkg:
        for rel in parse_part(pkg, DOCUMENT_RELS):
            if rel.get("Type") != rel_type or rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            name = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("word", target))
            )
            if name in pkg:
                return name
    return default if default in pkg else None
