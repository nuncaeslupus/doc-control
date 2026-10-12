"""Quote anchors: a passage named by its text and context, found again after edits.

An anchor is a quote plus the characters before and after it (``prefix``, ``suffix``),
the paragraph number it was taken from (``hint``) and that paragraph's fingerprint.
Unlike an address it survives edits: :func:`resolve` looks the quote up again and says
where it is now, how well it matches, or that it cannot tell.

Matching
    Exact first: every place the quote occurs verbatim is a candidate. Only when no exact
    candidate is good enough does the fuzzy search run, over a short list of paragraphs
    (the paragraph with the anchor's fingerprint, the ones near the hint, and the ones
    sharing the most quote words, rare words counting more). In each, ``difflib`` aligns
    the quote with the text and the densest run of matching blocks gives a window; the
    same paragraph is searched again with that window masked, so a repeated phrase shows
    up as several candidates.

Score
    ``0.5*quote + 0.2*prefix + 0.2*suffix + 0.1*position``, each part in ``[0, 1]``: the
    similarity of the quote, of the text before and of the text after (context may reach
    into neighbouring paragraphs, joined by ``"\\n"``), and how close the paragraph number
    is to the hint. Below :data:`MIN_SCORE` a candidate is not accepted. When the best
    accepted candidates are within :data:`TIE_MARGIN` of each other, the anchor is
    ambiguous: all of them are reported and none is chosen.

Offsets are code points into a paragraph's visible text, like every other address. A
range never starts or ends between a character and the combining marks after it.
Quotes are limited to one paragraph.
"""

from __future__ import annotations

import difflib
import itertools
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex, Paragraph
from doc_control.model.segment import DEFAULT_LANGUAGE, split_sentences

MIN_SCORE = 0.80
TIE_MARGIN = 0.02
MAX_REPORTED = 5
CONTEXT_CHARS = 32
MAX_CONTEXT_CHARS = 200
MAX_QUOTE_CHARS = 1000
W_QUOTE, W_PREFIX, W_SUFFIX, W_POSITION = 0.5, 0.2, 0.2, 0.1
POSITION_REACH = 200  # paragraphs away from the hint at which the position part reaches 0

_FLOOR = 1e-9  # slack so 0.8 computed as 0.7999999999 is still 0.8
_MAX_FUZZY_PARAGRAPHS = 40
_NEIGHBOURS = 3  # paragraphs on each side of the hint always searched
_MAX_WINDOWS = 3  # fuzzy windows taken from one paragraph
_MIN_WINDOW_RATIO = 0.4
_GRAM = 5  # characters in a run used to shortlist paragraphs
_GRAM_SHARE = 0.25  # share of the quote's runs a shortlisted paragraph must hold

MatchKind = Literal["exact", "fuzzy"]


@dataclass(frozen=True)
class Anchor:
    """The JSON form is :meth:`to_dict`; only ``exact`` is required."""

    exact: str
    prefix: str = ""
    suffix: str = ""
    hint: int | None = None  # paragraph number at creation
    fingerprint: str | None = None  # of that paragraph then
    at: str | None = None  # the range at creation, informative
    sentence: str | None = None  # sentence address at creation, if one was anchored

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"exact": self.exact, "prefix": self.prefix, "suffix": self.suffix}
        if self.hint is not None:
            out["hint"] = f"p{self.hint}"
        if self.fingerprint is not None:
            out["fingerprint"] = self.fingerprint
        if self.at is not None:
            out["at"] = self.at
        if self.sentence is not None:
            out["sentence"] = self.sentence
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Anchor:
        hint = data.get("hint")
        number: int | None = None
        if hint is not None:
            m = re.fullmatch(r"p([1-9][0-9]{0,11})", hint)
            if m is None:
                raise DocControlError(
                    ErrorCode.SCHEMA_INVALID,
                    f"hint {hint!r} is not a paragraph number like p142",
                    path="$.anchor.hint",
                )
            number = int(m[1])
        return cls(
            exact=data["exact"],
            prefix=data.get("prefix", ""),
            suffix=data.get("suffix", ""),
            hint=number,
            fingerprint=data.get("fingerprint"),
            at=data.get("at"),
            sentence=data.get("sentence"),
        )


