"""`section.list`: each section's page setup, read-only (groundwork for changing it).

A header or footer a section does not define is the previous section's, so the addresses
reported here are those of the section that defines the part (see
:mod:`doc_control.model.stories`).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.model.parts import W, parse_part
from doc_control.model.stories import HF_KINDS, list_stories, section_properties
from doc_control.registry import operation

SETTINGS_PART = "word/settings.xml"


def _attr(parent: ET.Element, tag: str, name: str) -> str | None:
    elem = parent.find(W + tag)
    return None if elem is None else elem.get(W + name)


def flag(parent: ET.Element, tag: str) -> bool:
    """An on/off element (``w:titlePg``): present and not ``w:val`` 0, false or off."""
    child = parent.find(W + tag)
    return child is not None and (child.get(W + "val") or "1").lower() not in ("0", "false", "off")


@operation(
    "section.list",
    "read",
    (
        "List each section's page setup: break type, title page, even/odd headers, "
        "page-number start and format, and the header and footer part address per kind "
        "(default, first, even). Use layout.get for the same plan with the header and "
        "footer text, in the form layout.apply takes back. Read-only."
    ),
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def sections_list(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    return Reply({"sections": section_rows(doc)}, doc=doc.envelope_doc())


def section_rows(doc: Document) -> list[dict[str, Any]]:
    """The rows `section.list` returns, for other operations built on them."""
    pkg = doc.pkg
    even_and_odd = False
    if SETTINGS_PART in pkg:
        even_and_odd = flag(parse_part(pkg, SETTINGS_PART), "evenAndOddHeaders")
    used: dict[tuple[str, str, int], str] = {}
    for story in list_stories(pkg, ("headers",)):
        for number in story.used_by_sections:
            used[(story.kind, story.hf_kind or "", number)] = story.key
    rows: list[dict[str, Any]] = []
    for number, sect in enumerate(section_properties(pkg), start=1):
        start = _attr(sect, "pgNumType", "start")
        rows.append(
            {
                "section": number,
                "break": _attr(sect, "type", "val") or "nextPage",
                "title_page": flag(sect, "titlePg"),
                "even_and_odd": even_and_odd,
                "page_numbers": {
                    "start": int(start) if start is not None and start.isdigit() else None,
                    "format": _attr(sect, "pgNumType", "fmt") or "decimal",
                },
                "headers": {k: used.get(("header", k, number)) for k in HF_KINDS},
                "footers": {k: used.get(("footer", k, number)) for k in HF_KINDS},
            }
        )
    return rows
