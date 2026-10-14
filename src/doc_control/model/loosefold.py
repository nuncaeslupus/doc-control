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

import functools
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.normalized import _chunks

__all__ = [
    "LEVELS",
    "Folded",
    "Hit",
    "Level",
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


@functools.lru_cache(maxsize=4096)
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
    text is dropped. Hits at one level do not overlap each other. At ``default`` and
    ``loose`` a hit's span takes the punctuation touching it on a side where the phrase
    itself has punctuation. The phrase is matched as given (Word codes are the caller's).
    A phrase that folds to nothing at ``level`` is refused (:func:`fold_phrase`).
    """
    if level not in LEVELS:
        raise ValueError(f"unknown fold level: {level!r}")
    fold_phrase(phrase, level)
    bare = "".join(ch for ch in phrase if unicodedata.category(ch) != "Cf")
    widen_left = bool(bare) and unicodedata.category(bare[0])[0] == "P"
    widen_right = bool(bare) and unicodedata.category(bare[-1])[0] == "P"
    kept: list[Hit] = []
    for current in LEVELS[: LEVELS.index(level) + 1]:
        needle = fold(phrase, current).text
        if not needle:
            continue
        widen = current in ("default", "loose")
        found: list[Hit] = []
        for index, text in enumerate(texts):
            folded = fold(text, current)
            pos = folded.text.find(needle)
            while pos >= 0:
                start, end = to_original(
                    folded,
                    pos,
                    pos + len(needle),
                    widen_left=widen and widen_left,
                    widen_right=widen and widen_right,
                )
                if not any(h.index == index and h.start < end and start < h.end for h in kept):
                    found.append(Hit(index, start, end, current))
                pos = folded.text.find(needle, pos + len(needle))
        kept.extend(found)
    kept.sort(key=lambda h: (h.index, h.start))
    return kept
