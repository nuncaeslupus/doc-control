"""`locate`: where a paragraph is (chapter, scene, estimated page) and how to find it again.

For each paragraph the result carries

* ``chapter`` (id, title, number, kind) and ``scene`` (id, ordinal within its chapter), from
  :mod:`doc_control.model.structure` (same ``overrides`` and ``profile`` as `segment`, so the
  ids agree); ``null`` before the first chapter or scene;
* ``part`` (id, title, number) when the paragraph lies in a part (see `segment` unit
  ``parts``), else ``null``;
* ``page`` and ``page_label`` from :mod:`doc_control.model.pages`: always an estimate;
* a *locator phrase*: the shortest ``head … tail`` made of the paragraph's first N and last M
  words that no other paragraph of the document also has.

Locator phrase
    Words are the paragraph text split on whitespace (punctuation stays on the word). Two
    words are the same when they are equal ignoring case (accents count, as in Word). The
    phrase keeps the document's own characters (no-break spaces stay). It is a citation for a
    report, not a search string: each half usually occurs elsewhere (use ``find_phrase``).
    A paragraph *matches* ``(head, tail)`` when it has at least N + M words,
    starts with the head words and ends with the tail words. The search tries N + M = 2, 3, ...
    (N and M at least 1, at most :data:`MAX_SIDE` each, larger N first among equals) and takes
    the first pair only this paragraph matches. When N + M reaches the paragraph's word count the
    phrase is the whole text (``complete: true``) and matches by equality. The phrase is
    ``null`` with a ``reason``: ``empty`` (no words), ``duplicate`` (another paragraph has the
    same words, ignoring case) or ``not_unique_within_limit`` (long, and every head/tail of up to
    :data:`MAX_SIDE` words is shared).

Find phrase
    ``find_phrase`` is what to type in Word's Find: the shortest run of consecutive words (at
    most :data:`MAX_FIND_WORDS`; a run starting at the paragraph's first word wins over one
    inside it) whose exact text occurs once in the whole document under Word's Find rules:
    case-insensitive, accent-sensitive, the document's own characters (no-break spaces stay,
    whitespace is not collapsed), occurrences counted in the joined text (paragraph marks
    included, overlaps counted). A line break inside a paragraph ends a phrase. Text outside
    the main story (footnotes, headers) is not searched. When no run inside the paragraph is
    unique (a repeated paragraph included), the shortest one across a paragraph mark is
    taken (``find_phrase_across``: ``previous``, ``next`` or ``both``; the mark is ``\\r`` in
    ``find_phrase``), e.g. the last
    words of the paragraph before, the mark, and ``-I-``. Only plain paragraph marks are
    spanned (not ones in table cells or ending a section), and uniqueness counts the mark
    Word puts after the last paragraph too. ``find_phrase_word`` is the same
    phrase as typed in Word's Find box (``^p`` mark, ``^t`` tab, ``^s`` no-break space,
    ``^~`` no-break hyphen, ``^-`` soft hyphen, ``^^`` caret), None past 255 characters.
    When none is unique ``find_phrase_unique`` is false with ``find_phrase_reason``
    (``empty``, ``duplicate`` or ``none_within_25_words``), and a non-empty paragraph gets
    ``context``: the find phrase (and ``after_word``) of the nearest earlier paragraph that
    has one, the paragraph it is in, and how many paragraphs after it this one comes.

Cost: the indexes (prefix and suffix word tuples, up to ``MAX_SIDE`` long), the structure and
the page estimates are built once per call, then each paragraph is a handful of set
lookups. Whole-document mode is paged and each response builds only its own rows. An
``at`` of at most ``LAZY_MAX`` paragraphs skips the indexes (unless an open document
already holds them): :class:`LazyIndexes` answers those paragraphs alone, with the same
results.
"""

from __future__ import annotations

import bisect
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, TypeVar, overload

from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.model.pages import estimate_pages
from doc_control.model.structure import detect_in
from doc_control.model.wordfind import word_find
from doc_control.ops.paging import PAGING_PROPERTIES, paged
from doc_control.ops.segment import (
    OVERRIDES_SCHEMA,
    PROFILE_SCHEMA,
    overrides_of,
    profile_of,
)
from doc_control.registry import operation

