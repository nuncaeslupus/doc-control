"""Layout facts for `para.get` and `outline` (D-18), registered through the
:data:`doc_control.ops.read.PARA_FACTS` and :data:`doc_control.ops.read.OUTLINE_FACTS` hooks.

The definitions live in :mod:`doc_control.model.layout`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.model.layout import blank_before, centred, is_empty, page_break_before
from doc_control.model.parts import W, parse_part
from doc_control.model.stories import HF_KINDS, list_stories
from doc_control.ops.read import OUTLINE_FACTS, PARA_FACTS, ParagraphContext
from doc_control.ops.sections import SETTINGS_PART, flag, section_rows
from doc_control.registry import operation


def para_facts(ctx: ParagraphContext) -> dict[str, Any]:
    doc, para = ctx.doc, ctx.paragraph
    return {
        "empty": is_empty(doc, para),
        "blank_before": blank_before(doc, para),
        "heading_level": doc.heading_level(para),
        "centred": centred(doc, para),
        "page_break_before": page_break_before(doc, para),
    }


def outline_facts(ctx: ParagraphContext) -> dict[str, Any]:
    """Outline entries already carry ``level`` and ``style``; add the rest."""
    doc, para = ctx.doc, ctx.paragraph
    style = doc.style_id(para)
    style_def = doc.styles.get(style) if style is not None else None
    return {
        "style_name": style_def.name if style_def is not None else None,
        "blank_before": blank_before(doc, para),
        "centred": centred(doc, para),
        "page_break_before": page_break_before(doc, para),
    }


PARA_FACTS.append(para_facts)
OUTLINE_FACTS.append(outline_facts)


# --- layout.get: the page plan (LAYOUT T2) -------------------------------------------------

_PLAN_VERSION = 1


def _local(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1]


def _placeholder(instr: str) -> str | None:
    """``{PAGE}``, ``{NUMPAGES}`` or ``{chapter}`` for a field code, else ``None``."""
    words = instr.split(None, 1)
    if not words:
        return None
    code = words[0].upper()
    if code in ("PAGE", "NUMPAGES"):
        return "{" + code + "}"
    if (
        code == "STYLEREF"
        and len(words) > 1
        and words[1].replace('"', " ").split()[:2]
        == [
            "Heading",
            "1",
        ]
    ):
        return "{chapter}"
    return None


class _Text:
    """The text of one header or footer paragraph, its recognised fields as placeholders."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.fields: list[dict[str, Any]] = []

    def emit(self, text: str) -> None:
        if not self.fields:
            self.out.append(text)
        elif self.fields[-1]["result"]:
            self.fields[-1]["buf"].append(text)

    def field_end(self) -> None:
        if self.fields:
            top = self.fields.pop()
            found = _placeholder("".join(top["instr"]))
            self.emit(found if found is not None else "".join(top["buf"]))

    def walk(self, parent: ET.Element) -> None:
        for child in parent:
            name = _local(child)
            if name in ("del", "delText", "pPr", "rPr", "moveFrom"):
                continue
            if name == "fldSimple":
                found = _placeholder(child.get(W + "instr", ""))
                if found is not None:
                    self.emit(found)
                else:
                    self.walk(child)
            elif name == "fldChar":
                kind = child.get(W + "fldCharType")
                if kind == "begin":
                    self.fields.append({"instr": [], "buf": [], "result": False})
                elif kind == "separate" and self.fields:
                    self.fields[-1]["result"] = True
                elif kind == "end":
                    self.field_end()
            elif name == "instrText":
                if self.fields and not self.fields[-1]["result"]:
                    self.fields[-1]["instr"].append(child.text or "")
            elif name == "t":
                self.emit(child.text or "")
            elif name == "tab":
                self.emit("\t")
            else:
                self.walk(child)


def _story_text(root: ET.Element) -> str:
    paragraphs = []
    for para in root.findall(W + "p"):
        text = _Text()
        text.walk(para)
        while text.fields:  # a field left open by a truncated part
            text.field_end()
        paragraphs.append("".join(text.out))
    return "\n".join(paragraphs)


def _paragraph_starts(doc: Document, count: int) -> list[int]:
    """The 1-based paragraph number each section starts at (the first at 1)."""
    total = len(doc.index)
    starts = [1]
    for number in range(1, total + 1):
        if len(starts) >= count:
            break
        elem = doc.paragraph_element(doc.index.paragraph(number))
        if elem.find(f"{W}pPr/{W}sectPr") is not None:
            starts.append(min(number + 1, total))
    while len(starts) < count:
        starts.append(total)
    return starts


@operation(
    "layout.get",
    "read",
    "The document's page plan: per section where it starts, its break, page numbering, "
    "title page, and the header and footer text per kind (default, first, even), with PAGE, "
    "NUMPAGES and chapter (STYLEREF) fields as {PAGE}, {NUMPAGES} and {chapter}. A kind the "
    'section does not define is null (inherited). The plan carries "version": 1.',
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def layout_get(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    pkg = doc.pkg
    rows = section_rows(doc)
    mirror = False
    if SETTINGS_PART in pkg:
        mirror = flag(parse_part(pkg, SETTINGS_PART), "mirrorMargins")
    stories = {s.key: s for s in list_stories(pkg, ("headers",))}
    texts: dict[str, str] = {}

    def text_of(key: str) -> str:
        if key not in texts:
            texts[key] = _story_text(parse_part(pkg, stories[key].part))
        return texts[key]

    sections: list[dict[str, Any]] = []
    for row, start in zip(rows, _paragraph_starts(doc, len(rows)), strict=True):
        number = row["section"]
        active = {"default"}
        if row["title_page"]:
            active.add("first")
        if row["even_and_odd"]:
            active.add("even")
        shown = False
        plan: dict[str, dict[str, str | None]] = {}
        for group, which in (("header", "headers"), ("footer", "footers")):
            values: dict[str, str | None] = {}
            for kind in HF_KINDS:
                key = row[which][kind]
                values[kind] = None
                if key is None:
                    continue
                if stories[key].section == number:
                    values[kind] = text_of(key)
                if kind in active and "{PAGE}" in text_of(key):
                    shown = True
            plan[group] = values
        sections.append(
            {
                "from": str(doc.index.paragraph(start).address),
                "break": row["break"],
                "numbering": {
                    "shown": shown,
                    "start": row["page_numbers"]["start"],
                    "format": row["page_numbers"]["format"],
                },
                "header": plan["header"],
                "footer": plan["footer"],
                "first_page_different": row["title_page"],
            }
        )
    result = {
        "version": _PLAN_VERSION,
        "two_sided": bool(rows and rows[0]["even_and_odd"]),
        "mirror_margins": mirror,
        "sections": sections,
    }
    return Reply(result, doc=doc.envelope_doc())
