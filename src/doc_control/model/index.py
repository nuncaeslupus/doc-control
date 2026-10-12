"""The paragraph index of ``word/document.xml``: visible text mapped back to runs.

:func:`build_index` takes the paragraph spans from :func:`doc_control.ooxml.splice.scan`
and, for each paragraph of the main story, computes its *visible text* and a list
of :class:`Piece` records saying which source element produced which characters.

Main story
    Numbering follows the spec: paragraphs in document order, table-cell and
    content-control paragraphs included. A paragraph inside another paragraph
    (a text box) belongs to a different story: it is not numbered and its text
    is not part of the enclosing paragraph.

Visible text
    Included: ``w:t`` text, ``w:tab`` as ``\\t``, ``w:br`` and ``w:cr`` as ``\\n``
    (page and column breaks add nothing), ``w:noBreakHyphen`` as U+2011,
    ``w:softHyphen`` as U+00AD; text inside ``w:ins``/``w:moveTo``, hyperlinks,
    smart tags, content controls and field results.
    Excluded: ``w:delText`` and everything in ``w:del``/``w:moveFrom``, field
    instructions (between a field's ``begin`` and ``separate``, nested fields
    included, and across paragraphs for a field that spans them), and runs whose
    own ``w:rPr`` has ``w:vanish`` switched on. Hidden text that comes from a
    character style is not resolved here (that needs ``styles.xml``).

Offsets are indexes into the visible text as a Python ``str`` (code points).
"""

from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import Address, AddressRange, fingerprint, parse_address, parse_range
from doc_control.ooxml.splice import W_NS, DocumentSpans, scan
from doc_control.ooxml.zipio import Package

W = f"{{{W_NS}}}"
DOCUMENT_PART = "word/document.xml"

# Containers whose children never carry visible body text.
_SKIP = frozenset(
    W + name
    for name in ("pPr", "rPr", "sdtPr", "sdtEndPr", "del", "moveFrom", "txbxContent", "delText")
)
_VANISH_OFF = frozenset({"0", "false", "off"})
_BREAKS_WITHOUT_TEXT = frozenset({"page", "column"})
_FIXED_TEXT = {
    W + "tab": "\t",
    W + "cr": "\n",
    W + "noBreakHyphen": "\N{NON-BREAKING HYPHEN}",
    W + "softHyphen": "\N{SOFT HYPHEN}",
}

_Emit = Callable[[str, tuple[int, ...]], None]


@dataclass(frozen=True)
class Piece:
    """Visible characters ``[start, end)`` of a paragraph and where they come from.

    ``path`` lists child indexes from the ``w:p`` element (as returned by
    :meth:`DocumentSpans.parse_paragraph`, comments counted) down to the source
    element: a ``w:t``, ``w:tab``, ``w:br``… ``path[:-1]`` is its run. For a
    ``w:t``, character ``i`` of the paragraph is character ``i - start`` of the
    element's text.
    """

    start: int
    end: int
    path: tuple[int, ...]


@dataclass(frozen=True)
class Paragraph:
    """One indexed paragraph of the main story."""

    number: int  # 1-based
    span_index: int  # position in DocumentSpans.paragraphs
    text: str
    fp: str
    pieces: tuple[Piece, ...] = field(repr=False)

    @property
    def address(self) -> Address:
        return Address(self.number, self.fp)

    def pieces_in(self, start: int, end: int) -> list[Piece]:
        """The pieces overlapping visible characters ``[start, end)``."""
        if start >= end:
            return []
        first = max(bisect.bisect_right(self.pieces, start, key=lambda p: p.start) - 1, 0)
        out: list[Piece] = []
        for piece in self.pieces[first:]:
            if piece.start >= end:
                break
            if piece.end > start:
                out.append(piece)
        return out


@dataclass(frozen=True)
class ResolvedRange:
    """A range checked against the document, with offsets filled in.

    A whole-paragraph range covers ``0`` to the end of the last paragraph. For a
    sentence range the offsets stay ``None``: segmentation resolves them.
    """

    range: AddressRange
    first: Paragraph
    last: Paragraph
    start_offset: int | None
    end_offset: int | None

    @property
    def sentence(self) -> int | None:
        return self.range.sentence


def _vanished(run: ET.Element) -> bool:
    rpr = run.find(W + "rPr")
    vanish = rpr.find(W + "vanish") if rpr is not None else None
    return vanish is not None and vanish.get(W + "val", "1").lower() not in _VANISH_OFF


