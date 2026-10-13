"""Estimated page of every paragraph. Always an estimate: only Word's layout engine knows.

The word-count model always runs; the marks and breaks correct it:

* ``w:lastRenderedPageBreak`` marks, written by Word each time it laid the document out,
  are Word's own page starts and win: up to the last mark only marks and explicit breaks
  start pages (the model never overflows there), so a document Word laid out gets Word's
  page count. Marks too sparse to be a layout (the model, resynced at each mark, needs
  more than ``MARKS_TRUST_RATIO`` times their pages up to the last one) only resync the
  model. Text after the last mark (added since the last layout, or a document without
  marks) is paged by the model alone, restarting from the last mark's page. A mark that
  lands on a page the model (or an explicit break) just started is not counted twice.
* Explicit page starts: ``w:br w:type="page"``, ``pageBreakBefore`` (direct or from the style
  chain) and section breaks that start a new page (``nextPage``, ``oddPage``, ``evenPage``;
  ``continuous`` does not).
* The model: a paragraph of *w* words takes ``ceil(w / words_per_line)`` lines (at least
  one), where a line holds ``width / (font_pt * CHAR_EM) / WORD_CHARS`` words, and each line
  is ``font_pt * LINE_FACTOR`` points high (scaled or fixed by ``w:spacing``), plus space
  before and after. A page holds its height minus the top and bottom margins; the width is
  the page width minus the left (with gutter) and right margins and the paragraph's indents.
  Font size, spacing and indents come from the paragraph, its style chain and
  ``w:docDefaults``; the page from the paragraph's section (``w:sectPr``). A table row is as
  tall as its tallest cell, a cell being the text width divided by the row's cell count (and
  again for a nested table). Not modelled: run-level font sizes, cell margins, merged cells,
  rows split across pages, floating objects, widow control, keep-with-next, headers, footers.

Each paragraph's ``source`` says what fixed its start page: ``break`` when it is the first on
a page an explicit break started, else ``rendered`` when a rendered mark falls in it (at its
start or inside it), else ``model``.

``page`` is the physical page counted from 1. ``page_label`` is the number Word would print:
the physical page shifted by ``w:pgNumType w:start`` of the section (applied at the section's
first page; ignored for a continuous section; the number format is not modelled).

A paragraph is placed on the page where it starts; ``end_page`` differs when it runs over.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from doc_control.model.document import DOCUMENT_PART, Document
from doc_control.model.format import para_format, run_format
from doc_control.model.parts import OFFICE_REL, W, parse_part, related_part

DEFAULT_PAGE_TWIPS = (12240, 15840)  # US Letter, Word's default
DEFAULT_MARGIN_TWIPS = 1440
DEFAULT_FONT_PT = 10.0  # the OOXML default when nothing sets a size
CHAR_EM = 0.5  # average character width as a fraction of the font size
WORD_CHARS = 6  # characters per word, space included
LINE_FACTOR = 1.15  # single line height as a multiple of the font size
MARKS_TRUST_RATIO = 2.0  # marks win when the model needs at most this many times their pages

NOTES = (
    "Run-level font sizes are ignored: only paragraph, style and document defaults count.",
    "A table row is as tall as its tallest cell; cell width is the text width divided by the "
    "row's cells (cell margins, merged cells and rows split across pages are ignored).",
    "page is the physical page from 1; page_label follows w:pgNumType w:start per section "
    "(numeric only).",
)

CONSTANTS: dict[str, Any] = {
    "char_em": CHAR_EM,
    "word_chars": WORD_CHARS,
    "line_factor": LINE_FACTOR,
    "default_font_pt": DEFAULT_FONT_PT,
}

_SKIP = frozenset(W + n for n in ("pPr", "del", "moveFrom", "txbxContent", "drawing", "pict"))


@dataclass(frozen=True)
class ParaPage:
    number: int  # 1-based paragraph number
    page: int  # physical page the paragraph starts on
    end_page: int
    source: str  # rendered | break | model
    label: int  # printed number of ``page``
    end_label: int


@dataclass(frozen=True)
class PageEstimate:
    pages: int
    basis: str  # "rendered+model" when the file holds rendered marks, else "model"
    marks: int  # rendered page marks in the document
    words_per_page: int  # the model's figure for the first section, in the default style
    rows: tuple[ParaPage, ...]


@dataclass(frozen=True)
class _Event:
    kind: str  # "br" (explicit page break) | "lrpb" (lastRenderedPageBreak)
    before: int  # visible characters before it in the paragraph


@dataclass(frozen=True)
class _Facts:
    words: int
    chars: int
    events: tuple[_Event, ...]
    ppr: dict[str, Any]
    font_pt: float
    sect: ET.Element | None
    start: int  # byte offset of the paragraph in word/document.xml


@dataclass(frozen=True)
class _Geometry:
    width_pt: float
    height_pt: float

    @classmethod
    def of(cls, sect: ET.Element | None) -> _Geometry:
        size = sect.find(W + "pgSz") if sect is not None else None
        mar = sect.find(W + "pgMar") if sect is not None else None
        side = _twips(mar, "left", DEFAULT_MARGIN_TWIPS) + _twips(mar, "gutter", 0)
        side += _twips(mar, "right", DEFAULT_MARGIN_TWIPS)
        tall = _twips(mar, "top", DEFAULT_MARGIN_TWIPS)
        tall += _twips(mar, "bottom", DEFAULT_MARGIN_TWIPS)
        width = _twips(size, "w", DEFAULT_PAGE_TWIPS[0]) - side
        height = _twips(size, "h", DEFAULT_PAGE_TWIPS[1]) - tall
        return cls(max(width, 400) / 20, max(height, 400) / 20)


def _twips(elem: ET.Element | None, name: str, default: int) -> int:
    raw = elem.get(W + name) if elem is not None else None
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _walk(elem: ET.Element, events: list[_Event], count: list[int]) -> None:
    for child in elem:
        tag = child.tag
        if not isinstance(tag, str) or tag in _SKIP:
            continue
        if tag == W + "t":
            count[0] += len(child.text or "")
        elif tag == W + "lastRenderedPageBreak":
            events.append(_Event("lrpb", count[0]))
        elif tag == W + "br":
            if child.get(W + "type") == "page":
                events.append(_Event("br", count[0]))
        else:
            _walk(child, events, count)


def _doc_defaults(doc: Document) -> tuple[dict[str, Any], dict[str, Any]]:
    name = related_part(doc.pkg, OFFICE_REL + "styles", "word/styles.xml")
    if name is None:
        return {}, {}
    defaults = parse_part(doc.pkg, name).find(W + "docDefaults")
    if defaults is None:
        return {}, {}
    ppr = defaults.find(f"{W}pPrDefault/{W}pPr")
    rpr = defaults.find(f"{W}rPrDefault/{W}rPr")
    return para_format(ppr), run_format(rpr)


def _facts(doc: Document) -> tuple[list[_Facts], float]:
    """Per-paragraph facts, and the font size of the default paragraph style."""
    base_ppr, base_rpr = _doc_defaults(doc)
    normal = doc.styles.default_paragraph
    normal_rpr = {**base_rpr, **(doc.styles.effective(normal)[1] if normal else {})}
    out: list[_Facts] = []
    for para in doc.index.paragraphs:
        elem = doc.index.spans.parse_paragraph(para.span_index)
        ppr_elem = elem.find(W + "pPr")
        style = doc.style_id(para)
        s_ppr, s_rpr = doc.styles.effective(style) if style is not None else ({}, {})
        rpr = {**base_rpr, **s_rpr}
        events: list[_Event] = []
        count = [0]
        _walk(elem, events, count)
        out.append(
            _Facts(
                words=len(para.text.split()),
                chars=count[0],
                events=tuple(events),
                ppr={**base_ppr, **s_ppr, **para_format(ppr_elem)},
                font_pt=float(rpr.get("size_pt", DEFAULT_FONT_PT)),
                sect=ppr_elem.find(W + "sectPr") if ppr_elem is not None else None,
                start=doc.index.spans.paragraphs[para.span_index].start,
            )
        )
    return out, float(normal_rpr.get("size_pt", DEFAULT_FONT_PT))


def _section_type(sect: ET.Element | None) -> str:
    kind = sect.find(W + "type") if sect is not None else None
    return (kind.get(W + "val") if kind is not None else None) or "nextPage"


def _line_height(font_pt: float, ppr: dict[str, Any]) -> float:
    natural = font_pt * LINE_FACTOR
    spacing: dict[str, Any] = ppr.get("spacing", {})
    line: int | None = spacing.get("line")
    if line is None:
        return natural
    rule = spacing.get("line_rule", "auto")
    if rule == "auto":
        return natural * line / 240
    fixed = line / 20
    return fixed if rule == "exact" else max(fixed, natural)


def _words_per_line(width_pt: float, font_pt: float) -> float:
    return max(width_pt / (font_pt * CHAR_EM) / WORD_CHARS, 1.0)


def _height(f: _Facts, width_pt: float) -> tuple[float, float]:
    """(height of the paragraph, height of one of its lines) in a column of ``width_pt``."""
    indent = f.ppr.get("indent", {})
    width = max(width_pt - (indent.get("left", 0) + indent.get("right", 0)) / 20, 20.0)
    lines = max(1, math.ceil(f.words / _words_per_line(width, f.font_pt)))
    line_h = _line_height(f.font_pt, f.ppr)
    spacing = f.ppr.get("spacing", {})
    gap = (spacing.get("before", 0) + spacing.get("after", 0)) / 20
    return lines * line_h + gap, line_h


_TABLE_TAG = re.compile(rb"<(/?)(?:[\w.-]+:)?(tbl|tr|tc)(?=[\s/>])")


def _table_cells(doc: Document, facts: list[_Facts]) -> list[tuple[int, int, float] | None]:
    """Per paragraph, ``(row id, cell id, share of the text width)`` of its table cell, else None.

    Rows and cells are found from the markup around each paragraph (nested tables give
    the cell of the inner table); a cell's share is 1 / cells in its row, times the share
    of the cell that holds its table.
    """
    data = doc.index.spans.data
    tokens = [(m.start(), m.group(1) == b"/", m.group(2)) for m in _TABLE_TAG.finditer(data)]
    kinds: list[bytes] = []
    outer: list[int | None] = []  # cell: the cell around its table
    row_of: list[int | None] = []  # cell: its row
    cells: dict[int, int] = {}
    stack: list[int] = []
    at = 0
    found: list[tuple[int, int] | None] = []
    for f in facts:
        while at < len(tokens) and tokens[at][0] < f.start:
            _, closing, kind = tokens[at]
            at += 1
            if closing:
                if stack:
                    stack.pop()
                continue
            node = len(kinds)
            kinds.append(kind)
            outer.append(None)
            row_of.append(None)
            if kind == b"tc" and stack:
                row_of[node] = stack[-1]
                cells[stack[-1]] = cells.get(stack[-1], 0) + 1
                outer[node] = next((n for n in reversed(stack) if kinds[n] == b"tc"), None)
            stack.append(node)
        cell = next((n for n in reversed(stack) if kinds[n] == b"tc"), None)
        row = row_of[cell] if cell is not None else None
        found.append((row, cell) if row is not None and cell is not None else None)
    share: dict[int, float] = {}
    for node, kind in enumerate(kinds):  # outer cells come before the cells inside them
        row = row_of[node]
        if kind == b"tc" and row is not None:
            parent = outer[node]
            share[node] = (share[parent] if parent is not None else 1.0) / max(cells[row], 1)
    return [(pos[0], pos[1], share[pos[1]]) if pos is not None else None for pos in found]


class _Sim:
    def __init__(self) -> None:
        self.page = 1
        self.used = 0.0
        self.empty = True  # nothing placed on the current page yet
        self.pending = False  # a break was just counted; Word's own mark for it is a duplicate
        self.broke = False  # the current page was started by an explicit break
        self.marks_ahead = 0  # rendered marks still to come: the model may not add pages

    def new_page(self, *, explicit: bool = False) -> None:
        self.page += 1
        self.used = 0.0
        self.empty = True
        self.pending = explicit
        self.broke = explicit

    def parity(self, kind: str) -> None:
        if (kind == "oddPage" and self.page % 2 == 0) or (kind == "evenPage" and self.page % 2):
            self.page += 1

    def event(self, ev: _Event) -> None:
        if ev.kind == "lrpb":
            self.marks_ahead -= 1
        if ev.kind == "lrpb" and (self.pending or self.empty):
            self.pending = False  # this page already started here
        else:
            self.new_page(explicit=ev.kind == "br")

    def place(self, height: float, line_h: float, geo: _Geometry) -> int:
        """Fill pages with a block of ``height``; returns the page it starts on."""
        if self.marks_ahead > 0:  # Word's marks decide where pages start
            self.used = min(self.used + height, geo.height_pt)
            return self.page
        room = geo.height_pt - self.used
        if not self.empty and room < line_h:
            self.new_page()
            room = geo.height_pt
        start = self.page
        if height <= room:
            self.used += height
        else:
            over = height - room
            extra = math.ceil(over / geo.height_pt)
            self.page += extra
            self.used = over - (extra - 1) * geo.height_pt
        return start


def _page_start(sect: ET.Element | None) -> int | None:
    num = sect.find(W + "pgNumType") if sect is not None else None
    raw = num.get(W + "start") if num is not None else None
    return int(raw) if raw is not None and raw.lstrip("-").isdigit() else None


def _row_height(
    group: list[int], facts: list[_Facts], shares: dict[int, tuple[int, float]], width: float
) -> float:
    """Tallest cell of a row: the heights of the paragraphs in each cell added up."""
    per_cell: dict[int, float] = {}
    for i in group:
        cell, share = shares[i]
        per_cell[cell] = per_cell.get(cell, 0.0) + _height(facts[i], width * share)[0]
    return max(per_cell.values())


def estimate_pages(doc: Document) -> PageEstimate:
    """The estimate for ``doc``, computed once per open document."""
    return doc.memo("pages", lambda: _estimate_pages(doc))


def _estimate_pages(doc: Document) -> PageEstimate:
    facts, normal_pt = _facts(doc)
    marks = sum(1 for f in facts for e in f.events if e.kind == "lrpb")
    body = parse_part(doc.pkg, DOCUMENT_PART).find(W + "body")
    final = body.find(W + "sectPr") if body is not None else None
    # Section k is closed by the k-th paragraph-level sectPr; the last one by the body's.
    closing = [f.sect for f in facts if f.sect is not None] + [final]
    where = _table_cells(doc, facts)
    members: dict[int, list[int]] = {}
    shares: dict[int, tuple[int, float]] = {}  # paragraph -> (cell, share)
    for i, pos in enumerate(where):
        if pos is not None:
            members.setdefault(pos[0], []).append(i)
            shares[i] = (pos[1], pos[2])

    def simulate(trust: bool) -> tuple[list[ParaPage], _Sim]:
        placed: dict[int, tuple[int, int]] = {}  # row id -> (start page, end page)
        sim = _Sim()
        sim.marks_ahead = marks if trust else 0
        rows: list[ParaPage] = []
        section = 0
        opens_section = False
        offset = 0  # printed page number minus physical page
        for number, f in enumerate(facts, start=1):
            sect = closing[section]
            geo = _Geometry.of(sect)
            starts_section = number == 1 or opens_section
            if opens_section and _section_type(sect) != "continuous":
                sim.new_page(explicit=True)
                sim.parity(_section_type(sect))
            if f.ppr.get("page_break_before") and not sim.empty:
                sim.new_page(explicit=True)
            # A mark or a break before any text moves this paragraph; one after only the next.
            early = [e.before == 0 and (e.kind == "lrpb" or f.chars > 0) for e in f.events]
            for ev, is_early in zip(f.events, early, strict=True):
                if is_early:
                    sim.event(ev)
            first_label = _page_start(sect)
            new_numbering = number == 1 or (starts_section and _section_type(sect) != "continuous")
            if first_label is not None and new_numbering:
                offset = first_label - sim.page
            broke = sim.empty and sim.broke  # first on a page that an explicit break started
            pos = where[number - 1]
            if pos is not None and pos[0] in placed:
                start = placed[pos[0]][0]
            elif pos is not None:
                height = _row_height(members[pos[0]], facts, shares, geo.width_pt)
                start = sim.place(height, _line_height(f.font_pt, f.ppr), geo)
            else:
                height, line_h = _height(f, geo.width_pt)
                start = sim.place(height, line_h, geo)
            sim.empty = False
            for ev, is_early in zip(f.events, early, strict=True):
                if is_early:
                    continue
                sim.event(ev)
                left = (f.chars - ev.before) / f.chars if f.chars else 0.0
                sim.used = _height(f, geo.width_pt)[0] * left
                sim.empty = left == 0.0
            if f.chars:
                sim.pending = False
            if pos is not None and pos[0] not in placed:
                placed[pos[0]] = (start, sim.page)
            end = placed[pos[0]][1] if pos is not None else sim.page
            has_mark = any(e.kind == "lrpb" for e in f.events)
            source = "break" if broke else "rendered" if has_mark else "model"
            rows.append(ParaPage(number, start, end, source, start + offset, end + offset))
            opens_section = f.sect is not None
            if opens_section:
                section += 1
        return rows, sim

    rows, sim = simulate(False)
    if marks:
        last = max(
            n for n, f in enumerate(facts, start=1) if any(e.kind == "lrpb" for e in f.events)
        )
        # Trust the marks when they are dense enough to be Word's layout, not a stray one:
        # the model alone (resynced at each mark) must not need far more pages up to there.
        if rows[last - 1].end_page <= MARKS_TRUST_RATIO * (marks + 1):
            rows, sim = simulate(True)
    first = _Geometry.of(closing[0])
    line_h = normal_pt * LINE_FACTOR
    words_per_page = int(first.height_pt / line_h * _words_per_line(first.width_pt, normal_pt))
    basis = "rendered+model" if marks else "model"
    return PageEstimate(sim.page, basis, marks, words_per_page, tuple(rows))
