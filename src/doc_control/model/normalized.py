"""Normalised view of visible paragraph text, with a two-way offset map.

Offsets are code points of the visible text, as in :mod:`doc_control.model.index`; the
end of a range is exclusive. :func:`normalize` applies, in this order:

1. removal of soft hyphens (U+00AD) and zero-width characters (U+200B, U+200C, U+200D,
   U+2060, U+FEFF);
2. NFC;
3. collapse of every run of whitespace (``str.isspace``: space, tab, newline, no-break
   space U+00A0, U+2028...) into one space. Nothing is trimmed.

Every normalised character covers a half-open range of original characters (if nothing
is left, the only boundary maps to 0). A removed
character belongs to the character that follows it (the trailing ones to the last
character), a collapsed run to its single space, and the characters NFC combines (``e`` +
U+0301 -> ``e`` with acute) to the combined character.

``to_original[i]`` is the original offset of normalised boundary ``i`` (``0..len(text)``)
and ``to_normalized[o]`` the normalised offset of original boundary ``o``
(``0..len(original)``). So ``text[a:b]`` comes from ``original[to_original[a]:to_original[b]]``.
Boundaries that exist on both sides survive a round trip unchanged. A boundary with no
counterpart (inside a collapsed whitespace run, between a letter and the accent NFC folds
into it, or inside the two characters one original character expands to) maps to the
nearest boundary before it, so both maps are monotonic and
``to_original[to_normalized[to_original[i]]] == to_original[i]`` always holds.
"""

from __future__ import annotations

import bisect
import unicodedata
from dataclasses import dataclass

IGNORABLE = frozenset("­​‌‍⁠﻿")


@dataclass(frozen=True)
class NormalizedText:
    """The normalised text and the offset maps between it and the original."""

    text: str
    original_length: int
    to_original: tuple[int, ...]
    to_normalized: tuple[int, ...]

    def original_offset(self, offset: int) -> int:
        """Original offset of normalised boundary ``offset``."""
        if not 0 <= offset <= len(self.text):
            raise IndexError(f"normalised offset {offset} outside 0..{len(self.text)}")
        return self.to_original[offset]

    def normalized_offset(self, offset: int) -> int:
        """Normalised offset of original boundary ``offset``."""
        if not 0 <= offset <= self.original_length:
            raise IndexError(f"original offset {offset} outside 0..{self.original_length}")
        return self.to_normalized[offset]


def normalize(original: str) -> NormalizedText:
    """Normalise ``original`` and map offsets both ways."""
    kept = [i for i, ch in enumerate(original) if ch not in IGNORABLE]
    # Output characters as [start, end) original ranges. A character's range starts
    # where the previous one ended, so removed characters attach to the next one.
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    prev_end = 0
    chunks = _chunks(original, kept)
    for pos, chunk in enumerate(chunks):
        out = unicodedata.normalize("NFC", "".join(original[i] for i in chunk))
        # Removed characters at the very end belong to the last chunk.
        end = len(original) if pos == len(chunks) - 1 else chunk[-1] + 1
        for ch in out:
            if ch.isspace() and chars and chars[-1] == " ":
                ends[-1] = end  # collapse into the previous space
                continue
            chars.append(" " if ch.isspace() else ch)
            starts.append(prev_end)
            ends.append(end)
        prev_end = end

    # A chunk's characters all share its range, so an interior boundary sits at its start.
    to_original = (*starts, len(original) if chars else 0)
    to_normalized = tuple(bisect.bisect_right(ends, o) for o in range(len(original) + 1))
    return NormalizedText("".join(chars), len(original), to_original, to_normalized)


def _chunks(original: str, kept: list[int]) -> list[list[int]]:
    """Split the kept indexes where NFC cannot combine across the cut.

    A character with combining class 0 composes only with the character right before
    it, so a cut before one is safe when NFC of the pair equals the NFC of each part.
    """
    chunks: list[list[int]] = []
    for pos, i in enumerate(kept):
        ch = original[i]
        joins = False
        if chunks:
            prev = original[kept[pos - 1]]
            # Nothing composes with whitespace, and a collapsed space must stay whole.
            joins = not prev.isspace() and (
                unicodedata.combining(ch) != 0
                or unicodedata.normalize("NFC", prev + ch)
                != unicodedata.normalize("NFC", prev) + unicodedata.normalize("NFC", ch)
            )
        if joins:
            chunks[-1].append(i)
        else:
            chunks.append([i])
    return chunks
