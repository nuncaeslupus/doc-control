"""Loose folding of text for phrase matching, with an offset map back to the original.

A phrase and a paragraph are folded to a comparison string at one of four levels, each
folding everything the one before it folds:

``exact``
    No folding: every character stands for itself.
``case``
    Format characters (category Cf: soft hyphen, zero-width characters, bidi marks, a
    superset of :data:`doc_control.model.normalized.IGNORABLE`) are dropped, then NFC,
    ``casefold`` and whitespace runs to one space.
``default``
    ``case``, plus NFD with the combining marks (category M) dropped, and every
    punctuation character (category P) turned into a word boundary, a space. Symbols
    (category S) stay.
``loose``
    ``default``, plus every symbol dropped.

Whitespace, at every folded level: ``\\r`` (a paragraph mark) is a hard separator. It is
never turned into a space, collapsed with one or trimmed, and a space next to it is
dropped. Any other whitespace becomes one space and runs collapse. Only spaces are
trimmed from the ends.

Offsets: each folded character records the original range ``[start, end)`` of the cluster
it comes from (a base character with its combining marks, cut as
:mod:`doc_control.model.normalized` cuts them), without the dropped characters around it.
All the characters a cluster folds to share its range (``ß`` folds to ``ss``), and a
collapsed run of spaces takes the range from its first to its last character.
:func:`to_original` maps folded positions back to a span of whole clusters.

Levelled search: :func:`hits` finds a phrase at one level over a list of texts and tags
every hit with the strictest level it meets; :func:`ladder` is the order in which a
single-target op tries the levels.
"""

from __future__ import annotations

import bisect
import functools
import itertools
import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.normalized import _chunks

__all__ = [
    "LEVELS",
    "Folded",
    "Hit",
    "Level",
    "exact_spans",
    "first_hits",
    "fold",
    "fold_phrase",
    "fold_text",
    "fold_texts",
    "hits",
    "joined_hits",
    "ladder",
    "to_original",
]

Level = Literal["exact", "case", "default", "loose"]
LEVELS: tuple[Level, ...] = ("exact", "case", "default", "loose")

PARAGRAPH_MARK = "\r"


@dataclass(frozen=True)
class Folded:
    """The comparison string of ``source`` and the original range of every character."""

    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]
    source: str


# Only texts up to this size go through the caches: a paragraph, a phrase, a window of a
# few paragraphs. A whole document is folded uncached, or every version of it would stay.
CACHED_TEXT_MAX = 8192


def fold(text: str, level: Level) -> Folded:
    """``text`` folded at ``level``, with the original start and end of each character.

    Cached per ``(text, level)`` when ``text`` is at most :data:`CACHED_TEXT_MAX` long.
    """
    if len(text) <= CACHED_TEXT_MAX:
        return _fold_cached(text, level)
    return _fold(text, level)


def _fold(text: str, level: Level) -> Folded:
    if level not in LEVELS:
        raise ValueError(f"unknown fold level: {level!r}")
    if level == "exact":
        return Folded(text, tuple(range(len(text))), tuple(range(1, len(text) + 1)), text)
    kept = [i for i, ch in enumerate(text) if unicodedata.category(ch) != "Cf"]
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for chunk in _chunks(text, kept):
        start, end = chunk[0], chunk[-1] + 1
        for ch in _fold_cluster("".join(text[i] for i in chunk), level):
            if ch == PARAGRAPH_MARK:
                if chars and chars[-1] == " ":
                    chars.pop()
                    starts.pop()
                    ends.pop()
            elif ch == " ":
                if chars and chars[-1] == PARAGRAPH_MARK:
                    continue
                if chars and chars[-1] == " ":
                    ends[-1] = end
                    continue
            chars.append(ch)
            starts.append(start)
            ends.append(end)
    lo, hi = 0, len(chars)
    while lo < hi and chars[lo] == " ":
        lo += 1
    while hi > lo and chars[hi - 1] == " ":
        hi -= 1
    return Folded("".join(chars[lo:hi]), tuple(starts[lo:hi]), tuple(ends[lo:hi]), text)


# Sized so every paragraph of a large document stays cached at all three folded levels.
_fold_cached = functools.lru_cache(maxsize=16384)(_fold)

# Marks a character that may join the cluster before it (see ``normalized._chunks``).
_JOINER = "\uffff"


