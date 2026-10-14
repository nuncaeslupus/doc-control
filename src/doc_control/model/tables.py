"""Tables of a story: their numbering, grid and the paragraphs in each cell.

One numbering serves ``cell`` on paragraph rows and ``table.get``: the ``w:tbl`` elements
of the story part, 1-based in document order, a nested table numbered after the table
that holds it. Not counted: tables inside a text box (``w:txbxContent``, whose paragraphs
are not indexed) and DrawingML tables (``a:tbl``), which are not Word tables. An empty
``w:tbl`` (no rows, which Word repairs) is still a ``w:tbl``: it takes its number, so the
numbers of the tables after it do not depend on a repair, and ``table.get`` shows it with
no rows.

Numbers belong to one story. The ``table`` field of a request names a table of the main
story (the body); a table of a note, header or footer is reached by the ``at`` of a
paragraph in it, and is numbered within that story.

Element names are matched under every prefix bound to the WordprocessingML namespace, and
without one when that namespace is the default (``<tbl xmlns="…/main">``). Attribute values
may be quoted either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from doc_control.ooxml.splice import W_NS

_PREFIX = re.compile(rb'xmlns(?::([\w.-]+))?\s*=\s*["\']' + re.escape(W_NS.encode()) + rb'["\']')
_NAME = rb"(?:[\w.-]+:)?"  # an element name's optional prefix
_VAL = rb'(?:[\w.-]+:)?val\s*=\s*(?:"([^"]*)"|\'([^\']*)\')'  # w:val, quoted either way
_SPAN = re.compile(rb"<" + _NAME + rb"gridSpan\b[^>]*?\s" + _VAL)
_VMERGE = re.compile(rb"<" + _NAME + rb"vMerge\b(?:[^>]*?\s" + _VAL + rb")?")
_GRID_BEFORE = re.compile(rb"<" + _NAME + rb"gridBefore\b[^>]*?\s" + _VAL)


def _val(m: re.Match[bytes]) -> bytes | None:
    """The value of the ``w:val`` a property regex matched (None: absent)."""
    return m[1] if m[1] is not None else m[2]


def _number(m: re.Match[bytes] | None) -> int | None:
    value = _val(m) if m else None
    return int(value) if value is not None and value.isdigit() else None


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
    # Per table, per row, the byte span ``(start, end)`` of its ``w:tr`` in the part; ``end``
    # is -1 for a row that is never closed (a part that is cut short).
    row_spans: list[list[tuple[int, int]]] = field(default_factory=list)


@dataclass
class _Frame:
    number: int
    rows: list[list[Cell]]
    spans: list[tuple[int, int]]
    col: int = 0
    cell: Cell | None = None


def _properties(data: bytes, pos: int, prefix: bytes, name: bytes) -> bytes:
    """Content of the ``name`` property block that opens the element at ``pos`` (``prefix``
    includes its colon); empty when there is none. A row's block may follow its
    ``w:tblPrEx``. A tracked change's old properties (``…Change``) are left out."""
    start = data.find(b">", pos) + 1
    tag = prefix + name
    skip = b""
    if name == b"trPr":
        ex = prefix + b"tblPrEx"
        skip = rb"(?:\s*<" + ex + rb"(?:\s[^>]*)?(?:/>|>.*?</" + ex + b">))?"
    m = re.compile(
        skip + rb"\s*<" + tag + rb"(?:\s[^>]*)?(?:/>|>(.*?)</" + tag + b">)", re.DOTALL
    ).match(data, start)
    if m is None or m[1] is None:
        return b""
    return bytes(m[1].split(b"<" + tag + b"Change", 1)[0])


def build_tables(data: bytes, paragraph_starts: list[int]) -> Tables:
    """The tables of ``data`` (a story part) and the cell of each paragraph, whose start
    offsets in ``data`` are ``paragraph_starts`` in document order."""
    prefixes = {m[1] or b"" for m in _PREFIX.finditer(data)}
    if not prefixes:
        return Tables([], (None,) * len(paragraph_starts))
    alt = b"|".join(re.escape(p + b":" if p else p) for p in sorted(prefixes, reverse=True))
    tags = re.compile(rb"<(/?)(" + alt + rb")(tbl|tr|tc|txbxContent)(?=[\s/>])")
    grids: list[list[list[Cell]]] = []
    row_spans: list[list[tuple[int, int]]] = []
    positions: list[tuple[int, int, int] | None] = []
    stack: list[_Frame] = []
    box = 0
    matches = iter(tags.finditer(data))
    pending = next(matches, None)

    def consume(limit: int) -> None:
        """Take every tag that starts before ``limit``."""
        nonlocal pending, box
        while pending is not None and pending.start() < limit:
            m, pending = pending, next(matches, None)
            closing, prefix, name = m[1] == b"/", m[2], m[3]
            close = data.find(b">", m.end())
            empty = not closing and data[close - 1 : close] == b"/"
            if name == b"txbxContent":
                box = max(box + (-1 if closing else 0 if empty else 1), 0)
            elif box:
                continue
            elif name == b"tbl":
                if closing:
                    if stack:
                        stack.pop()
                else:
                    frame = _Frame(len(grids) + 1, [], [])
                    grids.append(frame.rows)
                    row_spans.append(frame.spans)
                    if not empty:
                        stack.append(frame)
            elif not stack:
                continue
            elif name == b"tr":
                frame = stack[-1]
                if closing:
                    if frame.spans and frame.spans[-1][1] == -1:
                        frame.spans[-1] = (frame.spans[-1][0], close + 1)
                    continue
                before = _number(_GRID_BEFORE.search(_properties(data, m.start(), prefix, b"trPr")))
                frame.col = before or 0
                frame.rows.append([])
                frame.spans.append((m.start(), close + 1 if empty else -1))
            elif closing:
                if name == b"tc":
                    stack[-1].cell = None
            else:
                frame = stack[-1]
                if not frame.rows:  # a cell outside any row: Word repairs it, so do we
                    frame.rows.append([])
                    frame.spans.append((m.start(), -1))
                props = _properties(data, m.start(), prefix, b"tcPr")
                span = _number(_SPAN.search(props))
                merge = _VMERGE.search(props)
                cell = Cell(
                    frame.col + 1,
                    max(span, 1) if span else 1,
                    ("restart" if _val(merge) == b"restart" else "continue") if merge else None,
                    len(frame.rows[-1]) + 1,
                )
                frame.col += cell.span
                frame.rows[-1].append(cell)
                frame.cell = None if empty else cell

    for start in paragraph_starts:
        consume(start)
        top = stack[-1] if stack else None
        if top is not None and top.cell is not None:
            top.cell.paragraphs.append(len(positions) + 1)
            positions.append((top.number, len(top.rows), top.cell.ordinal))
        else:
            positions.append(None)
    consume(len(data) + 1)  # the rest: rows and tables that close after the last paragraph
    return Tables(grids, tuple(positions), row_spans)
