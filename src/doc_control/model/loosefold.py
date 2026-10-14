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
    "first_hits",
    "fold",
    "fold_phrase",
    "hits",
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


# Sized so every paragraph of a large document stays cached at all three folded levels
# (``exact`` is searched on the raw text): a search folds each text once per level.
@functools.lru_cache(maxsize=16384)
def fold(text: str, level: Level) -> Folded:
    """``text`` folded at ``level``, with the original start and end of each character."""
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
    bare = "".join(ch for ch in phrase if unicodedata.category(ch) != "Cf")
    widen_left = bool(bare) and unicodedata.category(bare[0])[0] == "P"
    widen_right = bool(bare) and unicodedata.category(bare[-1])[0] == "P"
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
                spans = _exact_spans(text, needle)
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


def _exact_spans(text: str, needle: str) -> Iterator[tuple[int, int]]:
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