def _joins(ch: str) -> bool:
    """Whether ``ch`` may belong to the cluster before it: a mark, or a Hangul jamo.

    ``_chunks`` joins a character to the one before when it has a combining class or NFC
    composes the two. Every character with a combining class is a mark (M*), and the only
    other characters that compose as a second element are Hangul jamo.
    """
    cp = ord(ch)
    return (
        unicodedata.category(ch)[0] == "M"
        or unicodedata.combining(ch) != 0
        or 0x1100 <= cp <= 0x11FF
        or 0xD7B0 <= cp <= 0xD7FF
        or ch == _JOINER
    )


class _Table(dict[int, str]):
    """A ``str.translate`` table folding one character on its own, filled on demand."""

    def __init__(self, level: Level) -> None:
        super().__init__()
        self.level: Level = level

    def __missing__(self, cp: int) -> str:
        ch = chr(cp)
        if unicodedata.category(ch) == "Cf":
            out = ""
        elif _joins(ch):
            out = _JOINER
        else:
            out = _fold_cluster(ch, self.level)
        self[cp] = out
        return out


_TABLES: dict[Level, _Table] = {level: _Table(level) for level in LEVELS[1:]}
_SPACE_RUN = re.compile(" {2,}")


def fold_text(text: str, level: Level) -> str:
    """``fold(text, level).text``, without the offset map, and fast.

    Where every cluster of ``text`` is one character (no marks, no Hangul jamo), each
    character folds on its own, so a translation table and a pass over the spaces give
    the same string in C. Otherwise it is :func:`fold`'s. Cached like :func:`fold`.
    """
    if len(text) <= CACHED_TEXT_MAX:
        return _fold_text_cached(text, level)
    return _fold_text(text, level)


def fold_texts(texts: Sequence[str], level: Level) -> list[str]:
    """``[fold_text(text, level) for text in texts]``, the loop in C when each is cached."""
    if all(len(text) <= CACHED_TEXT_MAX for text in texts):
        return list(map(_fold_text_cached, texts, itertools.repeat(level)))
    return [fold_text(text, level) for text in texts]


def _fold_text(text: str, level: Level) -> str:
    if level == "exact":
        return text
    bulk = _bulk_fold(text, level)
    if bulk is not None:
        return bulk
    out = text.translate(_TABLES[level])
    if _JOINER in out:
        return _fold(text, level).text
    out = _SPACE_RUN.sub(" ", out)
    if PARAGRAPH_MARK in out:
        out = out.replace(" " + PARAGRAPH_MARK, PARAGRAPH_MARK).replace(
            PARAGRAPH_MARK + " ", PARAGRAPH_MARK
        )
    return out.strip(" ")


# Placeholders for what a character folds to when that is not one Latin-1 character: C1
# controls, which fold to themselves (U+0085 is whitespace, so it is left out).
_PLACEHOLDERS = tuple(chr(cp) for cp in range(0x80, 0xA0) if cp != 0x85)
_WIDE = re.compile("[^\x00-\xff]")
# More distinct characters than this outside Latin-1 and the translation table is faster.
_BULK_MAX_WIDE = 48


class _Bytes:
    """The Latin-1 part of one level's fold as a ``bytes.translate`` table."""

    def __init__(self, level: Level) -> None:
        table = _TABLES[level]
        self.table = bytearray(range(256))
        self.delete = bytearray()
        # Latin-1 characters that fold to anything but one Latin-1 character (``ß``).
        self.special: list[str] = []
        for cp in range(256):
            out = table[cp]
            if out == "":
                self.delete.append(cp)
            elif len(out) == 1 and ord(out) < 256 and out != _JOINER:
                self.table[cp] = ord(out)
            else:
                self.special.append(chr(cp))
        self.bytes_table = bytes(self.table)
        # What a character may be replaced by before the pass: Latin-1 that folds to itself.
        self.fixed = frozenset(
            chr(cp)
            for cp in range(256)
            if self.bytes_table[cp] == cp
            and chr(cp) not in self.special
            and chr(cp) not in _PLACEHOLDERS
        )


_BYTES: dict[Level, _Bytes] = {}


