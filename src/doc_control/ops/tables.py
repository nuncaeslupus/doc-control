"""`table.get`: a table's grid, with merges and the paragraph addresses of each cell.

Tables are numbered as the ``cell.table`` field of a paragraph row is (see
:mod:`doc_control.model.tables`). ``table`` always names a table of the main story (the
body); ``at`` works in any story, a paragraph of a note, header or footer finding the table
that holds it, and the ``table`` number in the reply is then that story's own. An empty
``w:tbl`` takes a number and has no rows.
"""

from __future__ import annotations

from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import parse_range
from doc_control.model.document import Document
from doc_control.registry import operation


@operation(
    "table.get",
    "read",
    "One table's grid: its rows of cells, each with its first grid column (`col`), `span` "
    '(w:gridSpan), `vmerge` (null, "restart" or "continue") and the addresses of its own '
    "paragraphs. A nested table is a table of its own, numbered after the one holding it; an "
    "empty w:tbl takes a number and has no rows. `table` numbers the main story only; `at` "
    "works in any story (a footnote's table is numbered within that footnote's story).",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            "table": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Table number in the main story, 1-based in document order (the "
                    "`cell.table` of a paragraph row); text-box and DrawingML tables are not "
                    "counted. Tables of notes, headers and footers are reached with `at`."
                ),
            },
            "at": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Instead of `table`: any paragraph address in the table, in any story "
                    "(`fn3/p1#…`, `hdr1.default/p1#…`); the reply's `table` is then numbered "
                    "within that story."
                ),
            },
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def table_get(request: dict[str, Any]) -> Reply:
    if ("at" in request) == ("table" in request):
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "give exactly one of `table` and `at`")
    doc = Document(request["doc"])
    if "at" in request:
        view = doc.story_view(parse_range(request["at"]).start.story)
        para = view.index.resolve_address(request["at"])
        cell = view.cell_positions[para.number - 1]
        if cell is None:
            raise DocControlError(
                ErrorCode.ANCHOR_NOT_FOUND, f"{para.address} is not in a table", at=request["at"]
            )
        number = cell[0]
    else:
        view, number = doc, request["table"]
    grids = view.tables.grids
    if number > len(grids):
        raise DocControlError(
            ErrorCode.ANCHOR_NOT_FOUND,
            f"no table {number}: the document has {len(grids)}",
            table=number,
        )
    paragraphs = view.index.paragraphs
    rows = [
        [
            {
                "col": cell.col,
                "span": cell.span,
                "vmerge": cell.vmerge,
                "paragraphs": [str(paragraphs[n - 1].address) for n in cell.paragraphs],
            }
            for cell in row
        ]
        for row in grids[number - 1]
    ]
    return Reply({"table": number, "rows": rows}, doc=doc.envelope_doc())