class _Walker:
    """Reads paragraph text in document order, carrying field state across paragraphs."""

    def __init__(self) -> None:
        # One entry per open field: True once its ``separate`` was seen (result part).
        self.fields: list[bool] = []

    def paragraph(self, root: ET.Element) -> tuple[str, tuple[Piece, ...]]:
        parts: list[str] = []
        pieces: list[Piece] = []
        pos = 0

        def emit(text: str, path: tuple[int, ...]) -> None:
            nonlocal pos
            if text:
                parts.append(text)
                pieces.append(Piece(pos, pos + len(text), path))
                pos += len(text)

        def walk(elem: ET.Element, path: tuple[int, ...]) -> None:
            for i, child in enumerate(elem):
                tag = child.tag
                if not isinstance(tag, str) or tag in _SKIP:
                    continue
                if tag == W + "r":
                    self.run(child, (*path, i), emit)
                elif tag.startswith(W):
                    walk(child, (*path, i))

        walk(root, ())
        return "".join(parts), tuple(pieces)

    def run(self, run: ET.Element, path: tuple[int, ...], emit: _Emit) -> None:
        hidden = _vanished(run)
        for i, child in enumerate(run):
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag == W + "fldChar":
                self.field_char(child.get(W + "fldCharType"))
            elif hidden or not all(self.fields):
                continue
            elif tag == W + "t":
                emit("".join(child.itertext()), (*path, i))
            elif tag in _FIXED_TEXT:
                emit(_FIXED_TEXT[tag], (*path, i))
            elif tag == W + "br" and child.get(W + "type") not in _BREAKS_WITHOUT_TEXT:
                emit("\n", (*path, i))

    def field_char(self, kind: str | None) -> None:
        if kind == "begin":
            self.fields.append(False)
        elif kind == "separate" and self.fields:
            self.fields[-1] = True
        elif kind == "end" and self.fields:
            self.fields.pop()


class DocumentIndex:
    """Every main-story paragraph of one ``word/document.xml``, addressable."""

    def __init__(self, spans: DocumentSpans, paragraphs: Sequence[Paragraph]) -> None:
        self.spans = spans
        self.paragraphs: tuple[Paragraph, ...] = tuple(paragraphs)

    def __len__(self) -> int:
        return len(self.paragraphs)

    def paragraph(self, number: int) -> Paragraph:
        """Paragraph ``number`` (1-based); ``STALE_ADDRESS`` when there is none."""
        if not 1 <= number <= len(self.paragraphs):
            raise DocControlError(
                ErrorCode.STALE_ADDRESS,
                f"p{number} is outside the document ({len(self.paragraphs)} paragraphs)",
                number=number,
                paragraphs=len(self.paragraphs),
            )
        return self.paragraphs[number - 1]

    def resolve_address(self, address: Address | str) -> Paragraph:
        """The paragraph at ``address``; ``STALE_ADDRESS`` unless position and fingerprint match."""
        if isinstance(address, str):
            address = parse_address(address)
        para = self.paragraph(address.number)
        if para.fp != address.fp:
            raise DocControlError(
                ErrorCode.STALE_ADDRESS,
                f"{address} no longer matches the document",
                address=str(address),
                current=str(para.address),
            )
        return para

    def resolve_range(self, value: AddressRange | str) -> ResolvedRange:
        """Check a range against the document and fill in its offsets.

        ``STALE_ADDRESS`` for a position or fingerprint that does not match;
        ``ADDRESS_INVALID`` for an offset beyond its paragraph's text.
        """
        rng = parse_range(value) if isinstance(value, str) else value
        first = self.resolve_address(rng.start)
        last = first if rng.end == rng.start else self.resolve_address(rng.end)
        start, end = rng.start_offset, rng.end_offset
        if rng.sentence is None:
            if start is None or end is None:
                start, end = 0, len(last.text)
            elif start > len(first.text) or end > len(last.text) or (first is last and start > end):
                raise DocControlError(
                    ErrorCode.ADDRESS_INVALID,
                    f"{rng} has an offset beyond its paragraph's text",
                    address=str(rng),
                )
        return ResolvedRange(rng, first, last, start, end)

    @classmethod
    def from_package(cls, pkg: Package) -> DocumentIndex:
        return build_index(pkg.read(DOCUMENT_PART))


def build_index(data: bytes) -> DocumentIndex:
    """Index the bytes of a ``word/document.xml``; tokenizer errors propagate."""
    spans = scan(data)
    walker = _Walker()
    paragraphs: list[Paragraph] = []
    for span in spans.paragraphs:
        if span.nested:
            continue
        text, pieces = walker.paragraph(spans.parse_paragraph(span.index))
        number = len(paragraphs) + 1
        paragraphs.append(Paragraph(number, span.index, text, fingerprint(text), pieces))
    return DocumentIndex(spans, paragraphs)