def _bulk_fold(text: str, level: Level) -> str | None:
    """``fold(text, level).text`` through ``bytes.translate``, or None where it cannot.

    The characters outside Latin-1 are few in most prose: each is replaced first by what
    it folds to (or by a placeholder restored at the end), so the rest is one pass over
    bytes. None when a character may join the one before it, or there are too many.
    """
    info = _BYTES.get(level)
    if info is None:
        info = _BYTES[level] = _Bytes(level)
    table = _TABLES[level]
    restore: list[tuple[str, str]] = []
    free = iter([p for p in _PLACEHOLDERS if p not in text])

    def replace(ch: str) -> bool:
        nonlocal text
        out = table[ord(ch)]
        if out == _JOINER:
            return False
        if all(c in info.fixed for c in out):
            text = text.replace(ch, out)
            return True
        holder = next(free, None)
        if holder is None or " " in out or PARAGRAPH_MARK in out:
            return False
        text = text.replace(ch, holder)
        restore.append((holder, out))
        return True

    if not all(replace(ch) for ch in info.special if ch in text):
        return None
    # The first few characters outside Latin-1 are found by encoding, which stops at
    # each; past those, one scan of the rest finds them all.
    seen = 0
    while True:
        try:
            data = text.encode("latin-1")
            break
        except UnicodeEncodeError as err:
            wide = [text[err.start]] if seen < 4 else list(set(_WIDE.findall(text, err.start)))
            seen += len(wide)
            if seen > _BULK_MAX_WIDE or not all(replace(ch) for ch in wide):
                return None
    data = _SPACES.sub(b" ", data.translate(info.bytes_table, info.delete))
    out = data.decode("latin-1")
    for holder, folded in restore:
        out = out.replace(holder, folded)
    # No space is kept beside a paragraph mark, nor at either end.
    if PARAGRAPH_MARK in out:
        out = out.replace(" " + PARAGRAPH_MARK, PARAGRAPH_MARK).replace(
            PARAGRAPH_MARK + " ", PARAGRAPH_MARK
        )
    return out.strip(" ")


# Two spaces or more; spelt so ``re`` scans for the literal pair (``" {2,}"`` is 3x slower).
_SPACES = re.compile(rb"  [ ]*")


_fold_text_cached = functools.lru_cache(maxsize=1 << 16)(_fold_text)


def _fold_cluster(cluster: str, level: Level) -> str:
    """The folded characters of one cluster; whitespace and punctuation as ``" "``."""
    out: list[str] = []
    composed = unicodedata.normalize("NFC", cluster).casefold()
    # casefold can make marks (``İ``), so the mark-dropping levels decompose after it.
    text = unicodedata.normalize("NFC" if level == "case" else "NFD", composed)
    for ch in text:
        if ch == PARAGRAPH_MARK:
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
        elif level == "case":
            out.append(ch)
        else:
            cat = unicodedata.category(ch)
            if cat[0] == "P":
                out.append(" ")
            elif cat[0] != "M" and not (level == "loose" and cat[0] == "S"):
                out.append(ch)
    return "".join(out)


def fold_phrase(phrase: str, level: Level) -> Folded:
    """:func:`fold` for an agent's phrase, refused when nothing is left of it.

    A phrase made only of dropped characters (``--`` at ``default``, ``$`` at
    ``loose``) would match every paragraph, so it is matched at ``exact`` only.
    """
    folded = fold(phrase, level)
    if not folded.text:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"phrase {phrase!r} is empty after folding at level {level!r}; "
            "match it with match_level 'exact'",
        )
    return folded


def to_original(
    folded: Folded, i: int, j: int, *, widen_left: bool = False, widen_right: bool = False
) -> tuple[int, int]:
    """Original ``(start, end)`` of folded characters ``i..j`` (``j`` exclusive).

    The span runs from the start of character ``i`` to the end of character ``j - 1``, so
    it holds whole clusters. With ``widen_left`` it also takes the punctuation touching
    its start, and with ``widen_right`` the punctuation touching its end: for a phrase
    that began, or ended, with punctuation of its own. Format characters between the
    punctuation are crossed; whitespace and anything else stops the widening.
    """
    if not 0 <= i < j <= len(folded.text):
        raise IndexError(f"folded range {i}..{j} outside 0..{len(folded.text)}")
    start, end = folded.starts[i], folded.ends[j - 1]
    source = folded.source
    if widen_left:
        pos = start
        while pos > 0:
            cat = unicodedata.category(source[pos - 1])
            if cat[0] == "P":
                start = pos - 1
            elif cat != "Cf":
                break
            pos -= 1
    if widen_right:
        pos = end
        while pos < len(source):
            cat = unicodedata.category(source[pos])
            if cat[0] == "P":
                end = pos + 1
            elif cat != "Cf":
                break
            pos += 1
    return start, end


