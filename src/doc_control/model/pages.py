"""Estimated page of every paragraph. Always an estimate: only Word's layout engine knows.

Three sources, strongest first:

* ``rendered``: the document holds ``w:lastRenderedPageBreak`` marks, written by Word each
  time it laid the document out. They are taken as the page starts (a stale file can
  disagree with its text). An explicit break next to a mark is not counted twice.
* ``break``: an explicit page start: ``w:br w:type="page"``, ``pageBreakBefore`` (direct or
  from the style chain) or a section break that starts a new page (``nextPage``,
  ``oddPage``, ``evenPage``; ``continuous`` does not).
* ``model``: where there is no mark, pages are filled by a word-count model. A paragraph
  of *w* words takes ``ceil(w / words_per_line)`` lines (at least one), where a line holds
  ``width / (font_pt * CHAR_EM) / WORD_CHARS`` words, and each line is ``font_pt *
  LINE_FACTOR`` points high (scaled or fixed by ``w:spacing``), plus space before and
  after. A page holds its height minus the top and bottom margins; the width is the page
  width minus the left (with gutter) and right margins and the paragraph's indents. Font
  size, spacing and indents come from the paragraph, its style chain and
  ``w:docDefaults``; the page from the paragraph's section (``w:sectPr``). Not modelled:
  run-level sizes, tables as columns, floating objects, widow control, keep-with-next,
  headers and footers.

A paragraph is placed on the page where it starts; ``end_page`` differs when it runs over.
"""

from __future__ import annotations

import math
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
    page: int  # page the paragraph starts on
    end_page: int
    source: str  # rendered | break | model


@dataclass(frozen=True)
class PageEstimate:
    pages: int
    basis: str  # rendered | model
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


def _height(f: _Facts, geo: _Geometry) -> tuple[float, float]:
    """(height of the paragraph, height of one of its lines) in points."""
    indent = f.ppr.get("indent", {})
    width = max(geo.width_pt - (indent.get("left", 0) + indent.get("right", 0)) / 20, 20.0)
    lines = max(1, math.ceil(f.words / _words_per_line(width, f.font_pt)))
    line_h = _line_height(f.font_pt, f.ppr)
    spacing = f.ppr.get("spacing", {})
    gap = (spacing.get("before", 0) + spacing.get("after", 0)) / 20
    return lines * line_h + gap, line_h


class _Sim:
    def __init__(self) -> None:
        self.page = 1
        self.used = 0.0
        self.empty = True  # nothing placed on the current page yet
        self.pending = False  # a break was just counted; Word's own mark for it is a duplicate
        self.broke = False  # the current page was started by an explicit break

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
        if ev.kind == "lrpb" and self.pending:
            self.pending = False
        else:
            self.new_page(explicit=ev.kind == "br")

    def place(self, f: _Facts, geo: _Geometry) -> int:
        """Fill pages with the paragraph; returns the page it starts on."""
        height, line_h = _height(f, geo)
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


def estimate_pages(doc: Document) -> PageEstimate:
    facts, normal_pt = _facts(doc)
    rendered = any(e.kind == "lrpb" for f in facts for e in f.events)
    body = parse_part(doc.pkg, DOCUMENT_PART).find(W + "body")
    final = body.find(W + "sectPr") if body is not None else None
    # Section k is closed by the k-th paragraph-level sectPr; the last one by the body's.
    closing = [f.sect for f in facts if f.sect is not None] + [final]

    sim = _Sim()
    rows: list[ParaPage] = []
    section = 0
    opens_section = False
    for number, f in enumerate(facts, start=1):
        sect = closing[section]
        geo = _Geometry.of(sect)
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
        broke = sim.empty and sim.broke  # first on a page that an explicit break started
        start = sim.page if rendered else sim.place(f, geo)
        sim.empty = False
        for ev, is_early in zip(f.events, early, strict=True):
            if is_early:
                continue
            sim.event(ev)
            left = (f.chars - ev.before) / f.chars if f.chars else 0.0
            sim.used = _height(f, geo)[0] * left
            sim.empty = left == 0.0
        if f.chars:
            sim.pending = False
        source = "rendered" if rendered else "break" if broke else "model"
        rows.append(ParaPage(number, start, sim.page, source))
        opens_section = f.sect is not None
        if opens_section:
            section += 1
    first = _Geometry.of(closing[0])
    line_h = normal_pt * LINE_FACTOR
    words_per_page = int(first.height_pt / line_h * _words_per_line(first.width_pt, normal_pt))
    return PageEstimate(sim.page, "rendered" if rendered else "model", words_per_page, tuple(rows))
