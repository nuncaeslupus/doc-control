"""Normalised view of visible paragraph text, with a two-way offset map.

Offsets are code points of the visible text, as in :mod:`doc_control.model.index`; the
end of a range is exclusive. :func:`normalize` applies, in this order:

1. removal of soft hyphens (U+00AD) and zero-width characters (U+200B, U+200C, U+200D,
   U+2060, U+FEFF);
2. NFC;
3. collapse of every run of whitespace (``str.isspace``: space, tab, newline, no-break
   space U+00A0, U+2028...) into one space. Nothing is trimmed.

Zero-width characters go even where they hold a sequence together: U+200D (ZWJ) is
removed from an emoji sequence, which then reads as its separate emoji. NFC is applied to
clusters of at most 32 code points, so a longer run of marks (not found in real text) is
normalised in pieces.

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
from collections.abc import Sequence
from dataclasses import dataclass

#: Longest run of code points normalised together. Real clusters are a few characters;
#: Unicode's stream-safe text format allows 31 combining marks after a base. A longer run
#: (U+0F73 repeated, say) is cut here, where NFC of the whole would cost quadratic time.
MAX_CLUSTER = 32
IGNORABLE = frozenset("\u00ad\u200b\u200c\u200d\u2060\ufeff")


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


def normalized_text(original: str) -> str:
    """``normalize(original).text`` without the offset maps (for bulk reads).

    Exact by construction: the shortcut applies only when the text without ignorables is
    already NFC and has no combining marks, so no chunk can compose; else the full path.
    """
    kept = "".join(ch for ch in original if ch not in IGNORABLE)
    if any(unicodedata.combining(ch) for ch in kept) or not unicodedata.is_normalized("NFC", kept):
        return normalize(original).text
    out: list[str] = []
    for ch in kept:
        if ch.isspace():
            if not out or out[-1] != " ":
                out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _chunks(original: str, kept: list[int]) -> list[list[int]]:
    """Split the kept indexes where NFC cannot combine across the cut.

    A character joins the current chunk when it has a combining class, or when NFC of
    the chunk plus the character differs from NFC of the chunk followed by the
    character (a base letter and its accent, or Hangul L+V+T, which compose step by
    step). Whitespace stands alone: nothing composes with it, and a collapsed space
    must stay whole. A chunk stops growing at :data:`MAX_CLUSTER` code points.
    """
    chunks: list[list[int]] = []
    text = ""  # the current chunk's characters
    for i in kept:
        ch = original[i]
        if (
            chunks
            and len(text) < MAX_CLUSTER
            and not text[-1].isspace()
            and (
                unicodedata.combining(ch) != 0
                or (
                    not (ch.isascii() and text.isascii())
                    and unicodedata.normalize("NFC", text + ch)
                    != unicodedata.normalize("NFC", text) + unicodedata.normalize("NFC", ch)
                )
            )
        ):
            chunks[-1].append(i)
            text += ch
        else:
            chunks.append([i])
            text = ch
    return chunks


def encode_runs(values: Sequence[int], lo: int, hi: int) -> list[list[int]]:
    """Compact form of ``values[lo:hi]`` as runs ``[index, value, count]``.

    Entry ``index + k`` is ``value + k`` for ``k < count``. A run whose values do not
    move is written ``[index, value, count, 0]``: entry ``index + k`` is ``value``.
    """
    runs: list[list[int]] = []
    i = lo
    while i < hi:
        step = values[i + 1] - values[i] if i + 1 < hi else 1
        if step not in (0, 1):
            step = 1
        j = i + 1
        while j < hi and values[j] - values[j - 1] == step:
            j += 1
        runs.append([i, values[i], j - i] if step else [i, values[i], j - i, 0])
        i = j
    return runs


def decode_runs(runs: Sequence[Sequence[int]]) -> dict[int, int]:
    """Inverse of :func:`encode_runs`: index to value."""
    out: dict[int, int] = {}
    for run in runs:
        index, value, count = run[:3]
        step = 0 if len(run) > 3 else 1
        for k in range(count):
            out[index + k] = value + k * step
    return out