@dataclass(frozen=True)
class Hit:
    """One hit: ``texts[index][start:end]``, matched at ``level`` (its strictest)."""

    index: int
    start: int
    end: int
    level: Level


def ladder(requested: Level, *, retry: bool) -> tuple[Level, ...]:
    """The levels to try, strictest first; each runs only when those before found nothing.

    ``exact`` never retries. With ``retry`` (the single-target ops) any other request walks
    every level from ``exact`` to ``loose``, so a stricter match always wins. Without it
    (the multi-hit ops) only the requested level runs.
    """
    if requested not in LEVELS:
        raise ValueError(f"unknown fold level: {requested!r}")
    if requested == "exact":
        return ("exact",)
    return LEVELS if retry else (requested,)


def hits(texts: Sequence[str], phrase: str, level: Level) -> list[Hit]:
    """Every hit of ``phrase`` in ``texts`` at ``level`` or stricter, in text order.

    Each level from ``exact`` up to ``level`` is searched in turn. A hit is kept with the
    strictest level that finds it: a looser hit overlapping a stricter one in the same
    text is dropped. Hits at one level do not overlap each other, after widening (left to
    right, the first wins). At ``default`` and ``loose`` a hit's span takes the
    punctuation touching it on a side where the phrase itself has punctuation. A hit
    starts and ends on cluster boundaries: it never splits a multi-character fold
    (``sa`` does not match ``ßa``) or a base from its combining marks, even at ``exact``.
    The phrase is matched as given (Word codes are the caller's). A phrase that folds to
    nothing at ``level`` is refused (:func:`fold_phrase`).
    """
    if level not in LEVELS:
        raise ValueError(f"unknown fold level: {level!r}")
    fold_phrase(phrase, level)
    out: list[Hit] = []
    for _, found in _levelled(texts, phrase, LEVELS.index(level) + 1):
        out.extend(found)
    out.sort(key=lambda h: (h.index, h.start))
    return out


def first_hits(texts: Sequence[str], phrase: str, requested: Level, *, retry: bool) -> list[Hit]:
    """The hits of the first rung of ``ladder(requested, retry=retry)`` that has any.

    The same as calling :func:`hits` at each rung in turn and stopping at the first that
    finds something, but every level is searched only once.
    """
    rungs = ladder(requested, retry=retry)
    if len(rungs) == 1:
        return hits(texts, phrase, rungs[0])
    for current, found in _levelled(texts, phrase, len(LEVELS)):
        fold_phrase(phrase, current)
        if found:
            return sorted(found, key=lambda h: (h.index, h.start))
    return []


def _levelled(texts: Sequence[str], phrase: str, count: int) -> Iterator[tuple[Level, list[Hit]]]:
    """The new hits of each of the first ``count`` levels, strictest first."""
    widen_left, widen_right = _widens(phrase)
    # Per text, the spans kept at stricter levels: sorted by start and disjoint.
    kept: dict[int, list[tuple[int, int]]] = {}
    for current in LEVELS[:count]:
        needle = phrase if current == "exact" else fold(phrase, current).text
        if not needle:
            yield current, []
            continue
        widen = current in ("default", "loose")
        found: list[Hit] = []
        for index, text in enumerate(texts):
            if current == "exact":
                spans = exact_spans(text, needle)
            else:
                spans = _folded_spans(
                    fold(text, current), needle, widen and widen_left, widen and widen_right
                )
            prior = kept.get(index, [])
            new: list[tuple[int, int]] = []
            for start, end in spans:
                if new and start < new[-1][1]:
                    continue
                if prior:
                    k = bisect.bisect_left(prior, (end,))
                    if k and prior[k - 1][1] > start:
                        continue
                new.append((start, end))
                found.append(Hit(index, start, end, current))
            if new:
                kept[index] = sorted(prior + new)
        yield current, found


def exact_spans(text: str, needle: str) -> Iterator[tuple[int, int]]:
    """Verbatim matches of ``needle`` that do not split a base from its marks."""
    pos = text.find(needle)
    while pos >= 0:
        end = pos + len(needle)
        if unicodedata.category(text[pos])[0] != "M" and (
            end == len(text) or unicodedata.category(text[end])[0] != "M"
        ):
            yield pos, end
            pos = text.find(needle, end)
        else:
            pos = text.find(needle, pos + 1)


