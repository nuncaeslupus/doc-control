"""Tables of a story: their numbering, grid and the paragraphs in each cell.

One numbering serves ``cell`` on paragraph rows and ``table.get``: the ``w:tbl`` elements
of the story part, 1-based in document order, a nested table numbered after the table
that holds it. Not counted: tables inside a text box (``w:txbxContent``, whose paragraphs
are not indexed) and DrawingML tables (``a:tbl``), which are not Word tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from doc_control.ooxml.splice import W_NS

_PREFIX = re.compile(rb'xmlns:([\w.-]+)\s*=\s*["\']' + re.escape(W_NS.encode()) + rb'["\']')
_SPAN = re.compile(rb'<[\w.-]+:gridSpan\b[^>]*?:val\s*=\s*"(\d+)"')
_VMERGE = re.compile(rb'<[\w.-]+:vMerge\b(?:[^>]*?:val\s*=\s*"(\w+)")?')
_GRID_BEFORE = re.compile(rb'<[\w.-]+:gridBefore\b[^>]*?:val\s*=\s*"(\d+)"')


@dataclass
class Cell:
    col: int  # first grid column, 1-based
    span: int  # grid columns covered (``w:gridSpan``)
    vmerge: str | None  # "restart", "continue" or None (``w:vMerge``)
    ordinal: int  # position among the row's cells, 1-based
    paragraphs: list[int] = field(default_factory=list)  # paragraph numbers, document order


@dataclass
class Tables:
    grids: list[list[list[Cell]]]  # per table, per row, its cells
    # Per paragraph, ``(table, row, cell ordinal)`` of its innermost cell; None outside tables.
    positions: tuple[tuple[int, int, int] | None, ...]


@dataclass
class _Frame:
    number: int
    rows: list[list[Cell]]
    col: int = 0
    cell: Cell | None = None


def _properties(data: bytes, pos: int, prefix: bytes, name: bytes) -> bytes:
    """Content of the ``name`` property block that opens the element at ``pos``; empty
    when there is none. A tracked change's old properties (``…Change``) are left out."""
    start = data.find(b">", pos) + 1
    tag = prefix + b":" + name
    m = re.compile(rb"\s*<" + tag + rb"(?:\s[^>]*)?(?:/>|>(.*?)</" + tag + b">)", re.DOTALL).match(
        data, start
    )
    if m is None or m[1] is None:
        return b""
    return bytes(m[1].split(b"<" + tag + b"Change", 1)[0])


def build_tables(data: bytes, paragraph_starts: list[int]) -> Tables:
    """The tables of ``data`` (a story part) and the cell of each paragraph, whose start
    offsets in ``data`` are ``paragraph_starts`` in document order."""
    prefixes = {m[1] for m in _PREFIX.finditer(data)}
    if not prefixes:
        return Tables([], (None,) * len(paragraph_starts))
    alt = b"|".join(re.escape(p) for p in sorted(prefixes))
    tags = re.compile(rb"<(/?)(" + alt + rb"):(tbl|tr|tc|txbxContent)(?=[\s/>])")
    grids: list[list[list[Cell]]] = []
    positions: list[tuple[int, int, int] | None] = []
    stack: list[_Frame] = []
    box = 0
    matches = iter(tags.finditer(data))
    pending = next(matches, None)
    for start in paragraph_starts:
        while pending is not None and pending.start() < start:
            m, pending = pending, next(matches, None)
            closing, prefix, name = m[1] == b"/", m[2], m[3]
            empty = (
                not closing
                and data[data.find(b">", m.end()) - 1 : data.find(b">", m.end())] == b"/"
            )
            if name == b"txbxContent":
                box = max(box + (-1 if closing else 0 if empty else 1), 0)
            elif box:
                continue
            elif name == b"tbl":
                if closing:
                    if stack:
                        stack.pop()
                else:
                    frame = _Frame(len(grids) + 1, [])
                    grids.append(frame.rows)
                    if not empty:
                        stack.append(frame)
            elif not stack or closing:
                if name == b"tc" and stack:
                    stack[-1].cell = None
            elif name == b"tr":
                frame = stack[-1]
                before = _GRID_BEFORE.search(_properties(data, m.start(), prefix, b"trPr"))
                frame.col = int(before[1]) if before else 0
                frame.rows.append([])
            else:
                frame = stack[-1]
                if not frame.rows:  # a cell outside any row: Word repairs it, so do we
                    frame.rows.append([])
                props = _properties(data, m.start(), prefix, b"tcPr")
                span = _SPAN.search(props)
                merge = _VMERGE.search(props)
                cell = Cell(
                    frame.col + 1,
                    max(int(span[1]), 1) if span else 1,
                    ("restart" if merge[1] == b"restart" else "continue") if merge else None,
                    len(frame.rows[-1]) + 1,
                )
                frame.col += cell.span
                frame.rows[-1].append(cell)
                frame.cell = None if empty else cell
        top = stack[-1] if stack else None
        if top is not None and top.cell is not None:
            top.cell.paragraphs.append(len(positions) + 1)
            positions.append((top.number, len(top.rows), top.cell.ordinal))
        else:
            positions.append(None)
    return Tables(grids, tuple(positions))