@dataclass(frozen=True)
class Candidate:
    """A place the anchor may now be: paragraph ``number`` characters ``[start, end)``."""

    number: int
    fp: str
    start: int
    end: int
    score: float
    kind: MatchKind

    @property
    def at(self) -> str:
        return f"p{self.number}#{self.fp}:{self.start}-{self.end}"

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at, "score": round(self.score, 4)}


@dataclass(frozen=True)
class Resolution:
    """The outcome of :func:`resolve`.

    ``found``: ``best`` is the answer. ``ambiguous``: ``candidates`` (best first, at most
    :data:`MAX_REPORTED`) are within the tie margin. ``not_found``: ``best`` is the closest
    thing below the minimum score, if anything came close at all.
    """

    status: Literal["found", "ambiguous", "not_found"]
    best: Candidate | None
    candidates: tuple[Candidate, ...] = ()


def _is_mark(ch: str) -> bool:
    return unicodedata.category(ch).startswith("M")


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, _nfc(a), _nfc(b), autojunk=False).ratio()


# -- context -----------------------------------------------------------------


def _text_before(index: DocumentIndex, number: int, offset: int, size: int) -> str:
    """Up to ``size`` characters ending at ``offset``, reaching back over earlier paragraphs."""
    if size <= 0:
        return ""
    text = index.paragraphs[number - 1].text
    start = max(0, offset - size)
    while 0 < start < offset and _is_mark(text[start]):
        start -= 1
    out = text[start:offset]
    k = number - 1
    while len(out) < size and k >= 1:
        prev = index.paragraphs[k - 1].text
        room = size - len(out) - 1
        out = (prev[max(0, len(prev) - room) :] if room > 0 else "") + "\n" + out
        k -= 1
    return out[-size:] if len(out) > size else out


def _text_after(index: DocumentIndex, number: int, offset: int, size: int) -> str:
    if size <= 0:
        return ""
    text = index.paragraphs[number - 1].text
    end = min(len(text), offset + size)
    while offset < end < len(text) and _is_mark(text[end]):
        end += 1
    out = text[offset:end]
    k = number + 1
    while len(out) < size and k <= len(index):
        nxt = index.paragraphs[k - 1].text
        room = size - len(out) - 1
        out = out + "\n" + (nxt[:room] if room > 0 else "")
        k += 1
    return out[:size] if len(out) > size else out


# -- create ------------------------------------------------------------------


