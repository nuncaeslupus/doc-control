"""Layout facts of a paragraph (D-18): what it looks like on the page.

Plain functions of a :class:`~doc_control.model.document.Document` and a paragraph, so the
read operations (T31, :mod:`doc_control.ops.layout`) and structure detection share one
source of truth.

``empty``
    No visible text, or only whitespace (non-breaking spaces included), and no picture
    (``w:drawing``, ``w:pict``, ``w:object``): an image-only paragraph is not empty.
``blank_before``
    How many empty paragraphs sit immediately before this one (0 when none), within
    the same table cell or the same body level: counting stops at a table edge, at an
    image-only paragraph and at a page-break-only paragraph (which is not blank).
``heading_level``
    1-9, from a direct outline level, else the style chain (``basedOn``), else a style
    named ``heading N``; ``None`` for body text. (An outline entry's ``level``.)
``style`` / ``style_name``
    The paragraph's style id (explicit, else the default paragraph style) and its name;
    ``para.get`` and ``outline`` already report them.
``centred``
    Centre alignment, direct (``w:jc``) or inherited through the style chain. Text
    "centred" with tabs or spaces is not detected: that is a recipe (T25).
``page_break_before``
    The paragraph starts a page: ``w:pageBreakBefore`` (direct wins over the style
    chain), a page break (``w:br w:type="page"``) before any visible content, the
    previous paragraph (same cell or body level) ending in a page break or being only
    one, or a section break of type nextPage, oddPage, evenPage or none (not
    continuous or nextColumn) ending the previous paragraph.

Alignment is resolved as direct, then style chain, then ``w:docDefaults``. Not covered:
alignment from a table style or from numbering levels; ``w:lastRenderedPageBreak``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator

from doc_control.model.document import Document
from doc_control.model.format import para_format
from doc_control.model.index import Paragraph
from doc_control.model.parts import W

_NOT_CONTENT = frozenset(W + name for name in ("pPr", "del", "moveFrom", "delText"))
_CONTENT = frozenset(W + name for name in ("t", "tab", "drawing", "pict", "object", "sym"))
_IMAGES = frozenset(W + name for name in ("drawing", "pict", "object"))
_PAGE_SECTIONS = frozenset({"nextPage", "oddPage", "evenPage"})  # not continuous / nextColumn


def _events(elem: ET.Element) -> Iterator[bool]:
    """Visible items of a paragraph in order: True for a page break, False for content."""
    for child in elem:
        tag = child.tag
        if not isinstance(tag, str) or tag in _NOT_CONTENT:
            continue
        if tag == W + "br":
            if child.get(W + "type") == "page":
                yield True
        elif tag in _CONTENT:
            if tag != W + "t" or "".join(child.itertext()).strip():
                yield False
        else:
            yield from _events(child)


def _has_image(elem: ET.Element) -> bool:
    for child in elem:
        tag = child.tag
        if not isinstance(tag, str) or tag in _NOT_CONTENT:
            continue
        if tag in _IMAGES or _has_image(child):
            return True
    return False


def _elem(doc: Document, para: Paragraph) -> ET.Element:
    return doc.paragraph_element(para)


def is_empty(doc: Document, para: Paragraph) -> bool:
    return not para.text.strip() and not _has_image(_elem(doc, para))


def holds_content(doc: Document, para: Paragraph) -> bool:
    """A paragraph without text that is still not blank: it holds an image or only breaks."""
    return _has_image(_elem(doc, para)) or _break_only(doc, para)


def _break_only(doc: Document, para: Paragraph) -> bool:
    items = list(_events(_elem(doc, para)))
    return bool(items) and all(items) and not para.text.strip()


def _previous(doc: Document, para: Paragraph) -> Paragraph | None:
    """The paragraph before this one in the same table cell (or the same body level)."""
    if para.number < 2 or doc.cells[para.number - 2] != doc.cells[para.number - 1]:
        return None
    return doc.index.paragraphs[para.number - 2]


def blank_before(doc: Document, para: Paragraph) -> int:
    count = 0
    prev = _previous(doc, para)
    while prev is not None and is_empty(doc, prev) and not _break_only(doc, prev):
        count += 1
        prev = _previous(doc, prev)
    return count


def centred(doc: Document, para: Paragraph) -> bool:
    alignment = para_format(_elem(doc, para).find(W + "pPr")).get("alignment")
    if alignment is None:
        style = doc.style_id(para)
        alignment = doc.styles.effective(style)[0].get("alignment") if style else None
    if alignment is None:
        alignment = doc.styles.defaults_ppr.get("alignment")
    return alignment == "center"


def _section_break_before(doc: Document, prev: Paragraph) -> bool:
    """Whether the section that starts after ``prev`` (which closes one) starts a page.

    A ``w:sectPr`` describes the section it closes, ``w:type`` included: how that
    section starts. So the section after ``prev`` starts as its own closer says: the next
    paragraph-level ``w:sectPr``, else the body's (``nextPage`` when it has no type or
    there is no closer at all, Word's default).
    """
    if _elem(doc, prev).find(f"{W}pPr/{W}sectPr") is None:
        return False
    return _closer_types(doc)[prev.number] in _PAGE_SECTIONS


def _closer_types(doc: Document) -> list[str]:
    """Per paragraph index (0-based), the start type of that paragraph's section."""

    def build() -> list[str]:
        out = ["nextPage"] * len(doc.index.paragraphs)
        pending: list[int] = []
        for i, para in enumerate(doc.index.paragraphs):
            pending.append(i)
            sect = _elem(doc, para).find(f"{W}pPr/{W}sectPr")
            if sect is not None:
                kind = sect.find(W + "type")
                value = kind.get(W + "val", "nextPage") if kind is not None else "nextPage"
                for k in pending:
                    out[k] = value
                pending = []
        body = _body_sect_type(doc)
        for k in pending:
            out[k] = body
        return out

    return doc.memo("section-types", build)


_SECT_OPEN = re.compile(rb"<(?:[\w.-]+:)?sectPr[\s/>]")
_SECT_CHANGE = re.compile(rb"<((?:[\w.-]+:)?)sectPrChange\b.*?</\1sectPrChange>", re.S)
_TYPE = re.compile(rb'<(?:[\w.-]+:)?type\b[^>]*?\bval\s*=\s*["\']([^"\']*)["\']')


def _body_sect_type(doc: Document) -> str:
    """The start type of the last section, from the body-level ``w:sectPr``, read by
    pattern (it may carry ``r:id`` references whose namespace a fragment parse lacks);
    a tracked ``w:sectPrChange`` inside it holds the old properties and is skipped."""
    data = doc.index.spans.data
    after = doc.index.spans.paragraphs[-1].end if doc.index.spans.paragraphs else 0
    opening = _SECT_OPEN.search(data, after)
    if opening is None:
        return "nextPage"
    fragment = _SECT_CHANGE.sub(b"", data[opening.start() :])
    found = _TYPE.search(fragment)
    return found.group(1).decode("ascii", "replace") if found else "nextPage"


def page_break_before(doc: Document, para: Paragraph) -> bool:
    elem = _elem(doc, para)
    direct = para_format(elem.find(W + "pPr")).get("page_break_before")
    if direct is None:
        style = doc.style_id(para)
        direct = doc.styles.effective(style)[0].get("page_break_before") if style else None
    if direct:
        return True
    first = next(_events(elem), False)
    if first:
        return True
    prev = _previous(doc, para)
    if prev is None:
        return False
    prev_elem = _elem(doc, prev)
    if _section_break_before(doc, prev):
        return True
    last = None
    for item in _events(prev_elem):
        last = item
    return last is True
