"""`para.list`: every paragraph with its layout facts in one list (T39).

The bulk counterpart of ``para.get``'s facts, for indexers: address, text, style, heading
level, centred, page break before, blank paragraphs before, empty; optionally the
normalised text (``text.normalize``) and the estimated page (``page.estimate``). Paged
like the other lists on the CLI and MCP; whole in-process (:mod:`doc_control.api`).
"""

from __future__ import annotations

from typing import Any

from doc_control import outputs
from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.model.layout import blank_before, centred, is_empty, page_break_before
from doc_control.model.normalized import normalized_text
from doc_control.model.pages import estimate_pages
from doc_control.model.storyorder import STORIES_PROPERTY, kinds_of, rows
from doc_control.ops.paging import PAGING_PROPERTIES, paged
from doc_control.registry import operation

_OUTPUT = outputs.result(
    ("total", "paragraphs"),
    total=outputs.TOTAL,
    paragraphs=outputs.arr(
        "This page of paragraphs, in document order.",
        outputs.obj(
            "A paragraph.",
            at=outputs.ADDRESS,
            number=outputs.integer("Paragraph number (1-based) in its story."),
            text=outputs.string("Full text."),
            normalized=outputs.string("With normalized: the text as text.normalize gives it."),
            style=outputs.nullable("string", "Paragraph style id."),
            style_name=outputs.nullable("string", "Paragraph style display name."),
            heading_level=outputs.nullable(
                "integer", "1-9 for a heading, null for a body paragraph."
            ),
            centred=outputs.boolean("Whether it is centred."),
            page_break_before=outputs.boolean("Whether it starts a page."),
            blank_before=outputs.integer("Empty paragraphs right before it."),
            empty=outputs.boolean("No visible text."),
            **outputs.STORY_FIELDS,
            cell=outputs.obj(
                "Inside a table: where.",
                table=outputs.integer("Table number."),
                row=outputs.integer("Row (0-based)."),
                col=outputs.integer("Grid column (0-based)."),
            ),
            page=outputs.integer("With pages: estimated page (body paragraphs)."),
            page_label=outputs.PAGE_LABEL,
        ),
    ),
    page_basis=outputs.string(
        "With pages: model, or rendered+model when Word's page marks are used."
    ),
    rendered_marks=outputs.integer("With pages: rendered page-break marks found."),
    page_count=outputs.integer("With pages: estimated page count."),
)


@operation(
    "para.list",
    "read",
    "Every paragraph with its address, text, style and layout facts (optionally the "
    "normalised text and the estimated page), in document order. heading_level is null for "
    "a body paragraph.",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            "normalized": {
                "type": "boolean",
                "description": "Add `normalized`, the text as `text.normalize` gives it.",
            },
            "pages": {
                "type": "boolean",
                "description": (
                    "Add the estimated `page` and `page_label` per paragraph, and "
                    "`page_basis`, `rendered_marks` and `page_count` (see page.estimate)."
                ),
            },
            "stories": STORIES_PROPERTY,
            **PAGING_PROPERTIES,
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
    output=_OUTPUT,
)
def paragraphs_list(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    estimate = estimate_pages(doc) if request.get("pages") else None
    pages = estimate.rows if estimate is not None else None
    head: dict[str, Any] = {}
    if estimate is not None:  # whether the pages are Word's own (rendered marks) or modelled
        head = {
            "page_basis": estimate.basis,
            "rendered_marks": estimate.marks,
            "page_count": estimate.pages,
        }
    names: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    for row in rows(doc, kinds_of(request)):
        view, para = row.view, row.para
        style = view.style_id(para)
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
            heading_level=view.heading_level(para),
            centred=centred(view, para),
            page_break_before=page_break_before(view, para),
            blank_before=blank_before(view, para),
            empty=is_empty(view, para),
        )
        item.update(row.fields)
        cell = view.cell_positions[para.number - 1]
        if cell is not None:
            item["cell"] = dict(zip(("table", "row", "col"), cell, strict=True))
        if pages is not None and not para.story:
            page = pages[para.number - 1]
            item["page"] = page.page
            item["page_label"] = page.label
        items.append(item)
    return paged("para.list", doc, request, "paragraphs", items, head)