MAX_SIDE = 12
SEPARATOR = " … "
_TITLE = 160
MAX_FIND_WORDS = 25
LONG_WORD = 24  # longer words are kept out of the affix tables (see FindIndex)
LAZY_MAX = 20  # at most this many paragraphs asked: answer them without the indexes
LAZY_BUDGET = 0.3  # seconds of lazy answers per document before building the indexes
_WORD = re.compile(r"\S+")
_BREAK = re.compile(r"[\n\r\x0b\x0c]")
_A = TypeVar("_A")


@dataclass(frozen=True)
class Locator:
    phrase: str | None
    head: int = 0
    tail: int = 0
    complete: bool = False
    reason: str | None = None


def word_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of each whitespace-separated word of ``text``, characters untouched."""
    return [(m.start(), m.end()) for m in _WORD.finditer(text)]


class LocatorIndex:
    """Word prefixes and suffixes of every paragraph, to answer 'who else starts/ends so?'."""

    def __init__(self, texts: list[str], spans: list[list[tuple[int, int]]] | None = None) -> None:
        self.texts = texts
        self.spans = spans if spans is not None else [word_spans(t) for t in texts]
        self.keys = [
            tuple(t[s:e].casefold() for s, e in sp) for t, sp in zip(texts, self.spans, strict=True)
        ]
        self.prefixes: dict[tuple[str, ...], set[int]] = {}
        self.suffixes: dict[tuple[str, ...], set[int]] = {}
        self.whole: dict[tuple[str, ...], int] = {}
        for i, key in enumerate(self.keys):
            if not key:
                continue
            self.whole[key] = self.whole.get(key, 0) + 1
            for n in range(1, min(len(key), MAX_SIDE) + 1):
                self.prefixes.setdefault(key[:n], set()).add(i)
                self.suffixes.setdefault(key[-n:], set()).add(i)

    def matches(self, i: int, head: int, tail: int) -> int:
        """Paragraphs with at least ``head + tail`` words that start and end like paragraph i."""
        key = self.keys[i]
        starts = self.prefixes[key[:head]]
        ends = self.suffixes[key[-tail:]]
        small, big = (starts, ends) if len(starts) <= len(ends) else (ends, starts)
        need = head + tail
        return sum(1 for j in small if j in big and len(self.keys[j]) >= need)

    def locator(self, i: int) -> Locator:
        key = self.keys[i]
        count = len(key)
        if count == 0:
            return Locator(None, reason="empty")
        if self.whole[key] > 1:
            return Locator(None, reason="duplicate")
        text, spans = self.texts[i], self.spans[i]
        for total in range(2, min(count, 2 * MAX_SIDE + 1)):
            for head in range(min(total - 1, MAX_SIDE), 0, -1):
                tail = total - head
                if tail > MAX_SIDE:
                    break
                if self.matches(i, head, tail) == 1:
                    first = text[spans[0][0] : spans[head - 1][1]]
                    last = text[spans[-tail][0] : spans[-1][1]]
                    return Locator(first + SEPARATOR + last, head, tail)
        if count <= 2 * MAX_SIDE:
            return Locator(text[spans[0][0] : spans[-1][1]], count, 0, complete=True)
        return Locator(None, reason="not_unique_within_limit")


@dataclass(frozen=True)
class FindPhrase:
    """A run of words that occurs once in the document (``text`` None when none does)."""

    text: str | None
    words: int = 0
    start: int = 0  # character offset in the paragraph
    reason: str | None = None
    across: str | None = None  # "previous", "next" or "both": the phrase spans a mark


class MarkIndex:
    """Counts phrases that hold paragraph marks without scanning the text.

    ``X\\rY`` (``X`` and ``Y`` without marks) occurs only where a paragraph ending in ``X``
    is followed by one starting with ``Y`` (the last paragraph ends in a mark too), and
    ``\\rT\\r`` only where a paragraph after the first is exactly ``T``. Paragraphs sorted
    by text and by reversed text give each side as a range; the smaller side is checked
    against the other, stopping at a second occurrence.
    """

    def __init__(self, texts: Sequence[str]) -> None:
        self.folded = [t.casefold() for t in texts]
        self._by_start = sorted(range(len(texts)), key=self.folded.__getitem__)
        self._starts = [self.folded[i] for i in self._by_start]
        backwards = [f[::-1] for f in self.folded]
        self._by_end = sorted(range(len(texts)), key=backwards.__getitem__)
        self._ends = [backwards[i] for i in self._by_end]
        self._pairs = Counter(pairwise(self.folded))

    def pair_repeats(self, j: int) -> bool:
        """Paragraphs j and j + 1 recur as a pair elsewhere: every phrase across the mark
        between them recurs too (in a run of identical paragraphs, for one)."""
        return self._pairs[self.folded[j], self.folded[j + 1]] > 1

    @staticmethod
    def _span(keys: list[str], prefix: str) -> tuple[int, int]:
        lo = bisect.bisect_left(keys, prefix)
        hi = bisect.bisect_left(keys, prefix + "\U0010ffff", lo)
        while hi < len(keys) and keys[hi].startswith(prefix):  # keys holding U+10FFFF
            hi += 1
        return lo, hi

    def once(self, phrase: str) -> bool:
        """``phrase`` (folded, one mark, or one paragraph between two) occurs exactly once."""
        folded, n = self.folded, len(self.folded)
        parts = phrase.split("\r")
        if len(parts) == 3:
            lo, hi = self._span(self._starts, parts[1])
            hits = [q for q in self._by_start[lo:hi] if q >= 1 and folded[q] == parts[1]]
            return len(hits) == 1
        x, y = parts
        elo, ehi = self._span(self._ends, x[::-1])
        slo, shi = self._span(self._starts, y)
        count = 0
        if ehi - elo <= shi - slo:
            for q in self._by_end[elo:ehi]:
                if folded[q + 1].startswith(y) if q + 1 < n else not y:
                    count += 1
                    if count > 1:
                        return False
        else:
            for p in self._by_start[slo:shi]:
                if p >= 1 and folded[p - 1].endswith(x):
                    count += 1
                    if count > 1:
                        return False
            if not y and folded[n - 1].endswith(x):  # the last paragraph's own mark
                count += 1
        return count == 1


def across_marks(
    texts: Sequence[str],
    spans: Callable[[int], list[tuple[int, int]]],
    marks: MarkIndex,
    i: int,
    crossable: Callable[[int], bool] = lambda j: True,
) -> FindPhrase | None:
    """The shortest phrase spanning a paragraph mark next to paragraph ``i`` that Word's Find
    meets once: words ending the previous paragraph (or none), the mark, the paragraph's
    first words; or its last words, the mark, words opening the next one (or none); or the
    whole paragraph between both marks. Fewest words first, then fewest marks, then the
    paragraph's start first. None when no such phrase fits in Word's Find box."""
    text, own = texts[i], spans(i)
    if not own:
        return None
    n = len(own)
    runs = FindIndex._run_ends(text, own)
    found: list[tuple[int, int, int, str, int, str]] = []

    def neighbour_words(j: int, tail: bool) -> list[str]:
        """Text pieces of paragraph ``j`` next to its mark, by word count (0 words first)."""
        other, sp = texts[j], spans(j)
        pieces = [""]
        for k in range(1, min(len(sp), MAX_FIND_WORDS) + 1):
            piece = other[sp[-k][0] :] if tail else other[: sp[k - 1][1]]
            if _BREAK.search(piece):
                break
            pieces.append(piece)
        return pieces

    # Only a plain paragraph mark is ^p: not one ending a table cell or a section.
    before = (
        neighbour_words(i - 1, tail=True)
        if i > 0 and crossable(i - 1) and not marks.pair_repeats(i - 1)
        else []
    )
    after = (
        neighbour_words(i + 1, tail=False)
        if i + 1 < len(texts) and crossable(i) and not marks.pair_repeats(i)
        else []
    )
    for k in range(1, min(n, MAX_FIND_WORDS) + 1):
        head = text[: own[k - 1][1]]
        if before and runs[0] >= k - 1 and not _BREAK.search(head):
            for j, piece in enumerate(before[: MAX_FIND_WORDS - k + 1]):
                found.append((k + j, 1, 0, piece + "\r" + head, 0, "previous"))
        tail = text[own[n - k][0] :]
        if after and not _BREAK.search(tail):
            for j, piece in enumerate(after[: MAX_FIND_WORDS - k + 1]):
                found.append((k + j, 1, 1, tail + "\r" + piece, own[n - k][0], "next"))
    if before and after and n <= MAX_FIND_WORDS and not _BREAK.search(text):
        found.append((n, 2, 2, "\r" + text + "\r", 0, "both"))
    for words, _marks, _order, phrase, start, side in sorted(found, key=lambda c: c[:3]):
        if word_find(phrase) is not None and marks.once(phrase.casefold()):
            return FindPhrase(phrase, words, start, across=side)
    return None


class FindIndex:
    """Shortest words that Word's Find would meet exactly once in the whole document.

    Find semantics: case-insensitive (``casefold``), accent-sensitive, on the document's own
    characters (a no-break space stays one; whitespace is not collapsed); paragraph marks are
    ``\\r`` in the searched text, and a line break inside a paragraph (or any ``\\n``, ``\\r``,
    vertical tab, form feed) ends a phrase. Occurrences may overlap.

    A word-aligned window that occurs twice cannot be unique, so counting windows of each
    length (one pass per length over integer tokens, dropped afterwards) rules candidates
    out cheaply; the survivors are confirmed with a substring search in the joined text.
    """

    def __init__(self, index: LocatorIndex) -> None:
        self.texts = index.texts
        self.spans = index.spans
        # Word ends the last paragraph with a mark too, so the joined text does.
        self.haystack = "\r".join(t.casefold() for t in self.texts) + "\r"
        # A one-word phrase has no whitespace, so it can only occur inside words: searching
        # the distinct words (once each) answers "elsewhere?" on a far shorter text.
        distinct = dict.fromkeys(k for keys in index.keys for k in keys)
        self.words = "\r".join(distinct)
        # A window of 2+ words that occurs once word-aligned can only recur unaligned if its
        # first word ends some longer word or its last word starts one (the words between
        # sit between whitespace, so they recur whole). Without either, the count is proof.
        # Affix tables are quadratic in word length: words past LONG_WORD (URLs, unspaced
        # scripts) stay out of them and are checked one by one instead.
        short = [w for w in distinct if len(w) <= LONG_WORD]
        self.long_words = [w for w in distinct if len(w) > LONG_WORD]
        self.suffixes = {w[n:] for w in short for n in range(1, len(w))}
        self.prefixes = {w[:n] for w in short for n in range(1, len(w))}
        # Any occurrence of a 2+ word phrase holds a word starting with its last word, so
        # only the paragraphs holding such words need the substring search.
        self.folded = [t.casefold() for t in self.texts]
        self._paras_of: dict[str, set[int]] = {}
        for i, keys in enumerate(index.keys):
            for word in keys:
                self._paras_of.setdefault(word, set()).add(i)
        self._starting: dict[str, list[str]] = {}
        self._ending: dict[str, list[str]] = {}
        for w in short:
            for n in range(1, len(w) + 1):
                self._starting.setdefault(w[:n], []).append(w)
                self._ending.setdefault(w[-n:], []).append(w)
        self._tokens = self._tokenise()
        self._runs = [self._run_ends(t, sp) for t, sp in zip(self.texts, self.spans, strict=True)]
        self._windows = sum(len(r) for r in index.spans)
        self._copies = Counter(t.casefold() for t in self.texts)
        self._keys = index.keys
        self.found: list[FindPhrase] = []
        self._solve()

    def _tokenise(self) -> list[list[int]]:
        """Per paragraph ``word, gap, word, ...`` as ints (words folded, gaps as written)."""
        ids: dict[str, int] = {}
        out: list[list[int]] = []
        for text, spans in zip(self.texts, self.spans, strict=True):
            toks: list[int] = []
            for k, (begin, end) in enumerate(spans):
                if k:
                    toks.append(ids.setdefault(text[spans[k - 1][1] : begin], len(ids)))
                toks.append(ids.setdefault(text[begin:end].casefold(), len(ids)))
            out.append(toks)
        return out

    @staticmethod
    def _run_ends(text: str, spans: list[tuple[int, int]]) -> list[int]:
        """Per word, the last word reachable from it without crossing a line or paragraph break."""
        ends = [0] * len(spans)
        for k in range(len(spans) - 1, -1, -1):
            joined = k + 1 < len(spans) and not _BREAK.search(text, spans[k][1], spans[k + 1][0])
            ends[k] = ends[k + 1] if joined else k
        return ends

    def _solve(self) -> None:
        count = len(self.texts)
        self.found = [FindPhrase(None, reason=f"none_within_{MAX_FIND_WORDS}_words")] * count
        open_: list[int] = []
        for i, key in enumerate(self._keys):
            if not key:
                self.found[i] = FindPhrase(None, reason="empty")
            elif self._copies[self.texts[i].casefold()] > 1:
                # Every window of a repeated paragraph occurs in its twin as well.
                self.found[i] = FindPhrase(None, reason="duplicate")
            else:
                open_.append(i)
        for length in range(1, MAX_FIND_WORDS + 1):
            if not open_:
                break
            seen: Counter[tuple[int, ...]] | None = None
            # A substring search costs about 300 window counts: with few paragraphs left,
            # skip the counting pass and confirm their windows directly.
            if sum(max(len(self._runs[i]) - length + 1, 0) for i in open_) * 300 > self._windows:
                seen = Counter()
                for toks, runs in zip(self._tokens, self._runs, strict=True):
                    for s in range(len(runs) - length + 1):
                        if runs[s] >= s + length - 1:
                            seen[tuple(toks[2 * s : 2 * (s + length) - 1])] += 1
            still: list[int] = []
            for i in open_:
                hit = self._pick(i, length, seen)
                if hit is None:
                    still.append(i)
                else:
                    self.found[i] = hit
            open_ = still

    def once(self, key: str) -> bool:
        """``key`` (folded, may hold paragraph marks) occurs exactly once in the document."""
        at = self.haystack.find(key)
        return at >= 0 and self.haystack.find(key, at + 1) < 0

    def _paras(self, words: list[str]) -> set[int]:
        out: set[int] = set()
        for word in words:
            out |= self._paras_of[word]
        return out

    def _occurrences(self, key: str, words: tuple[str, ...]) -> int:
        """Occurrences of ``key`` (the folded text of ``words``) in the document, up to 2.

        Any occurrence holds a word ending with the first word, one starting with the last,
        and the words between whole; the narrowest of those says which paragraphs to search.
        """
        ending = self._ending.get(words[0], []) + [
            w for w in self.long_words if w.endswith(words[0])
        ]
        starting = self._starting.get(words[-1], []) + [
            w for w in self.long_words if w.startswith(words[-1])
        ]
        options = [ending, starting]
        options += [[w] for w in words[1:-1]]
        narrowest = min(options, key=lambda ws: sum(len(self._paras_of[w]) for w in ws))
        found = 0
        paras = self._paras(narrowest)
        for j in paras:
            hay = self.folded[j]
            at = hay.find(key)
            while at >= 0:
                found += 1
                if found > 1:
                    return found
                at = hay.find(key, at + 1)
        return found

    def _pick(
        self, i: int, length: int, seen: Counter[tuple[int, ...]] | None
    ) -> FindPhrase | None:
        text, spans, runs = self.texts[i], self.spans[i], self._runs[i]
        starts = range(len(spans) - length + 1)  # the paragraph's start comes first
        for s in starts:
            if runs[s] < s + length - 1:
                continue
            begin, end = spans[s][0], spans[s + length - 1][1]
            if seen is not None and seen[tuple(self._tokens[i][2 * s : 2 * (s + length) - 1])] != 1:
                continue
            if seen is not None and length > 1:
                words = self._keys[i]
                first, last = words[s], words[s + length - 1]
                if (
                    first not in self.suffixes
                    and last not in self.prefixes
                    and not any(w.endswith(first) or w.startswith(last) for w in self.long_words)
                ):
                    return FindPhrase(text[begin:end], length, begin)
            key = text[begin:end].casefold()
            if length > 1:
                if self._occurrences(key, self._keys[i][s : s + length]) == 1:
                    return FindPhrase(text[begin:end], length, begin)
                continue
            hit = self.words.find(key)
            if hit >= 0 and self.words.find(key, hit + 1) < 0:
                return FindPhrase(text[begin:end], length, begin)
        return None


class LazyIndexes:
    """Locator and find phrases of a few paragraphs, without the whole-document indexes.

    Gives what :class:`LocatorIndex` and :class:`FindIndex` give for the same paragraph
    (the gate compares them on every paragraph of the corpus); each answer costs a scan of
    the paragraphs' edges and substring counts in the document's folded text, which beats
    building both indexes when a request asks about a handful of paragraphs.
    """

    def __init__(self, texts: list[str], crossable: Sequence[bool] | None = None) -> None:
        self.texts = texts
        self.crossable = crossable
        self.haystack = "\r".join(t.casefold() for t in texts) + "\r"  # see FindIndex
        self._copies: Counter[str] | None = None  # folded text -> paragraphs holding it
        self._edges: list[tuple[tuple[str, ...], tuple[str, ...], int]] | None = None
        self._locators: dict[int, Locator] = {}
        self._finds: dict[int, FindPhrase] = {}
        self.spent = 0.0  # seconds spent answering (see _Phrases)
        self._mark_index: MarkIndex | None = None

    def _keys(self, i: int) -> tuple[list[tuple[int, int]], tuple[str, ...]]:
        text = self.texts[i]
        spans = word_spans(text)
        return spans, tuple(text[s:e].casefold() for s, e in spans)

    def _edge_table(self) -> list[tuple[tuple[str, ...], tuple[str, ...], int]]:
        """Per paragraph: its first and last ``MAX_SIDE`` folded words and its word count."""
        if self._edges is None:
            edges = []
            for text in self.texts:
                words = text.casefold().split()
                edges.append((tuple(words[:MAX_SIDE]), tuple(words[-MAX_SIDE:]), len(words)))
            self._edges = edges
        return self._edges

    def _copies_of(self, i: int) -> int:
        if self._copies is None:
            self._copies = Counter(t.casefold() for t in self.texts)
        return self._copies[self.texts[i].casefold()]

    def locator(self, i: int) -> Locator:
        if i not in self._locators:
            self._locators[i] = self._locator(i)
        return self._locators[i]

    def _locator(self, i: int) -> Locator:
        spans, key = self._keys(i)
        count = len(key)
        if count == 0:
            return Locator(None, reason="empty")
        edges = self._edge_table()
        # Only paragraphs sharing the first and the last word can match any head/tail.
        alike = [
            j
            for j, (head, tail, n) in enumerate(edges)
            if n and head[0] == key[0] and tail[-1] == key[-1]
        ]
        if any(j != i and edges[j][2] == count and self._keys(j)[1] == key for j in alike):
            return Locator(None, reason="duplicate")
        text = self.texts[i]
        for total in range(2, min(count, 2 * MAX_SIDE + 1)):
            for head in range(min(total - 1, MAX_SIDE), 0, -1):
                tail = total - head
                if tail > MAX_SIDE:
                    break
                first, last = key[:head], key[-tail:]
                hits = sum(
                    1
                    for j in alike
                    if edges[j][2] >= total
                    and edges[j][0][:head] == first
                    and edges[j][1][-tail:] == last
                )
                if hits == 1:
                    return Locator(
                        text[spans[0][0] : spans[head - 1][1]]
                        + SEPARATOR
                        + text[spans[-tail][0] : spans[-1][1]],
                        head,
                        tail,
                    )
        if count <= 2 * MAX_SIDE:
            return Locator(text[spans[0][0] : spans[-1][1]], count, 0, complete=True)
        return Locator(None, reason="not_unique_within_limit")

    def find(self, i: int) -> FindPhrase:
        if i not in self._finds:
            self._finds[i] = self._find(i)
        return self._finds[i]

    def _marks(self) -> MarkIndex:
        if self._mark_index is None:
            self._mark_index = MarkIndex(self.texts)
        return self._mark_index

    def _once(self, key: str) -> bool:
        """``key`` occurs exactly once in the document (occurrences may overlap)."""
        at = self.haystack.find(key)
        return at >= 0 and self.haystack.find(key, at + 1) < 0

    def _find(self, i: int) -> FindPhrase:
        text = self.texts[i]
        spans, key = self._keys(i)
        if not key:
            return FindPhrase(None, reason="empty")
        marks = self.crossable
        crossable = (lambda j: marks[j]) if marks is not None else (lambda j: True)
        if self._copies_of(i) > 1:
            # Every run of a repeated paragraph recurs in its twin; only its marks differ.
            spanning = across_marks(
                self.texts, lambda j: self._keys(j)[0], self._marks(), i, crossable
            )
            return spanning or FindPhrase(None, reason="duplicate")
        runs = FindIndex._run_ends(text, spans)
        for length in range(1, MAX_FIND_WORDS + 1):
            for s in range(len(spans) - length + 1):
                if runs[s] < s + length - 1:
                    continue
                begin, end = spans[s][0], spans[s + length - 1][1]
                if self._once(text[begin:end].casefold()):
                    return FindPhrase(text[begin:end], length, begin)
        spanning = across_marks(self.texts, lambda j: self._keys(j)[0], self._marks(), i, crossable)
        return spanning or FindPhrase(None, reason=f"none_within_{MAX_FIND_WORDS}_words")


def _indexes(
    texts: list[str], crossable: Sequence[bool] | None = None
) -> tuple[LocatorIndex, list[FindPhrase]]:
    """The whole-document indexes; ``crossable[j]``: the mark after paragraph j is a plain
    paragraph mark a phrase may span (default: every one)."""
    locators = LocatorIndex(texts)
    finder = FindIndex(locators)
    found = finder.found
    marks: MarkIndex | None = None
    for i, phrase in enumerate(found):
        if phrase.text is None and phrase.reason != "empty":
            marks = marks or MarkIndex(texts)
            spanning = across_marks(
                texts,
                locators.spans.__getitem__,
                marks,
                i,
                (lambda j: crossable[j]) if crossable is not None else (lambda j: True),
            )
            found[i] = spanning or phrase
    return locators, found


def _plain_marks(doc: Document) -> list[bool]:
    """Per paragraph: its mark and the next paragraph's are plain paragraph marks (outside
    table cells, not ending a section), so a find phrase may span the mark between them."""
    paras = doc.index.paragraphs
    plain = [
        cell == -1 and b"sectPr" not in doc.paragraph_bytes(para)
        for cell, para in zip(doc.cells, paras, strict=True)
    ]
    return [plain[j] and (j + 1 >= len(plain) or plain[j + 1]) for j in range(len(plain))]


class _Phrases:
    """Lazy answers until they have cost ``LAZY_BUDGET`` seconds on this document, then the
    whole-document indexes (built once, same answers): a run of near-identical paragraphs
    makes each lazy answer scan the document again, where the indexes answer from tables."""

    def __init__(self, doc: Document, texts: list[str], crossable: Sequence[bool]) -> None:
        self.doc = doc
        self.texts = texts
        self.crossable = crossable
        self.lazy = doc.memo("locate-lazy", lambda: LazyIndexes(texts, crossable))

    def _indexes(self) -> tuple[LocatorIndex, list[FindPhrase]]:
        return self.doc.memo("locate-index", lambda: _indexes(self.texts, self.crossable))

    def _lazy(self, answer: Callable[[int], _A], i: int) -> _A:
        start = time.perf_counter()
        try:
            return answer(i)
        finally:
            self.lazy.spent += time.perf_counter() - start

    def locator(self, i: int) -> Locator:
        if self.lazy.spent < LAZY_BUDGET:
            return self._lazy(self.lazy.locator, i)
        return self._indexes()[0].locator(i)

    def find(self, i: int) -> FindPhrase:
        if self.lazy.spent < LAZY_BUDGET:
            return self._lazy(self.lazy.find, i)
        return self._indexes()[1][i]


class _Rows(Sequence[dict[str, Any]]):
    """Rows built only for the slice a page asks for; the indexes behind them are shared."""

    def __init__(self, numbers: list[int], build: Callable[[int], dict[str, Any]]) -> None:
        self._numbers = numbers
        self._build = build

    def __len__(self) -> int:
        return len(self._numbers)

    @overload
    def __getitem__(self, item: int) -> dict[str, Any]: ...
    @overload
    def __getitem__(self, item: slice) -> list[dict[str, Any]]: ...
    def __getitem__(self, item: int | slice) -> dict[str, Any] | list[dict[str, Any]]:
        if isinstance(item, slice):
            return [self._build(n) for n in self._numbers[item]]
        return self._build(self._numbers[item])


@operation(
    "locate",
    "read",
    "Chapter, scene, estimated page and the shortest unique first-and-last-words phrase of "
    "each paragraph, plus a phrase Word's Find meets once (one, a range, or the whole "
    "document, paged).",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            "at": {
                "type": "string",
                "minLength": 1,
                "description": "Paragraph address or range (offsets ignored), e.g. p142#a7b2a7b2; "
                "default the whole document (empty paragraphs left out).",
            },
            "overrides": OVERRIDES_SCHEMA,
            "profile": PROFILE_SCHEMA,
            "structure": {
                "type": "boolean",
                "description": "false: skip chapter, part and scene detection (they come back "
                "null), for callers that keep their own structure; faster on a single "
                "paragraph. Default true.",
            },
            **PAGING_PROPERTIES,
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def locate(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    index = doc.index
    count = len(index)
    texts = [index.paragraph(n).text for n in range(1, count + 1)]
    profile, overrides = profile_of(request), overrides_of(request)  # checked either way
    if request.get("structure", True):
        found = detect_in(doc, profile, overrides)
        chapters, parts, scenes = found.chapters, found.parts, found.scenes
    else:
        chapters, parts, scenes = (), (), ()
    pages = estimate_pages(doc)
    if "at" in request:
        resolved = index.resolve_range(request["at"])
        numbers = range(resolved.first.number, resolved.last.number + 1)
    else:
        numbers = range(1, count + 1)
    memo: dict[Any, Any] = doc.__dict__.get("_memo", {})
    crossable = doc.memo("locate-marks", lambda: _plain_marks(doc))
    if len(numbers) <= LAZY_MAX and "locate-index" not in memo:
        # A few paragraphs: answer them alone, without the whole-document indexes.
        phrases = _Phrases(doc, texts, crossable)
        locator_of: Callable[[int], Locator] = phrases.locator
        find_of: Callable[[int], FindPhrase] = phrases.find
    else:
        locators, finds = doc.memo("locate-index", lambda: _indexes(texts, crossable))
        locator_of = locators.locator
        find_of = finds.__getitem__

    chapter_at = [0] * (count + 2)  # 1-based paragraph -> 1-based chapter, 0 for none
    for k, chapter in enumerate(chapters, 1):
        for n in range(chapter.first, chapter.last + 1):
            chapter_at[n] = k
    part_at = [0] * (count + 2)
    for k, part in enumerate(parts, 1):
        for n in range(part.first, part.last + 1):
            part_at[n] = k
    scene_at = [0] * (count + 2)
    ordinal: dict[str | None, int] = {}
    ordinals = [0]
    for k, scene in enumerate(scenes, 1):
        ordinal[scene.chapter] = ordinal.get(scene.chapter, 0) + 1
        ordinals.append(ordinal[scene.chapter])
        for n in range(scene.first, scene.last + 1):
            scene_at[n] = k

    wanted = [n for n in numbers if "at" in request or texts[n - 1].strip()]

    def row(n: int) -> dict[str, Any]:
        page = pages.rows[n - 1]
        chapter = chapters[chapter_at[n] - 1] if chapter_at[n] else None
        scene = scenes[scene_at[n] - 1] if scene_at[n] else None
        part = parts[part_at[n] - 1] if part_at[n] else None
        loc = locator_of(n - 1)
        out: dict[str, Any] = {
            "at": str(index.paragraph(n).address),
            "chapter": None
            if chapter is None
            else {
                "id": chapter.id,
                "title": " ".join(index.paragraph(chapter.first).text.split())[:_TITLE],
                "number": chapter.number,
                "kind": chapter.kind,
            },
            "part": None
            if part is None
            else {
                "id": part.id,
                "title": " ".join(index.paragraph(part.first).text.split())[:_TITLE],
                "number": part.number,
            },
            "scene": None if scene is None else {"id": scene.id, "ordinal": ordinals[scene_at[n]]},
            "page": page.page,
            "page_label": page.label,
            "phrase": loc.phrase,
        }
        find = find_of(n - 1)
        out["find_phrase"] = find.text
        out["find_phrase_unique"] = find.text is not None
        if find.text is not None:
            out["find_phrase_words"] = find.words
            out["find_phrase_word"] = word_find(find.text)
            if find.across is not None:
                out["find_phrase_across"] = find.across
        else:
            out["find_phrase_reason"] = find.reason
            earlier = next((j for j in range(n - 2, -1, -1) if find_of(j).text is not None), None)
            if earlier is not None and find.reason != "empty":
                out["context"] = {
                    "after": find_of(earlier).text,
                    "after_word": word_find(find_of(earlier).text or ""),
                    "at": str(index.paragraph(earlier + 1).address),
                    "paragraphs_after": n - 1 - earlier,
                }
        if loc.phrase is None:
            out["reason"] = loc.reason
        else:
            out["head_words"] = loc.head
            out["tail_words"] = loc.tail
            if loc.complete:
                out["complete"] = True
        return out

    head: dict[str, Any] = {"estimated": True, "page_count": pages.pages, "basis": pages.basis}
    return paged("locate", doc, request, "paragraphs", _Rows(wanted, row), head)
