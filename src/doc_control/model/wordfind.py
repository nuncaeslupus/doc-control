"""Text as typed in Word's Find box: the codes Word uses for characters it cannot take."""

from __future__ import annotations

__all__ = ["WORD_FIND_MAX", "two_forms", "word_find"]

#: Characters Word's Find box needs written as codes (``^`` itself first).
_WORD_CODES = (
    ("^", "^^"),
    ("\r", "^p"),
    ("\n", "^l"),
    ("\t", "^t"),
    ("\u00a0", "^s"),
)
WORD_FIND_MAX = 255  # characters Word's Find box takes


def two_forms(text: str) -> bool:
    """``text`` holds a soft or no-break hyphen, so Word's Find has no one string for it.

    Word keeps two forms of each, matched by different codes, and the text model reads both
    as the same character: an optional hyphen (``w:softHyphen``) is found by ``^-`` and a
    literal U+00AD only by ``^u173``; a no-break hyphen (``w:noBreakHyphen``) by ``^~`` and a
    literal U+2011 only by ``^u8209`` (measured in Word 16.0.17932).
    """
    return "\u00ad" in text or "\u2011" in text


def word_find(text: str) -> str | None:
    """``text`` as typed in Word's Find box (``^p`` a paragraph mark, ``^l`` a line break,
    ``^t`` a tab, ``^s`` a no-break space, ``^^`` a caret); None when that is longer than
    the box takes, or when the text holds a soft or no-break hyphen, which no one Find
    string matches in both forms Word keeps."""
    if two_forms(text):
        return None
    for char, code in _WORD_CODES:
        text = text.replace(char, code)
    return text if len(text) <= WORD_FIND_MAX else None