def _snap(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen ``[start, end)`` so it neither starts on nor ends before a combining mark."""
    while 0 < start < len(text) and _is_mark(text[start]):
        start -= 1
    while 0 < end < len(text) and _is_mark(text[end]):
        end += 1
    return start, end


def create(
    index: DocumentIndex,
    at: str,
    context: int = CONTEXT_CHARS,
    language: str = DEFAULT_LANGUAGE,
) -> Anchor:
    """The anchor for the range ``at`` (any address form), which must lie in one paragraph."""
    resolved = index.resolve_range(at, language)
    if resolved.first is not resolved.last:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            "an anchor covers one paragraph; anchor each paragraph of the range separately",
            path="$.at",
        )
    para = resolved.first
    start, end = _snap(para.text, resolved.start_offset or 0, resolved.end_offset or 0)
    quote = para.text[start:end]
    if not quote.strip():
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "cannot anchor empty text", path="$.at")
    if len(quote) > MAX_QUOTE_CHARS:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"quote is {len(quote)} characters; an anchor holds at most {MAX_QUOTE_CHARS}",
            path="$.at",
        )
    sentence = None
    if resolved.sentence is not None:
        sentence = f"{para.address}.s{resolved.sentence}"
    return Anchor(
        exact=quote,
        prefix=_text_before(index, para.number, start, context),
        suffix=_text_after(index, para.number, end, context),
        hint=para.number,
        fingerprint=para.fp,
        at=f"{para.address}:{start}-{end}",
        sentence=sentence,
    )


# -- resolve -----------------------------------------------------------------


def _position(anchor: Anchor, number: int) -> float:
    if anchor.hint is None:
        return 1.0
    return max(0.0, 1.0 - abs(number - anchor.hint) / POSITION_REACH)


def _candidate(
    index: DocumentIndex,
    anchor: Anchor,
    para: Paragraph,
    start: int,
    end: int,
    quote_score: float,
    kind: MatchKind,
) -> Candidate:
    prefix = (
        _similarity(anchor.prefix, _text_before(index, para.number, start, len(anchor.prefix)))
        if anchor.prefix
        else 1.0
    )
    suffix = (
        _similarity(anchor.suffix, _text_after(index, para.number, end, len(anchor.suffix)))
        if anchor.suffix
        else 1.0
    )
    score = (
        W_QUOTE * quote_score
        + W_PREFIX * prefix
        + W_SUFFIX * suffix
        + W_POSITION * _position(anchor, para.number)
    )
    return Candidate(para.number, para.fp, start, end, score, kind)


def _exact_spans(text: str, quote: str) -> list[tuple[int, int]]:
    """Verbatim occurrences of ``quote`` that do not cut a character from its marks."""
    out: list[tuple[int, int]] = []
    i = text.find(quote)
    while i >= 0:
        j = i + len(quote)
        if not (j < len(text) and _is_mark(text[j])) and not (i > 0 and _is_mark(text[i])):
            out.append((i, j))
        i = text.find(quote, i + 1)
    return out


def resolve(index: DocumentIndex, anchor: Anchor, *, exact_only: bool = False) -> Resolution:
    """Find ``anchor`` in ``index``.

    With ``exact_only`` the fuzzy search is skipped: only verbatim occurrences of the
    quote are candidates (the extension point for ``anchor.resolve`` ``mode: "exact"``).
    """
    quote = anchor.exact
    found: list[Candidate] = []
    taken: dict[int, list[tuple[int, int]]] = {}
    for para in index.paragraphs:
        if len(para.text) < len(quote):
            continue
        for start, end in _exact_spans(para.text, quote):
            taken.setdefault(para.number, []).append((start, end))
            found.append(_candidate(index, anchor, para, start, end, 1.0, "exact"))
    accepted = [c for c in found if c.score + _FLOOR >= MIN_SCORE]
    if not accepted and not exact_only:
        found += _fuzzy(index, anchor, taken)
        accepted = [c for c in found if c.score + _FLOOR >= MIN_SCORE]
    ranked = sorted(found, key=lambda c: (-c.score, c.number, c.start))
    if not accepted:
        return Resolution("not_found", ranked[0] if ranked else None)
    accepted.sort(key=lambda c: (-c.score, c.number, c.start))
    best = accepted[0]
    tied = [c for c in accepted if best.score - c.score <= TIE_MARGIN + _FLOOR]
    if len(tied) > 1:
        return Resolution("ambiguous", None, tuple(tied[:MAX_REPORTED]))
    return Resolution("found", best)


def _fuzzy_paragraphs(index: DocumentIndex, anchor: Anchor) -> list[Paragraph]:
    """The paragraphs worth a fuzzy comparison, in a stable order."""
    chosen: dict[int, Paragraph] = {}
    if anchor.hint is not None:
        for n in range(anchor.hint - _NEIGHBOURS, anchor.hint + _NEIGHBOURS + 1):
            if 1 <= n <= len(index):
                chosen[n] = index.paragraphs[n - 1]
    if anchor.fingerprint is not None:
        for para in index.paragraphs:
            if para.fp == anchor.fingerprint:
                chosen[para.number] = para
    # Character runs of the quote that survive a typo: the paragraphs holding the most of
    # them (rare runs counting more) are the likeliest homes of an edited quote.
    quote = unicodedata.normalize("NFC", anchor.exact)
    size = min(_GRAM, len(quote))
    wanted = {quote[i : i + size] for i in range(len(quote) - size + 1)}
    wanted = {g for g in wanted if g.strip()}
    if wanted:
        need = max(1, math.ceil(len(wanted) * _GRAM_SHARE))
        shared: list[tuple[Paragraph, list[str]]] = []
        df: Counter[str] = Counter()
        for para in index.paragraphs:
            if len(para.text) < size:
                continue
            common = [g for g in wanted if g in para.text]
            if len(common) >= need:
                shared.append((para, common))
                df.update(common)
        hint = anchor.hint or 0

        def rank(item: tuple[Paragraph, list[str]]) -> tuple[float, int, int]:
            para, common = item
            weight = sum(1.0 / df[g] for g in common)
            return (-weight, abs(para.number - hint), para.number)

        for para, _ in sorted(shared, key=rank)[:_MAX_FUZZY_PARAGRAPHS]:
            chosen[para.number] = para
    return [chosen[n] for n in sorted(chosen)]


def _fuzzy(
    index: DocumentIndex, anchor: Anchor, taken: dict[int, list[tuple[int, int]]]
) -> list[Candidate]:
    out: list[Candidate] = []
    quote = anchor.exact
    for para in _fuzzy_paragraphs(index, anchor):
        text = para.text
        for s, e in taken.get(para.number, ()):
            text = text[:s] + "\x00" * (e - s) + text[e:]
        for _ in range(_MAX_WINDOWS):
            window = _best_window(quote, text)
            if window is None:
                break
            start, end, ratio = window
            out.append(_candidate(index, anchor, para, start, end, ratio, "fuzzy"))
            text = text[:start] + "\x00" * (end - start) + text[end:]
    return out


def _best_window(quote: str, text: str) -> tuple[int, int, float] | None:
    """The stretch of ``text`` most like ``quote``, with its similarity, or ``None``."""
    m = len(quote)
    if m == 0 or len(text) == 0:
        return None
    matcher = difflib.SequenceMatcher(None, text, quote, autojunk=False)
    floor = max(1, min(3, m // 2))
    blocks = [b for b in matcher.get_matching_blocks() if b.size >= floor]
    if not blocks:
        return None
    # Group blocks that sit where the quote would: the text between two blocks may be
    # longer than the quote between them (an insertion), but only by a bounded amount.
    slack = max(10, m // 4)
    groups: list[list[difflib.Match]] = [[blocks[0]]]
    for prev, cur in itertools.pairwise(blocks):
        gap_text = cur.a - (prev.a + prev.size)
        gap_quote = cur.b - (prev.b + prev.size)
        if gap_text <= gap_quote + slack:
            groups[-1].append(cur)
        else:
            groups.append([cur])
    group = max(groups, key=lambda g: sum(b.size for b in g))
    first, last = group[0], group[-1]
    starts = {max(0, first.a - first.b), first.a}
    ends = {min(len(text), last.a + last.size + (m - last.b - last.size)), last.a + last.size}
    best: tuple[int, int, float] | None = None
    for start in sorted(starts):
        for end in sorted(ends, reverse=True):
            if end <= start:
                continue
            s, e = _snap(text, start, end)
            piece = text[s:e]
            if "\x00" in piece:
                continue
            ratio = difflib.SequenceMatcher(None, _nfc(quote), _nfc(piece), autojunk=False).ratio()
            if best is None or ratio > best[2] + 0.005:
                best = (s, e, ratio)
    if best is None or best[2] < _MIN_WINDOW_RATIO:
        return None
    return best


def sentence_of(
    index: DocumentIndex, candidate: Candidate, language: str = DEFAULT_LANGUAGE
) -> str:
    """The address of the sentence that holds the start of ``candidate``."""
    para = index.paragraphs[candidate.number - 1]
    spans = split_sentences(para.text, language)
    k = 1
    for i, span in enumerate(spans, 1):
        if span.start <= candidate.start:
            k = i
    return f"{para.address}.s{k}"
