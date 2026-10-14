"""Table row revisions (``w:trPr/w:ins``, ``w:trPr/w:del``) for ``revision.accept``/``reject``.

A row is not a paragraph, so the row layer works on bytes: :func:`scan_rows` finds the
marked rows of the body, :func:`plan_rows` turns the decisions into cuts and marker drops
(refusing to leave a table without rows), and :func:`apply_rows` makes them on the
document bytes after the paragraph decisions were applied.

Accepting a deletion or rejecting an insertion removes the whole ``w:tr``; the paragraphs in
it go with it and are not resolved on their own. Accepting an insertion or rejecting a
deletion drops the marker and leaves the row's own text revisions as they are.
"""

from __future__ import annotations

import bisect
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex
from doc_control.model.parts import W
from doc_control.model.tables import build_tables
from doc_control.ooxml.revisions import AUTHOR, ID

__all__ = ["RowPlan", "RowRevision", "apply_rows", "plan_rows", "row_spans", "scan_rows"]

_KINDS = {W + "ins": "row_insert", W + "del": "row_delete"}
_TR_OPEN = re.compile(rb"<(?:[\w.-]+:)?tr[\s>/]")
_TRPR_OPEN = re.compile(rb"<((?:[\w.-]+:)?)trPr(?=[\s>/])[^>]*?(/?)>")
_TC_OPEN = re.compile(rb"<(?:[\w.-]+:)?tc[\s>/]")
_REMOVES = frozenset({("row_insert", "reject"), ("row_delete", "accept")})


@dataclass(eq=False)
class RowRevision:
    """One ``w:ins``/``w:del`` marker of a row; ``kind`` is the ``revisions.list`` type."""

    id: str
    kind: str
    author: str
    ordinal: int  # position of the row among all rows of the part, in document order
    table: int
    start: int  # byte span of the ``w:tr``
    end: int
    first: int  # number of the row's first paragraph (where ``revisions.list`` reports it)
    paragraphs: frozenset[int] = field(default_factory=frozenset)


@dataclass
class RowScan:
    revisions: list[RowRevision]
    rows_per_table: dict[int, int]


def _paragraph_bounds(index: DocumentIndex) -> tuple[list[int], list[int]]:
    spans = index.spans.paragraphs
    return (
        [spans[p.span_index].start for p in index.paragraphs],
        [spans[p.span_index].end for p in index.paragraphs],
    )


def row_spans(index: DocumentIndex) -> list[tuple[int, int, int]]:
    """``(start, end, table)`` of every row of the part, in document order."""
    starts, _ = _paragraph_bounds(index)
    tables = build_tables(index.spans.data, starts)
    return sorted((s, e, t) for t, spans in enumerate(tables.row_spans, 1) for s, e in spans)


def _trs(root: ET.Element) -> list[ET.Element]:
    out: list[ET.Element] = []

    def walk(elem: ET.Element) -> None:
        for child in elem:
            if child.tag == W + "p" or not isinstance(child.tag, str):
                continue
            if child.tag == W + "tr":
                out.append(child)
            walk(child)

    walk(root)
    return out


def scan_rows(index: DocumentIndex) -> RowScan:
    """The row revisions of the main story's body (none if the rows cannot be paired up)."""
    data = index.spans.data
    rows = row_spans(index)
    if not rows or any(end < 0 for _, end, _ in rows):
        return RowScan([], {})
    try:
        trs = _trs(ET.fromstring(data))
    except ET.ParseError:
        return RowScan([], {})
    if len(trs) != len(rows):
        return RowScan([], {})
    starts, _ = _paragraph_bounds(index)
    per_table: dict[int, int] = {}
    for _, _, table in rows:
        per_table[table] = per_table.get(table, 0) + 1
    found: list[RowRevision] = []
    for ordinal, (tr, (start, end, table)) in enumerate(zip(trs, rows, strict=True)):
        marks = [m for m in tr.iterfind(f"{W}trPr/*") if m.tag in _KINDS]
        if not marks:
            continue
        low = bisect.bisect_left(starts, start)
        if low >= len(starts):
            continue  # no paragraph to report it at
        inside = frozenset(range(low + 1, bisect.bisect_left(starts, end) + 1))
        found += [
            RowRevision(
                m.get(ID) or "",
                _KINDS[m.tag],
                m.get(AUTHOR) or "",
                ordinal,
                table,
                start,
                end,
                low + 1,
                inside,
            )
            for m in marks
        ]
    return RowScan(found, per_table)


