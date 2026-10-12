"""Layout facts for `para.get` and `outline` (D-18), registered through the
:data:`doc_control.ops.read.PARA_FACTS` and :data:`doc_control.ops.read.OUTLINE_FACTS` hooks.

The definitions live in :mod:`doc_control.model.layout`.
"""

from __future__ import annotations

from typing import Any

from doc_control.model.layout import blank_before, centred, is_empty, page_break_before
from doc_control.ops.read import OUTLINE_FACTS, PARA_FACTS, ParagraphContext


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
