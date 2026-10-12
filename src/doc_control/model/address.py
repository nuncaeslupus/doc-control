"""Paragraph addresses ``p<n>#<fp>`` and the ranges built on them.

``n`` is the 1-based index over the main story's paragraphs and ``fp`` the first
8 hex digits of BLAKE2s over the paragraph's visible text, NFC-normalised.

Forms accepted by :func:`parse_range` (offsets are characters of the visible
text, the end exclusive)::

    p142#a7b2a7b2                          the whole paragraph
    p142#a7b2a7b2:10-15                    inside one paragraph
    p142#a7b2a7b2:10..p144#0c9e0c9e:3      across paragraphs (the end paragraph is later)
    p142#a7b2a7b2.s3                       the third sentence (resolved by segmentation)

Parsing is strict: numbers have no leading zeros and no sign. Upper-case hex is
accepted and lower-cased, so ``str(parse_range(s)) == s`` for every canonical
``s``. Anything else raises ``DocControlError(ADDRESS_INVALID)``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from doc_control.errors import DocControlError, ErrorCode

FINGERPRINT_LENGTH = 8

# Bounded digit runs keep int() clear of Python's digit-conversion limit.
_NUM = r"(?:0|[1-9][0-9]{0,11})"
_POS = r"[1-9][0-9]{0,11}"
_HEX = rf"[0-9a-fA-F]{{{FINGERPRINT_LENGTH}}}"
_RANGE = re.compile(
    rf"p(?P<n1>{_POS})#(?P<f1>{_HEX})"
    rf"(?::(?P<a>{_NUM})-(?P<b>{_NUM})"
    rf"|:(?P<c>{_NUM})\.\.p(?P<n2>{_POS})#(?P<f2>{_HEX}):(?P<d>{_NUM})"
    rf"|\.s(?P<s>{_POS}))?"
)


def fingerprint(text: str) -> str:
    """The 8-hex-digit fingerprint of a paragraph's visible ``text``."""
    data = unicodedata.normalize("NFC", text).encode("utf-8")
    return hashlib.blake2s(data).hexdigest()[:FINGERPRINT_LENGTH]


@dataclass(frozen=True)
class Address:
    """``p<number>#<fp>``: a paragraph position plus a fingerprint of its text."""

    number: int
    fp: str

    def __str__(self) -> str:
        return f"p{self.number}#{self.fp}"


@dataclass(frozen=True)
class AddressRange:
    """A parsed range. ``start`` and ``end`` are equal unless it crosses paragraphs.

    Offsets are ``None`` for a whole paragraph or a sentence; ``sentence`` is the
    1-based sentence number, only set for the ``.s<k>`` form.
    """

    start: Address
    end: Address
    start_offset: int | None = None
    end_offset: int | None = None
    sentence: int | None = None

    def __str__(self) -> str:
        if self.sentence is not None:
            return f"{self.start}.s{self.sentence}"
        if self.start_offset is None or self.end_offset is None:
            return str(self.start)
        if self.start == self.end:
            return f"{self.start}:{self.start_offset}-{self.end_offset}"
        return f"{self.start}:{self.start_offset}..{self.end}:{self.end_offset}"


def _invalid(text: object, why: str) -> DocControlError:
    return DocControlError(ErrorCode.ADDRESS_INVALID, f"{text!r} {why}", address=str(text))


def parse_address(text: str) -> Address:
    """Parse a bare paragraph address (no offsets, no sentence)."""
    rng = parse_range(text)
    if rng.sentence is not None or rng.start_offset is not None:
        raise _invalid(text, "is not a bare paragraph address like p142#a7b2a7b2")
    return rng.start


def parse_range(text: str) -> AddressRange:
    """Parse any address or range form listed in the module docstring."""
    m = _RANGE.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        raise _invalid(text, "is not an address or range like p142#a7b2a7b2:10-15")
    start = Address(int(m["n1"]), m["f1"].lower())
    if m["s"] is not None:
        return AddressRange(start, start, sentence=int(m["s"]))
    if m["a"] is not None:
        a, b = int(m["a"]), int(m["b"])
        if a > b:
            raise _invalid(text, "ends before it starts")
        return AddressRange(start, start, a, b)
    if m["c"] is not None:
        end = Address(int(m["n2"]), m["f2"].lower())
        if end.number <= start.number:
            raise _invalid(text, "must end in a later paragraph than it starts")
        return AddressRange(start, end, int(m["c"]), int(m["d"]))
    return AddressRange(start, start)
