"""`paragraphs.list`: every paragraph with its layout facts in one list (T39).

The bulk counterpart of ``para.get``'s facts, for indexers: address, text, style, heading
level, centred, page break before, blank paragraphs before, empty; optionally the
normalised text (``text.normalized``) and the estimated page (``page.estimate``). Paged
like the other lists on the CLI and MCP; whole in-process (:mod:`doc_control.api`).
"""

from __future__ import annotations

from typing import Any

from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.model.layout import blank_before, centred, is_empty, page_break_before
from doc_control.model.normalized import normalized_text
from doc_control.model.pages import estimate_pages
from doc_control.ops.paging import PAGING_PROPERTIES, paged
from doc_control.registry import operation


@operation(
    "paragraphs.list",
    "read",
    "Every paragraph with its address, text, style and layout facts (optionally the "
    "normalised text and the estimated page), in document order.",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            "normalized": {
                "type": "boolean",
                "description": "Add `normalized`, the text as `text.normalized` gives it.",
            },
            "pages": {
                "type": "boolean",
                "description": "Add the estimated `page` and `page_label` (see page.estimate).",
            },
            **PAGING_PROPERTIES,
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def paragraphs_list(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    pages = estimate_pages(doc).rows if request.get("pages") else None
    names: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    for para in doc.index.paragraphs:
        style = doc.style_id(para)
        if style is not None and style not in names:
            found = doc.styles.get(style)
            names[style] = found.name if found is not None else style
        item: dict[str, Any] = {
            "at": str(para.address),
            "number": para.number,
            "text": para.text,
        }
        if request.get("normalized"):
            item["normalized"] = normalized_text(para.text)
        item.update(
            style=style,
            style_name=names.get(style) if style is not None else None,
            heading_level=doc.heading_level(para),
            centred=centred(doc, para),
            page_break_before=page_break_before(doc, para),
            blank_before=blank_before(doc, para),
            empty=is_empty(doc, para),
        )
        if pages is not None:
            row = pages[para.number - 1]
            item["page"] = row.page
            item["page_label"] = row.label
        items.append(item)
    return paged("paragraphs.list", doc, request, "paragraphs", items)
