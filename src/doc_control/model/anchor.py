"""Quote anchors: a passage named by its text and context, found again after edits.

An anchor is a quote plus the characters before and after it (``prefix``, ``suffix``),
the paragraph number it was taken from (``hint``) and that paragraph's fingerprint.
Unlike an address it survives edits: :func:`resolve` looks the quote up again and says
where it is now, how well it matches, or that it cannot tell.

Matching
    Exact first: every place the quote occurs verbatim is a candidate. Only when no exact
    candidate is good enough does the fuzzy search run, over a short list of paragraphs
    (the paragraph with the anchor's fingerprint, the ones near the hint, and the ones
    sharing the most character runs of the quote, rare runs counting more). In each,
    ``difflib`` aligns the quote with the text and the densest run of matching blocks gives
    a window, whose ends are then moved by whole words while that helps; the same paragraph
    is searched again with that window masked, so a repeated phrase shows up as several
    candidates. Quotes of three words or fewer are never matched fuzzily.

Score
    ``(0.5*quote + 0.2*prefix + 0.2*suffix) / 0.9``, each part in ``[0, 1]``, compared word
    by word: a word with a typo (character similarity >= 0.8) counts by that similarity,
    a different word counts nothing, longer words weigh more, and in a fuzzy quote a
    different word also scales the quote part down. The context is the text before and
    after (it may reach into neighbouring paragraphs, joined by ``"\\n"``), and counts by how
    much of the old context is still there. Position is not scored: it only orders equal
    scores. A candidate in the anchor's own paragraph (same fingerprint) or within
    :data:`NEAR_PARAGRAPHS` of its hint needs :data:`MIN_SCORE`; any other needs
    :data:`FAR_MIN_SCORE`; a short quote needs :data:`SHORT_MIN_SCORE` wherever it is.
    When anything scoring at least :data:`MIN_SCORE` is within :data:`TIE_MARGIN` of the
    best, the anchor is ambiguous and the candidates are reported, unless exactly one of
    them is in a paragraph with the anchor's fingerprint.

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
FAR_MIN_SCORE = 0.85  # minimum for a candidate that is not where the anchor came from
NEAR_PARAGRAPHS = 8  # "where the anchor came from": the hint's paragraph number +- this
SHORT_QUOTE_WORDS = 3  # quotes this short are never matched fuzzily
SHORT_MIN_SCORE = 0.95  # and need nearly all of their context to match
W_QUOTE, W_PREFIX, W_SUFFIX = 0.5, 0.2, 0.2
TOKEN_MATCH = 0.8  # character similarity at which two words count as the same word, typo aside

_FLOOR = 1e-9  # slack so 0.8 computed as 0.7999999999 is still 0.8
_MAX_FUZZY_PARAGRAPHS = 40
_NEIGHBOURS = 3  # paragraphs on each side of the hint always searched
_MAX_WINDOWS = 3  # fuzzy windows taken from one paragraph
_MIN_WINDOW_RATIO = 0.4
_WORD = re.compile(r"\w+")
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
    near: bool = True  # in the anchor's own paragraph (by fingerprint) or close to its hint

    @property
    def threshold(self) -> float:
        return MIN_SCORE if self.near else FAR_MIN_SCORE

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


def _tokens(text: str) -> list[str]:
    return _WORD.findall(_nfc(text).casefold())


def _token_credit(ta: list[str], tb: list[str]) -> float:
    return _token_credit_pair(ta, tb)[0]


def _token_credit_pair(ta: list[str], tb: list[str]) -> tuple[float, int]:
    """Characters of ``ta`` that ``tb`` matches: whole words fully, a word with a typo
    (character similarity of at least :data:`TOKEN_MATCH`) by that similarity, a different
    word not at all."""
    credit = 0.0
    matched = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if tag == "equal":
            n = sum(len(t) for t in ta[i1:i2])
            credit += n
            matched += n
        elif tag == "replace":
            for x, y in zip(ta[i1:i2], tb[j1:j2], strict=False):
                r = difflib.SequenceMatcher(None, x, y, autojunk=False).ratio()
                if r >= TOKEN_MATCH:
                    credit += r * len(x)
                    matched += len(x)
    return credit, matched


def _similarity(a: str, b: str) -> float:
    """How alike two stretches of text are, word by word, in ``[0, 1]``.

    Longer words weigh more, so changing a content word costs far more than changing
    "la"; a word with a typo still counts. Text without words falls back to character
    similarity.
    """
    if a == b:
        return 1.0
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return difflib.SequenceMatcher(None, _nfc(a), _nfc(b), autojunk=False).ratio()
    if not ta or not tb:
        return 0.0
    total = max(sum(map(len, ta)), sum(map(len, tb)))
    return min(1.0, _token_credit(ta, tb) / total)


def _quote_similarity(quote: str, found: str) -> float:
    """Word-level similarity of a quote to what was found, for fuzzy matches.

    Typos cost a little. A word that is simply different costs twice: it earns no credit
    and it also scales the whole score down, so a near-copy with one content word changed
    cannot pass for the quote.
    """
    ta, tb = _tokens(quote), _tokens(found)
    if not ta or not tb:
        return _similarity(quote, found)
    credit, matched = _token_credit_pair(ta, tb)
    total = max(sum(map(len, ta)), sum(map(len, tb)))
    return min(1.0, credit / total) * (matched / sum(map(len, ta)))


def _word_edges(text: str, lo: int, hi: int) -> list[tuple[int, int]]:
    """Windows one word wider or narrower than ``text[lo:hi]`` at either end."""
    out: list[tuple[int, int]] = []
    k = lo
    while k > 0 and text[k - 1].isspace():
        k -= 1
    if k > 0:
        while k > 0 and (text[k - 1].isalnum() or _is_mark(text[k - 1])):
            k -= 1
        out.append((k, hi))
    k = hi
    while k < len(text) and text[k].isspace():
        k += 1
    if k < len(text):
        while k < len(text) and (text[k].isalnum() or _is_mark(text[k])):
            k += 1
        out.append((lo, k))
    cut = text.find(" ", lo, hi)
    if cut >= 0:
        out.append((cut + 1, hi))
    cut = text.rfind(" ", lo, hi)
    if cut >= 0:
        out.append((lo, cut))
    return out


def _refine(quote: str, text: str, lo: int, hi: int) -> tuple[int, int]:
    """Move the ends of a window by whole words while that makes it more like the quote."""
    best = _quote_similarity(quote, text[lo:hi])
    for _ in range(6):
        scored = [
            (_quote_similarity(quote, text[a:b]), a, b)
            for a, b in _word_edges(text, lo, hi)
            if "\x00" not in text[a:b] and b > a
        ]
        if not scored:
            break
        top = max(scored)
        if top[0] <= best + 0.005:
            break
        best, lo, hi = top
    return lo, hi


def _context_similarity(anchor_side: str, actual: str, *, before: bool) -> float:
    """How much of the anchor's prefix (``before``) or suffix is found in the text now.

    ``actual`` is a longer stretch than the anchor's, so words inserted nearby do not push
    the old ones out of view; the outermost word on each side may be cut and is ignored.
    What counts is how much of the old context is still there, word for word.
    """
    ta, tb = _tokens(anchor_side), _tokens(actual)
    if len(ta) > 2:
        ta = ta[1:] if before else ta[:-1]
    if len(tb) > 2:
        tb = tb[1:] if before else tb[:-1]
    total = sum(map(len, ta))
    if total == 0:
        return 1.0
    return min(1.0, _token_credit(ta, tb) / total)


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


def _candidate(
    index: DocumentIndex,
    anchor: Anchor,
    para: Paragraph,
    start: int,
    end: int,
    quote_score: float,
    kind: MatchKind,
) -> Candidate:
    near = (para.fp == anchor.fingerprint) or (
        anchor.hint is not None and abs(para.number - anchor.hint) <= NEAR_PARAGRAPHS
    )
    wide = len(anchor.prefix) * 3 // 2 + 1, len(anchor.suffix) * 3 // 2 + 1
    prefix = (
        _context_similarity(
            anchor.prefix, _text_before(index, para.number, start, wide[0]), before=True
        )
        if anchor.prefix
        else 1.0
    )
    suffix = (
        _context_similarity(
            anchor.suffix, _text_after(index, para.number, end, wide[1]), before=False
        )
        if anchor.suffix
        else 1.0
    )
    # Normalised to [0, 1]; position is not part of it, it only orders equal scores.
    score = (W_QUOTE * quote_score + W_PREFIX * prefix + W_SUFFIX * suffix) / (
        W_QUOTE + W_PREFIX + W_SUFFIX
    )
    return Candidate(para.number, para.fp, start, end, score, kind, near)


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

    With ``exact_only`` (``anchor.resolve`` ``mode: "exact"``) the fuzzy search is skipped
    and every verbatim occurrence of the quote is accepted whatever its context score: the
    score only breaks ties, and several occurrences about equally good are ambiguous.
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
    short = len(_WORD.findall(quote)) <= SHORT_QUOTE_WORDS

    def accepted_of(pool: list[Candidate]) -> list[Candidate]:
        # A short quote has too little text to prove a match: it needs the stricter bar
        # even where it came from.
        if exact_only:
            return list(pool)
        return [c for c in pool if c.score + _FLOOR >= (SHORT_MIN_SCORE if short else c.threshold)]

    accepted = accepted_of(found)
    if not accepted and not exact_only and not short:
        found += _fuzzy(index, anchor, taken)
        accepted = accepted_of(found)
    order = anchor.hint or 0

    def key(c: Candidate) -> tuple[float, int, int, int]:
        # Position only orders results that score the same; it never lifts a score.
        return (-round(c.score, 6), abs(c.number - order), c.number, c.start)

    ranked = sorted(found, key=key)
    if not accepted:
        return Resolution("not_found", ranked[0] if ranked else None)
    accepted.sort(key=key)
    best = accepted[0]
    # Everything that scores nearly as well counts, accepted or not: a runner-up the
    # stricter bar rejected still shows the passage is not unique.
    tied = [
        c
        for c in sorted(found, key=key)
        if (exact_only or c.score + _FLOOR >= MIN_SCORE)
        and best.score - c.score <= TIE_MARGIN + _FLOOR
    ]
    if len(tied) > 1:
        # The one paragraph that still has the anchor's own fingerprint is where it came from.
        own = [c for c in tied if c.fp == anchor.fingerprint]
        if len(own) == 1 and own[0] in accepted:
            return Resolution("found", own[0])
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
            start, end, _ = window
            lo, hi = start, end
            while lo > 0 and (text[lo - 1].isalnum() or _is_mark(text[lo - 1])):
                lo -= 1
            while hi < len(text) and (text[hi].isalnum() or _is_mark(text[hi])):
                hi += 1
            if "\x00" in text[lo:hi]:
                lo, hi = start, end
            lo, hi = _refine(quote, text, lo, hi)
            quote_score = _quote_similarity(quote, text[lo:hi])
            out.append(_candidate(index, anchor, para, lo, hi, quote_score, "fuzzy"))
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