@dataclass
class RowPlan:
    removed: list[RowRevision] = field(default_factory=list)  # one per row that goes
    dropped: list[RowRevision] = field(default_factory=list)  # markers that go, row stays
    dead: frozenset[int] = frozenset()  # paragraph numbers that go with a removed row

    @property
    def spans(self) -> frozenset[tuple[int, int]]:
        """Byte spans of the original part that change."""
        return frozenset((r.start, r.end) for r in [*self.removed, *self.dropped])

    def __bool__(self) -> bool:
        return bool(self.removed or self.dropped)


def plan_rows(scan: RowScan, decisions: dict[int, str]) -> RowPlan:
    """Cuts and marker drops for ``decisions`` (``index in scan.revisions`` -> action).

    Raises ``UNSUPPORTED_CONTENT`` when a table would be left without rows."""
    revs = scan.revisions
    cut: dict[int, RowRevision] = {}  # ordinal -> a marker that removes the row
    for i, action in decisions.items():
        if (revs[i].kind, action) in _REMOVES:
            cut.setdefault(revs[i].ordinal, revs[i])
    outer: list[RowRevision] = []
    last_end = -1
    for r in sorted(cut.values(), key=lambda r: (r.start, -r.end)):
        if r.start >= last_end:
            outer.append(r)
            last_end = r.end
    gone: dict[int, int] = {}
    for r in outer:
        gone[r.table] = gone.get(r.table, 0) + 1
    for table, n in sorted(gone.items()):
        if n >= scan.rows_per_table[table]:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"resolving these row revisions would leave table {table} without rows; "
                "keep one of its rows or delete the table another way",
                table=table,
            )

    def inside_removed(r: RowRevision) -> bool:
        return any(o.start <= r.start and r.end <= o.end and o is not r for o in outer)

    dropped = [
        revs[i]
        for i in sorted(decisions)
        if revs[i].ordinal not in cut and not inside_removed(revs[i])
    ]
    dead = frozenset(n for r in outer for n in r.paragraphs)
    return RowPlan(outer, dropped, dead)


def apply_rows(index_of_data: DocumentIndex, plan: RowPlan) -> bytes:
    """``index_of_data``'s part with the plan made. Rows are found again by ordinal, so the
    part may have had paragraphs rewritten (not merged across rows) since the scan."""
    data = index_of_data.spans.data
    rows = row_spans(index_of_data)
    edits: list[tuple[int, int, bytes]] = []
    for r in plan.removed:
        start, end, _ = rows[r.ordinal]
        edits.append((start, end, b""))
    drop_ids: dict[int, list[RowRevision]] = {}
    for r in plan.dropped:
        drop_ids.setdefault(r.ordinal, []).append(r)
    for ordinal, marks in drop_ids.items():
        start, end, _ = rows[ordinal]
        edits.append((start, end, _drop_markers(data[start:end], marks)))
    out = bytearray()
    pos = 0
    for start, end, new in sorted(edits):
        out += data[pos:start] + new
        pos = end
    out += data[pos:]
    return bytes(out)


def _drop_markers(row: bytes, marks: Sequence[RowRevision]) -> bytes:
    """``row`` without the ``w:ins``/``w:del`` markers ``marks`` in its ``w:trPr``."""
    tr = _TR_OPEN.match(row)
    pr = _TRPR_OPEN.search(row)
    cell = _TC_OPEN.search(row)
    if tr is None or pr is None or (cell is not None and cell.start() < pr.start()):
        raise DocControlError(ErrorCode.INTERNAL, "row revision: the row has no w:trPr")
    prefix = pr.group(1)
    if pr.group(2):
        raise DocControlError(ErrorCode.INTERNAL, "row revision: an empty w:trPr holds a marker")
    close = row.find(b"</" + prefix + b"trPr>", pr.end())
    change = row.find(b"<" + prefix + b"trPrChange", pr.end(), close)
    stop = close if change < 0 else change
    body = row[pr.end() : stop]
    for m in marks:
        name = b"ins" if m.kind == "row_insert" else b"del"
        pattern = re.compile(
            rb"<(?:[\w.-]+:)?" + name + rb"(?=[\s/>])[^>]*?"
            rb"(?:[\w.-]+:)?id\s*=\s*[\"']" + re.escape(m.id.encode()) + rb"[\"'][^>]*?"
            rb"(?:/>|>\s*</(?:[\w.-]+:)?" + name + rb">)"
        )
        body, n = pattern.subn(b"", body, count=1)
        if n != 1:
            raise DocControlError(ErrorCode.INTERNAL, f"row revision {m.id}: marker not found")
    rest = row[stop:close] if change >= 0 else b""
    if not body.strip() and not rest:
        # nothing else in the properties: drop the whole block
        return row[: pr.start()] + row[close + len(b"</" + prefix + b"trPr>") :]
    return row[: pr.end()] + body + row[stop:]