def _folded_spans(
    folded: Folded, needle: str, widen_left: bool, widen_right: bool
) -> Iterator[tuple[int, int]]:
    """Original spans of the matches of ``needle`` that start and end between clusters."""
    text, starts = folded.text, folded.starts
    pos = text.find(needle)
    while pos >= 0:
        end = pos + len(needle)
        if (pos == 0 or starts[pos] != starts[pos - 1]) and (
            end == len(text) or starts[end] != starts[end - 1]
        ):
            yield to_original(folded, pos, end, widen_left=widen_left, widen_right=widen_right)
            pos = text.find(needle, end)
        else:
            pos = text.find(needle, pos + 1)


def _widens(phrase: str) -> tuple[bool, bool]:
    """Whether a hit of ``phrase`` takes the punctuation touching its start, and its end."""
    bare = "".join(ch for ch in phrase if unicodedata.category(ch) != "Cf")
    widen_left = bool(bare) and unicodedata.category(bare[0])[0] == "P"
    widen_right = bool(bare) and unicodedata.category(bare[-1])[0] == "P"
    return widen_left, widen_right


def joined_hits(texts: Sequence[str], phrase: str, level: Level, *, limit: int) -> list[Hit]:
    """The first ``limit`` hits of ``phrase`` at folded ``level`` in the joined texts.

    The texts are joined as ``"\\r".join(texts) + "\\r"`` (a paragraph and its mark), and
    every hit is ``Hit(0, start, end, level)`` in that string: the same spans as
    ``hits([joined], phrase, level)`` keeps at ``level``, ignoring the stricter levels.
    The joined text is folded in one uncached pass where :func:`_bulk_fold` can, else
    paragraph by paragraph with :func:`fold_text`, cached; either way no document-sized
    text is cached. Only the paragraphs a folded match touches are folded with their
    offset map, to find the original span.
    """
    if level == "exact":
        raise ValueError("joined_hits searches a folded level")
    needle = fold_phrase(phrase, level).text
    widen = level in ("default", "loose")
    widen_left, widen_right = _widens(phrase) if widen else (False, False)
    joined = PARAGRAPH_MARK.join(texts) + PARAGRAPH_MARK
    # One pass over the whole text, never cached, where no paragraph holds a mark itself.
    bulk = _bulk_fold(joined, level) if joined.count(PARAGRAPH_MARK) == len(texts) else None
    folded = (
        bulk
        if bulk is not None
        else PARAGRAPH_MARK.join([fold_text(text, level) for text in texts]) + PARAGRAPH_MARK
    )
    size = len(needle)
    out: list[Hit] = []
    pos = folded.find(needle)
    if pos < 0:
        return out
    # The folded start of each paragraph, and its original start in the joined text.
    pieces = folded.split(PARAGRAPH_MARK)[:-1]  # a mark ends each paragraph's fold
    fstarts = list(itertools.accumulate((len(p) + 1 for p in pieces), initial=0))
    starts = list(itertools.accumulate((len(t) + 1 for t in texts), initial=0))
    # A hit ending on a mark widens into the paragraph after it.
    after = 1 if widen_right and needle.endswith(PARAGRAPH_MARK) else 0
    while pos >= 0 and len(out) < limit:
        first = bisect.bisect_right(fstarts, pos) - 1
        last = bisect.bisect_right(fstarts, pos + size - 1) - 1
        # A later match that starts in these paragraphs may run on past them.
        while True:
            later = folded.rfind(needle, pos + 1, fstarts[last + 1] - 1 + size)
            if later < 0:
                break
            ends_in = bisect.bisect_right(fstarts, later + size - 1) - 1
            if ends_in <= last:
                break
            pos, last = later, ends_in
        window = PARAGRAPH_MARK.join(texts[first : last + 1 + after]) + PARAGRAPH_MARK
        base = starts[first]
        for start, end in _folded_spans(fold(window, level), needle, widen_left, widen_right):
            if base + start >= starts[last + 1]:
                break  # in the paragraph after, which is there for its punctuation only
            if out and base + start < out[-1].end:
                continue
            out.append(Hit(0, base + start, base + end, level))
        pos = folded.find(needle, fstarts[last + 1])
    return out[:limit]
