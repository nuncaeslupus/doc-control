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
    Also shown: ``w:sym`` as its one ``w:char`` character, ``w:ptab`` as ``\\t``,
    the base text of ``w:ruby`` (not the ``w:rt`` annotation) and, in a
    paragraph-level ``mc:AlternateContent``, exactly one branch: the first
    ``mc:Choice`` whose ``Requires`` prefixes resolve (in the paragraph's
    namespace scope) to a Word namespace, otherwise the ``mc:Fallback``.
    Not read: equations (``m:oMath``), which are not prose, and anything in a
    text box, drawing or picture.

Offsets
    Offsets are indexes into the visible text as a Python ``str``, that is code
    points, not user-perceived characters: a combining mark is its own offset, so
    a range can split it from its base letter. What a write does about that is
    decided by the edit operations (T11), not here.
"""

from __future__ import annotations

import bisect
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import Address, AddressRange, fingerprint, parse_address, parse_range
from doc_control.model.segment import DEFAULT_LANGUAGE, sentence_span
from doc_control.ooxml.splice import MC_NS, W_NS, DocumentSpans, scan
from doc_control.ooxml.zipio import Package

W = f"{{{W_NS}}}"
MC = f"{{{MC_NS}}}"
DOCUMENT_PART = "word/document.xml"

# Containers whose children never carry visible body text.
_SKIP = frozenset(
    W + name
    for name in ("pPr", "rPr", "sdtPr", "sdtEndPr", "del", "moveFrom", "txbxContent", "delText")
)
# Namespaces whose markup Word understands, for choosing an mc:Choice branch.
_WORD_NAMESPACES = (W_NS, "http://schemas.microsoft.com/office/word/")
_VANISH_OFF = frozenset({"0", "false", "off"})
_BREAKS_WITHOUT_TEXT = frozenset({"page", "column"})
_FIXED_TEXT = {
    W + "tab": "\t",
    W + "cr": "\n",
    W + "noBreakHyphen": "\N{NON-BREAKING HYPHEN}",
    W + "softHyphen": "\N{SOFT HYPHEN}",
    W + "ptab": "\t",
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

    A whole-paragraph range covers ``0`` to the end of the last paragraph. A sentence
    range (``.s<k>``) gets the offsets of that sentence, found by segmentation.
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

    def paragraph(
        self, root: ET.Element, scope: Mapping[str, str]
    ) -> tuple[str, tuple[Piece, ...]]:
        parts: list[str] = []
        pieces: list[Piece] = []
        pos = 0

        def emit(text: str, path: tuple[int, ...]) -> None:
            nonlocal pos
            if text:
                parts.append(text)
                pieces.append(Piece(pos, pos + len(text), path))
                pos += len(text)

        self.walk(root, (), scope, emit)
        return "".join(parts), tuple(pieces)

    def walk(
        self, elem: ET.Element, path: tuple[int, ...], scope: Mapping[str, str], emit: _Emit
    ) -> None:
        for i, child in enumerate(elem):
            tag = child.tag
            if not isinstance(tag, str) or tag in _SKIP:
                continue
            if tag == W + "r":
                self.run(child, (*path, i), scope, emit)
            elif tag == MC + "AlternateContent":
                branch = _branch(child, scope)
                if branch is not None:
                    j, chosen = branch
                    self.walk(chosen, (*path, i, j), scope, emit)
            elif tag.startswith(W):
                self.walk(child, (*path, i), scope, emit)

    def run(
        self, run: ET.Element, path: tuple[int, ...], scope: Mapping[str, str], emit: _Emit
    ) -> None:
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
            elif tag == W + "sym":
                emit(_symbol(child), (*path, i))
            elif tag == W + "ruby":
                for j, part in enumerate(child):
                    if part.tag == W + "rubyBase":  # the annotation (w:rt) is not body text
                        self.walk(part, (*path, i, j), scope, emit)

    def field_char(self, kind: str | None) -> None:
        if kind == "begin":
            self.fields.append(False)
        elif kind == "separate" and self.fields:
            self.fields[-1] = True
        elif kind == "end" and self.fields:
            self.fields.pop()


def _symbol(sym: ET.Element) -> str:
    """The one character of a ``w:sym`` (its ``w:char`` hex code); empty if malformed."""
    try:
        code = int(sym.get(W + "char", ""), 16)
    except ValueError:
        return ""
    if code == 0 or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
        return ""
    return chr(code)


def _branch(alt: ET.Element, scope: Mapping[str, str]) -> tuple[int, ET.Element] | None:
    """The single branch of an ``mc:AlternateContent`` to read: never both.

    The first ``mc:Choice`` whose ``Requires`` prefixes all resolve, in the
    paragraph's namespace scope, to a Word namespace; otherwise the Fallback.
    """
    fallback: tuple[int, ET.Element] | None = None
    for j, child in enumerate(alt):
        if child.tag == MC + "Choice":
            prefixes = child.get("Requires", "").split()
            if prefixes and all(_understood(scope.get(p)) for p in prefixes):
                return j, child
        elif child.tag == MC + "Fallback" and fallback is None:
            fallback = j, child
    return fallback


def _understood(uri: str | None) -> bool:
    return uri is not None and uri.startswith(_WORD_NAMESPACES)


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

    def resolve_range(
        self, value: AddressRange | str, language: str = DEFAULT_LANGUAGE
    ) -> ResolvedRange:
        """Check a range against the document and fill in its offsets.

        ``STALE_ADDRESS`` for a position or fingerprint that does not match;
        ``ADDRESS_INVALID`` for an offset beyond its paragraph's text or a sentence
        number beyond the paragraph's sentences (split per ``language``).
        """
        rng = parse_range(value) if isinstance(value, str) else value
        first = self.resolve_address(rng.start)
        last = first if rng.end == rng.start else self.resolve_address(rng.end)
        start, end = rng.start_offset, rng.end_offset
        if rng.sentence is not None:
            span = sentence_span(first.text, rng.sentence, language)
            if span is None:
                raise DocControlError(
                    ErrorCode.ADDRESS_INVALID,
                    f"{rng} names a sentence the paragraph does not have",
                    address=str(rng),
                )
            start, end = span.start, span.end
        else:
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
    """Index the bytes of a ``word/document.xml``; tokenizer errors propagate.

    Field state carries from one paragraph to the next so a field that spans
    paragraphs (a table of contents) keeps its instruction hidden. If the body
    ends with a field still open (a ``begin`` with no ``end``), that carry would
    blank every later paragraph, so the document is indexed again with the field
    state reset at each paragraph.
    """
    spans = scan(data)
    paragraphs, open_fields = _paragraphs(spans, carry=True)
    if open_fields:
        paragraphs, _ = _paragraphs(spans, carry=False)
    return DocumentIndex(spans, paragraphs)


def _paragraphs(spans: DocumentSpans, *, carry: bool) -> tuple[list[Paragraph], bool]:
    walker = _Walker()
    paragraphs: list[Paragraph] = []
    for span in spans.paragraphs:
        if span.nested:
            continue
        if not carry:
            walker.fields.clear()
        text, pieces = walker.paragraph(spans.parse_paragraph(span.index), span.scope)
        number = len(paragraphs) + 1
        paragraphs.append(Paragraph(number, span.index, text, fingerprint(text), pieces))
    return paragraphs, bool(walker.fields)
