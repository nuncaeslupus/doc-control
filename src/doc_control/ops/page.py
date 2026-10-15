"""`page.estimate`: the estimated page of every paragraph, always marked as an estimate.

The model and its constants are documented in :mod:`doc_control.model.pages`. The list is
capped and continues with `next` like the other read operations.
"""

from __future__ import annotations

from typing import Any

from doc_control import outputs
from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.model.pages import CONSTANTS, NOTES, PageEstimate, estimate_pages
from doc_control.ops.paging import PAGING_PROPERTIES, paged
from doc_control.registry import operation


def _page_rows(est: PageEstimate) -> list[dict[str, Any]]:
    """One row per page that has a paragraph starting on it."""
    rows: list[dict[str, Any]] = []
    for row in est.rows:
        last = rows[-1] if rows else None
        if last is not None and last["page"] == row.page:
            last["to"] = row.number
            last["paragraphs"] += 1
        else:
            rows.append(
                {
                    "page": row.page,
                    "page_label": row.label,
                    "from": row.number,
                    "to": row.number,
                    "paragraphs": 1,
                }
            )
    return rows


_PAGE_ROW = {
    "page": outputs.integer("Estimated page (1-based)."),
    "page_label": outputs.PAGE_LABEL,
    "source": outputs.string("rendered (Word's page marks), break (explicit break) or model."),
    "end_page": outputs.integer("Page it ends on, when it runs onto another page."),
    "end_page_label": outputs.PAGE_LABEL,
}
_OUTPUT = outputs.result(
    ("estimated", "page_count", "basis", "words_per_page", "rendered_marks", "model", "notes"),
    estimated=outputs.boolean("Always true: pages are estimates."),
    page_count=outputs.integer("Estimated page count."),
    basis=outputs.string("model, or rendered+model when Word's page marks are used."),
    words_per_page=outputs.integer("Words per page the model assumes."),
    rendered_marks=outputs.integer("Rendered page-break marks found."),
    model=outputs.mapping("The model's constants."),
    notes=outputs.strings("Caveats about the estimate."),
    total=outputs.TOTAL,
    paragraphs=outputs.arr(
        "by paragraph: this page of rows.",
        outputs.obj("A paragraph.", at=outputs.ADDRESS, **_PAGE_ROW),
    ),
    pages=outputs.arr(
        "by page: this page of rows.",
        outputs.obj(
            "A page.",
            page=outputs.integer("Page."),
            page_label=outputs.PAGE_LABEL,
            to=outputs.integer("Last paragraph number on it."),
            paragraphs=outputs.integer("Paragraphs starting on it."),
            **{"from": outputs.integer("First paragraph number on it.")},
        ),
    ),
    at=outputs.string("With at: the paragraph."),
    **_PAGE_ROW,
)


@operation(
    "page.estimate",
    "read",
    "Estimated page of each paragraph (or of one): rendered page marks and explicit "
    "breaks first, a word-count model otherwise. Always `estimated: true`.",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            "at": {
                "type": "string",
                "minLength": 1,
                "description": "Only this paragraph, by address, e.g. p142#a7b2a7b2.",
            },
            "by": {
                "type": "string",
                "enum": ["paragraph", "page"],
                "description": "List one row per paragraph (default) or per page.",
            },
            **PAGING_PROPERTIES,
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
    output=_OUTPUT,
)
def page_estimate(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    est = estimate_pages(doc)
    head: dict[str, Any] = {
        "estimated": True,
        "page_count": est.pages,
        "basis": est.basis,
        "words_per_page": est.words_per_page,
        "rendered_marks": est.marks,
        "model": CONSTANTS,
        "notes": list(NOTES),
    }
    if "at" in request:
        para = doc.index.resolve_address(request["at"])
        row = est.rows[para.number - 1]
        result = {
            **head,
            "at": str(para.address),
            "page": row.page,
            "page_label": row.label,
            "source": row.source,
        }
        if row.end_page != row.page:
            result["end_page"] = row.end_page
            result["end_page_label"] = row.end_label
        return Reply(result, doc=doc.envelope_doc())
    if request.get("by") == "page":
        return paged("page.estimate", doc, request, "pages", _page_rows(est), head)
    items: list[dict[str, Any]] = []
    for row in est.rows:
        item: dict[str, Any] = {
            "at": str(doc.index.paragraph(row.number).address),
            "page": row.page,
            "page_label": row.label,
            "source": row.source,
        }
        if row.end_page != row.page:
            item["end_page"] = row.end_page
            item["end_page_label"] = row.end_label
        items.append(item)
    return paged("page.estimate", doc, request, "paragraphs", items, head)
