"""Text as typed in Word's Find box: the codes Word uses for characters it cannot take."""

from __future__ import annotations

__all__ = ["WORD_FIND_MAX", "word_find"]

#: Characters Word's Find box needs written as codes (``^`` itself first).
_WORD_CODES = (
    ("^", "^^"),
    ("\r", "^p"),
    ("\n", "^l"),
    ("\t", "^t"),
    ("\u00a0", "^s"),
    ("\u2011", "^~"),
    ("\u00ad", "^-"),
)
WORD_FIND_MAX = 255  # characters Word's Find box takes


def word_find(text: str) -> str | None:
    """``text`` as typed in Word's Find box (``^p`` a paragraph mark, ``^l`` a line break,
    ``^t`` a tab, ``^s`` a no-break space, ``^~`` a no-break hyphen, ``^-`` a soft hyphen,
    ``^^`` a caret); None when that is longer than the box takes."""
    for char, code in _WORD_CODES:
        text = text.replace(char, code)
    return text if len(text) <= WORD_FIND_MAX else None
