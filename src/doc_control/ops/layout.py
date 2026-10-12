"""Layout facts for `para.get` and `outline` (D-18): what a paragraph looks like on the page.

Registered through the :data:`doc_control.ops.read.PARA_FACTS` and
:data:`doc_control.ops.read.OUTLINE_FACTS` hooks, so the read handlers stay unchanged.

``empty``
    No visible text, or only whitespace (non-breaking spaces included).
``blank_before``
    How many empty paragraphs sit immediately before this one (0 when none).
``heading_level``
    1-9, from a direct outline level, else the style chain (``basedOn``), else a style
    named ``heading N``; ``None`` for body text. (An outline entry's ``level``.)
``style`` / ``style_name``
    The paragraph's style id (explicit, else the default paragraph style) and its name;
    ``para.get`` and ``outline`` already report them.
``centred``
    Centre alignment, direct (``w:jc``) or inherited through the style chain. Text
    "centred" with tabs or spaces is not detected: that is a recipe (T25).
``page_break_before``
    The paragraph starts a page: ``w:pageBreakBefore`` (direct wins over the style
    chain), a page break (``w:br w:type="page"``) before any visible content, or a
    section break (not ``continuous``) ending the previous paragraph.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from doc_control.model.document import Document
from doc_control.model.format import para_format
from doc_control.model.index import Paragraph
from doc_control.model.parts import W
from doc_control.ops.read import OUTLINE_FACTS, PARA_FACTS, ParagraphContext

_NOT_CONTENT = frozenset(W + name for name in ("pPr", "del", "moveFrom", "delText"))
_CONTENT = frozenset(W + name for name in ("t", "tab", "drawing", "pict", "object", "sym"))


def is_empty(para: Paragraph) -> bool:
    return not para.text.strip()


def blank_before(doc: Document, para: Paragraph) -> int:
    count = 0
    number = para.number - 1
    while number >= 1 and is_empty(doc.index.paragraphs[number - 1]):
        count += 1
        number -= 1
    return count


def centred(doc: Document, para: Paragraph) -> bool:
    ppr = para_format(doc.index.spans.parse_paragraph(para.span_index).find(W + "pPr"))
    alignment = ppr.get("alignment")
    if alignment is None:
        style = doc.style_id(para)
        alignment = doc.styles.effective(style)[0].get("alignment") if style else None
    return alignment == "center"


def _starts_with_page_break(elem: ET.Element) -> bool | None:
    """True/False once visible content or a page break decides; None while undecided."""
    for child in elem:
        tag = child.tag
        if not isinstance(tag, str) or tag in _NOT_CONTENT:
            continue
        if tag == W + "br":
            if child.get(W + "type") == "page":
                return True
            continue
        if tag in _CONTENT:
            if tag == W + "t" and not "".join(child.itertext()).strip():
                continue
            return False
        decided = _starts_with_page_break(child)
        if decided is not None:
            return decided
    return None


def _section_break_before(doc: Document, para: Paragraph) -> bool:
    if para.number < 2:
        return False
    prev = doc.index.spans.parse_paragraph(doc.index.paragraphs[para.number - 2].span_index)
    sect = prev.find(f"{W}pPr/{W}sectPr")
    if sect is None:
        return False
    kind = sect.find(W + "type")
    return kind is None or kind.get(W + "val", "nextPage") != "continuous"


def page_break_before(doc: Document, para: Paragraph) -> bool:
    elem = doc.index.spans.parse_paragraph(para.span_index)
    direct = para_format(elem.find(W + "pPr")).get("page_break_before")
    if direct is None:
        style = doc.style_id(para)
        direct = doc.styles.effective(style)[0].get("page_break_before") if style else None
    return bool(direct) or _starts_with_page_break(elem) is True or _section_break_before(doc, para)


def para_facts(ctx: ParagraphContext) -> dict[str, Any]:
    doc, para = ctx.doc, ctx.paragraph
    return {
        "empty": is_empty(para),
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
