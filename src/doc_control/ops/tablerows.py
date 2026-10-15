"""`table.row.insert` and `table.row.delete`: tracked changes to the rows of a body table.

Tables are numbered as `table.get` numbers them (the ``table`` field is always a table of
the main story; rows are 1-based). Both are batch kinds and standalone operations, and
every number in a batch refers to the document as read.

``table.row.insert`` adds a row after row ``after_row`` (``0``: before the first row) with
one text per cell (``cells``). The new row copies the row it follows (the first row for
``after_row`` 0): its row properties, and each cell's width, span, borders and first
paragraph's properties, but not a vertical merge. A row insertion is ``w:trPr/w:ins``;
the text is a tracked insertion in a paragraph whose mark is inserted too, so rejecting the
row removes it whole, in Word and in LibreOffice.

``table.row.delete`` deletes row ``row``: ``w:trPr/w:del``, every run of its cells a tracked
deletion and every paragraph mark in them a deleted one. Rejecting gives the row back.

Both are refused (``UNSUPPORTED_CONTENT``) where the row crosses a vertical merge: deleting
a row with any ``w:vMerge`` cell, or inserting between two rows that a merge spans, would
tear the merged cell. A row with a nested table is refused too.

A row is not a paragraph, so the write declares byte spans (see
:class:`doc_control.safety.verify.Footprint`) and the undo log keeps whole rows.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from doc_control import outputs
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.index import build_index
from doc_control.model.parts import W
from doc_control.model.tables import Cell
from doc_control.ooxml.revisions import (
    DEFAULT_AUTHOR,
    ParagraphEditor,
    RevisionContext,
    format_author,
    format_date,
    inserted_content,
    new_paragraph,
    used_ids,
)
from doc_control.ops.tracked import check_name, check_text
from doc_control.registry import operation
from doc_control.safety.history import record, to_text
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, Footprint, write_verified
from doc_control.schema import validate

__all__ = [
    "ROW_SCHEMAS",
    "DeleteRow",
    "InsertRow",
    "apply_row_ops",
    "row_op_from_dict",
]

_TABLE: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "description": (
        "Table number as `table.get` numbers it: 1-based, in document order, main story only "
        "(a table in a note, header or footer cannot be named here)."
    ),
}
ROW_SCHEMAS: dict[str, dict[str, Any]] = {
    "table.row.insert": {
        "type": "object",
        "properties": {
            "op": {"enum": ["table.row.insert"]},
            "table": _TABLE,
            "after_row": {
                "type": "integer",
                "minimum": 0,
                "description": "Insert after this row (1-based); 0 inserts before the first row.",
            },
            "cells": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "One text per cell of the row it follows (the first row for after_row 0); "
                    "a horizontally merged cell is one cell."
                ),
            },
        },
        "required": ["op", "table", "after_row", "cells"],
        "additionalProperties": False,
    },
    "table.row.delete": {
        "type": "object",
        "properties": {
            "op": {"enum": ["table.row.delete"]},
            "table": _TABLE,
            "row": {"type": "integer", "minimum": 1, "description": "The row, 1-based."},
        },
        "required": ["op", "table", "row"],
        "additionalProperties": False,
    },
}
_SUMMARY = {
    "table.row.insert": (
        "Insert a row into a body table as a tracked change (w:trPr/w:ins, tracked text), "
        "after row `after_row`, one text per cell. Refused between two rows that a vertical "
        "merge spans."
    ),
    "table.row.delete": (
        "Delete a row of a body table as a tracked change (w:trPr/w:del, tracked deleted "
        "runs). Refused for a row with a vertical merge (w:vMerge) cell."
    ),
}


@dataclass(frozen=True)
class InsertRow:
    table: int
    after_row: int
    cells: tuple[str, ...]


@dataclass(frozen=True)
class DeleteRow:
    table: int
    row: int


RowOp = InsertRow | DeleteRow


def row_op_from_dict(raw: Mapping[str, Any]) -> RowOp:
    """Validate a ``{"op": "table.row.insert", …}`` object and build the operation."""
    op = raw.get("op")
    if not isinstance(op, str) or op not in ROW_SCHEMAS:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"unknown table row operation: {op!r}",
            expected=list(ROW_SCHEMAS),
        )
    validate(dict(raw), ROW_SCHEMAS[op])
    if op == "table.row.delete":
        return DeleteRow(raw["table"], raw["row"])
    for i, text in enumerate(raw["cells"]):
        check_text(text, f"cells[{i}]")
    return InsertRow(raw["table"], raw["after_row"], tuple(raw["cells"]))


def _request(op: RowOp) -> dict[str, Any]:
    if isinstance(op, DeleteRow):
        return {"op": "table.row.delete", "table": op.table, "row": op.row}
    return {
        "op": "table.row.insert",
        "table": op.table,
        "after_row": op.after_row,
        "cells": list(op.cells),
    }


# -- planning -----------------------------------------------------------------------------

_TR_OPEN = re.compile(rb"<((?:[\w.-]+:)?)tr(?=[\s/>])")
_TBL_OPEN = re.compile(rb"<(?:[\w.-]+:)?tbl(?=[\s/>])")
_NOT_COPIED_ROW = (W + "ins", W + "del", W + "trPrChange", W + "tblHeader")
_NOT_COPIED_CELL = (W + "vMerge", W + "cellIns", W + "cellDel", W + "cellMerge", W + "tcPrChange")


@dataclass
class _Cut:
    start: int
    end: int
    data: bytes
    seq: int


class _Plan:
    def __init__(self, doc: Document, ctx: RevisionContext) -> None:
        self.doc = doc
        self.ctx = ctx
        self.spans = doc.index.spans
        self.data = self.spans.data
        self.cuts: list[_Cut] = []
        self.changes: list[dict[str, Any]] = []
        self.previews: list[dict[str, Any]] = []
        self.deleted: set[tuple[int, int]] = set()
        self.new_paragraphs: list[tuple[int, list[str]]] = []  # (insert offset, texts)
        self.emptied: set[int] = set()  # paragraph numbers whose text is gone

    # -- looking up --------------------------------------------------------------------

    def row(self, table: int, row: int) -> tuple[list[Cell], tuple[int, int]]:
        tables = self.doc.tables
        if table > len(tables.grids):
            raise DocControlError(
                ErrorCode.ANCHOR_NOT_FOUND,
                f"no table {table}: the document has {len(tables.grids)}",
                table=table,
            )
        rows = tables.grids[table - 1]
        if not 1 <= row <= len(rows):
            raise DocControlError(
                ErrorCode.ANCHOR_NOT_FOUND,
                f"table {table} has {len(rows)} rows, not {row}",
                table=table,
                row=row,
            )
        start, end = tables.row_spans[table - 1][row - 1]
        if end < 0:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT, f"row {row} of table {table} is not closed"
            )
        return rows[row - 1], (start, end)

    def _near(self, cells: list[Cell], where: str) -> int:
        """Span index of a paragraph of the row: the scope its elements are written in."""
        for cell in cells:
            if cell.paragraphs:
                return self.doc.index.paragraph(cell.paragraphs[0]).span_index
        raise DocControlError(ErrorCode.UNSUPPORTED_CONTENT, f"{where} has no paragraph")

    def _cut(self, start: int, end: int, data: bytes) -> None:
        self.cuts.append(_Cut(start, end, data, len(self.cuts)))

    # -- insert ------------------------------------------------------------------------

    def insert(self, number: int, op: InsertRow) -> None:
        where = f"table {op.table}"
        self.row(op.table, 1)  # the table exists and has rows
        count = len(self.doc.tables.grids[op.table - 1])
        if op.after_row > count:
            raise DocControlError(
                ErrorCode.ANCHOR_NOT_FOUND,
                f"{where} has {count} rows, not {op.after_row}",
                table=op.table,
                row=op.after_row,
            )
        template_no = max(op.after_row, 1)
        cells, (t_start, t_end) = self.row(op.table, template_no)
        if op.after_row < count:
            below, _ = self.row(op.table, op.after_row + 1)
            if any(c.vmerge == "continue" for c in below):
                raise DocControlError(
                    ErrorCode.UNSUPPORTED_CONTENT,
                    f"a vertical merge spans rows {op.after_row} and {op.after_row + 1} of "
                    f"{where}: a row cannot be inserted between them",
                    table=op.table,
                    after_row=op.after_row,
                )
        if len(op.cells) != len(cells):
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                f"`cells` has {len(op.cells)} texts but row {template_no} of {where} has "
                f"{len(cells)} cells",
                table=op.table,
                expected=len(cells),
            )
        near = self._near(cells, f"row {template_no} of {where}")
        template = self.spans.parsed_range(t_start, t_end, near)
        tcs = template.findall(W + "tc")
        if len(tcs) != len(cells):
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"row {template_no} of {where} has cells inside other elements",
            )
        tr = ET.Element(W + "tr")
        ex = template.find(W + "tblPrEx")
        if ex is not None:
            tr.append(copy.deepcopy(ex))
        trpr = ET.Element(W + "trPr")
        src = template.find(W + "trPr")
        for child in src if src is not None else []:
            if child.tag not in _NOT_COPIED_ROW:
                trpr.append(copy.deepcopy(child))
        trpr.append(self.ctx.revision("ins"))
        tr.append(trpr)
        for tc, text in zip(tcs, op.cells, strict=True):
            cell = ET.SubElement(tr, W + "tc")
            tcpr = tc.find(W + "tcPr")
            if tcpr is not None:
                kept = ET.Element(W + "tcPr")
                kept.extend(copy.deepcopy(c) for c in tcpr if c.tag not in _NOT_COPIED_CELL)
                if len(kept):
                    cell.append(kept)
            para, props = new_paragraph(tc.find(W + "p"), self.ctx, inserted_mark=True)
            content = inserted_content(self.ctx, text=text, run_props=props)
            if content is not None:
                para.append(content)
            cell.append(para)
        data = self.spans.serialize(tr, near)
        at = t_end if op.after_row else t_start
        self._cut(at, at, data)
        self.changes.append(
            {"part": DOCUMENT_PART, "kind": "span", "before": "", "after": to_text(data)}
        )
        self.new_paragraphs.append((at, list(op.cells)))
        self.previews.append(
            {
                "op": number,
                "at": f"table {op.table} after row {op.after_row}",
                "before": "",
                "after": " | ".join(op.cells),
            }
        )

    # -- delete ------------------------------------------------------------------------

    def delete(self, number: int, op: DeleteRow) -> None:
        where = f"row {op.row} of table {op.table}"
        cells, (start, end) = self.row(op.table, op.row)
        if (op.table, op.row) in self.deleted:
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT, f"{where} is deleted twice in the batch", table=op.table
            )
        self.deleted.add((op.table, op.row))
        if any(c.vmerge is not None for c in cells):
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"{where} crosses a vertical merge (w:vMerge): deleting it would tear the merged "
                "cell; delete the cell text instead",
                table=op.table,
                row=op.row,
            )
        old = self.data[start:end]
        if _TBL_OPEN.search(old):
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT, f"{where} holds a nested table", table=op.table
            )
        near = self._near(cells, where)
        edits: list[tuple[int, int, bytes]] = []
        texts: list[str] = []
        for cell in cells:
            for n in cell.paragraphs:
                para = self.doc.index.paragraph(n)
                span = self.spans.paragraphs[para.span_index]
                elem = self.spans.parse_paragraph(para.span_index)
                editor = ParagraphEditor(elem, para, self.ctx)
                editor.delete_content()
                editor.mark("del")
                edits.append((span.start, span.end, self.spans.serialize(elem, para.span_index)))
                texts.append(para.text)
                self.emptied.add(n)
        edits.append(self._row_mark(start, end, near, where))
        new = bytearray()
        pos = start
        for s, e, data in sorted(edits):
            new += self.data[pos:s] + data
            pos = e
        new += self.data[pos:end]
        self._cut(start, end, bytes(new))
        self.changes.append(
            {
                "part": DOCUMENT_PART,
                "kind": "span",
                "before": to_text(old),
                "after": to_text(bytes(new)),
            }
        )
        self.previews.append(
            {"op": number, "at": f"table {op.table} row {op.row}", "before": " | ".join(texts),
             "after": "(deleted)"}
        )  # fmt: skip

    def _row_mark(self, start: int, end: int, near: int, where: str) -> tuple[int, int, bytes]:
        """The edit that gives the row at ``start:end`` a ``w:del`` in its ``w:trPr``."""
        m = _TR_OPEN.match(self.data, start)
        assert m is not None
        prefix = m[1]
        head = self.data.find(b">", m.end()) + 1
        if self.data[head - 2 : head - 1] == b"/":
            raise DocControlError(ErrorCode.UNSUPPORTED_CONTENT, f"{where} has no cells")
        ex = prefix + b"tblPrEx"
        pr = prefix + b"trPr"
        found = re.compile(
            rb"(?:\s*<" + ex + rb"(?:\s[^>]*)?(?:/>|>.*?</" + ex + rb">))?\s*"
            rb"(<" + pr + rb"(?:\s[^>]*?)?(?:(/)>|>(.*?)</" + pr + rb">))",
            re.DOTALL,
        ).match(self.data, head, end)
        mark = self.spans.serialize(self.ctx.revision("del"), near)
        if found is None:
            at = head
            ex_block = re.compile(
                rb"\s*<" + ex + rb"(?:\s[^>]*)?(?:/>|>.*?</" + ex + rb">)", re.DOTALL
            ).match(self.data, head, end)
            if ex_block is not None:
                at = ex_block.end()
            return at, at, b"<" + pr + b">" + mark + b"</" + pr + b">"
        if found[2] is not None:  # <w:trPr/>: open it
            tag = self.data[found.start(1) : found.end(1) - 2].rstrip()
            return found.start(1), found.end(1), tag + b">" + mark + b"</" + pr + b">"
        body = found[3]
        if re.search(rb"<(?:[\w.-]+:)?del[\s/>]", body.split(b"<" + prefix + b"trPrChange")[0]):
            raise DocControlError(ErrorCode.UNSUPPORTED_CONTENT, f"{where} is already deleted")
        inner = found.start(3)
        cut = body.find(b"<" + prefix + b"trPrChange")
        at = inner + (cut if cut >= 0 else len(body))
        return at, at, mark


# -- applying -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RowsResult:
    applied: int
    dry_run: bool
    preview: list[dict[str, Any]]
    address_map: dict[str, str]
    footprint: list[str]
    revision_ids: list[int]
    author: str
    date: str
    before_sha256: str
    after_sha256: str | None = None
    backup: Path | None = None
    warnings: tuple[str, ...] = ()
    op_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "applied": self.applied,
            "dry_run": self.dry_run,
            "preview": self.preview,
            "address_map": self.address_map,
            "footprint": {DOCUMENT_PART: self.footprint},
            "revision_ids": self.revision_ids,
            "author": self.author,
            "date": self.date,
        }
        if self.warnings:
            out["warnings"] = list(self.warnings)
        if not self.dry_run:
            out |= {
                "sha256": self.after_sha256,
                "backup": str(self.backup) if self.backup else None,
                "op_id": self.op_id,
            }
        return out


def apply_row_ops(
    doc_path: Path,
    ops: list[RowOp],
    *,
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    stage: Callable[[str], None] | None = None,
) -> RowsResult:
    """Apply row operations to ``doc_path`` as tracked changes in one verified write.

    Every number refers to the document as read. Raises ``ANCHOR_NOT_FOUND`` (no such
    table or row), ``UNSUPPORTED_CONTENT`` (a vertical merge, a nested table),
    ``SCHEMA_INVALID`` (the wrong number of cells) and ``BATCH_CONFLICT`` before touching the
    file; the result is checked first (every paragraph has the text it should), or
    ``INTERNAL`` is raised and nothing is written.
    """
    if not ops:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "no row operations given")
    check_name(author, "author")
    if tag is not None:
        check_name(tag, "tag")
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    doc = Document(str(doc_path))
    pkg = doc.pkg
    ctx = RevisionContext(used_ids(pkg), format_author(author, tag), format_date(now))
    plan = _Plan(doc, ctx)
    for number, op in enumerate(ops):
        if isinstance(op, InsertRow):
            plan.insert(number, op)
        else:
            plan.delete(number, op)

    data = plan.data
    cuts = sorted(plan.cuts, key=lambda c: (c.start, c.end, c.seq))
    out = bytearray()
    pos = 0
    for cut in cuts:
        out += data[pos : cut.start] + cut.data
        pos = cut.end
    out += data[pos:]
    new_data = bytes(out)
    address_map = _check(doc, plan, new_data)
    footprint = Footprint(spans=frozenset((c.start, c.end) for c in cuts))
    result = RowsResult(
        applied=len(ops),
        dry_run=dry_run,
        preview=plan.previews,
        address_map=address_map,
        footprint=[p["at"] for p in plan.previews],
        revision_ids=[int(r) for r in ctx.revision_ids],
        author=ctx.author,
        date=ctx.date,
        before_sha256=pkg.sha256,
    )
    if dry_run:
        return result
    written = write_verified(
        doc_path, {DOCUMENT_PART: new_data}, footprint, pkg.sha256, now=now, env=env, stage=stage
    )
    warnings: list[str] = []
    op_id: str | None = None
    try:
        op_id = record(
            doc_path,
            "batch",
            before_sha256=written.before_sha256,
            after_sha256=written.after_sha256,
            backup=written.backup,
            changes=plan.changes,
            request=[_request(op) for op in ops],
            author=result.author,
            tag=tag,
            footprint={DOCUMENT_PART: result.footprint},
            now=now,
            env=env,
        )
    except (DocControlError, OSError) as exc:
        warnings.append(f"HISTORY_NOT_RECORDED: the write succeeded but could not be logged: {exc}")
    return RowsResult(
        **{
            **result.__dict__,
            "before_sha256": written.before_sha256,
            "after_sha256": written.after_sha256,
            "backup": written.backup,
            "warnings": tuple(warnings),
            "op_id": op_id,
        }
    )


def _check(doc: Document, plan: _Plan, new_data: bytes) -> dict[str, str]:
    """Fail with ``INTERNAL`` unless every paragraph has the text it should; returns the
    old-to-new address of each paragraph whose number moved."""
    old = doc.index
    new = build_index(new_data)
    starts = [old.spans.paragraphs[p.span_index].start for p in old.paragraphs]
    added = sorted(plan.new_paragraphs, key=lambda item: item[0])  # stable: the ops' order
    expected: list[str] = []
    moved: dict[int, int] = {}  # old paragraph number -> new number
    queue = list(added)
    for para, start in zip(old.paragraphs, starts, strict=True):
        while queue and queue[0][0] <= start:
            expected.extend(queue.pop(0)[1])
        moved[para.number] = len(expected) + 1
        expected.append("" if para.number in plan.emptied else para.text)
    for _, texts in queue:
        expected.extend(texts)
    got = [p.text for p in new.paragraphs]
    if got != expected:
        bad = next((n for n, (a, b) in enumerate(zip(got, expected, strict=False), 1) if a != b), 0)
        raise DocControlError(
            ErrorCode.INTERNAL,
            f"self-check: paragraph {bad} of the edited document has unexpected text",
            paragraph=bad,
        )
    return {
        str(para.address): str(new.paragraph(moved[para.number]).address)
        for para in old.paragraphs
        if moved[para.number] != para.number
        or str(new.paragraph(moved[para.number]).address) != str(para.address)
    }


# -- registration -------------------------------------------------------------------------


_OUTPUT = outputs.result(
    ("applied", "dry_run", "preview", "address_map", "footprint", "revision_ids"),
    **{k: v for k, v in outputs.BATCH_FIELDS.items() if k not in ("comment_ids", "tracked")},
)


def _register(kind: str) -> None:
    fields = {k: v for k, v in ROW_SCHEMAS[kind]["properties"].items() if k != "op"}
    schema = {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            **fields,
            "expect": {
                "type": "object",
                "properties": {"sha256": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
            "author": {"type": "string", "minLength": 1, "description": "Revision author."},
            "tag": {"type": "string", "minLength": 1, "description": "Appended to the author."},
        },
        "required": ["doc", *ROW_SCHEMAS[kind]["required"][1:]],
        "additionalProperties": False,
    }

    def handler(request: dict[str, Any]) -> Reply:
        op = row_op_from_dict({"op": kind, **{k: request[k] for k in fields if k in request}})
        result = apply_row_ops(
            Path(request["doc"]),
            [op],
            expect_sha256=request.get("expect", {}).get("sha256"),
            author=request.get("author", DEFAULT_AUTHOR),
            tag=request.get("tag"),
            dry_run=request.get("dry_run", False),
        )
        return Reply(result.to_dict(), doc=Document(request["doc"]).envelope_doc())

    operation(kind, "write", _SUMMARY[kind], schema, output=_OUTPUT)(handler)


for _kind in ROW_SCHEMAS:
    _register(_kind)
