"""Text as typed in Word's Find box: the codes Word uses for characters it cannot take.

Soft and no-break hyphens
    Word keeps two forms of each and its Find tells them apart (measured in Word
    16.0.17932 with ``Range.Find``, no wildcards):

    * an optional hyphen (``w:softHyphen``) is found by ``^-``, and a search skips it: the
      word typed without it finds the word that holds it; ``^u173`` does not find it,
      and it is skipped next to a paragraph mark too (``alpha^pbeta`` finds the mark after
      ``alpha`` and an optional hyphen);
    * a literal U+00AD in the text is found by ``^u173`` only, and is not skipped;
    * a no-break hyphen (``w:noBreakHyphen``) is found by ``^~`` and by a plain ``-``;
      ``^u8209`` does not find it;
    * a literal U+2011 in the text is found by ``^u8209`` only.

    The text model reads both forms as the same character, so a caller that knows which
    hyphens are Word's own passes their offsets as ``typed`` (see
    :attr:`doc_control.model.index.Paragraph.typed`).
"""

from __future__ import annotations

from collections.abc import Collection

__all__ = ["WORD_FIND_MAX", "as_searched", "two_forms", "word_find"]

SOFT_HYPHEN = "\N{SOFT HYPHEN}"
NO_BREAK_HYPHEN = "\N{NON-BREAKING HYPHEN}"
#: Characters Word's Find box needs written as codes (``^`` itself first).
_WORD_CODES = (
    ("^", "^^"),
    ("\r", "^p"),
    ("\n", "^l"),
    ("\t", "^t"),
    ("\N{NO-BREAK SPACE}", "^s"),
)
#: Per hyphen: the code of the literal character, then of Word's own element.
_HYPHEN_CODES = {SOFT_HYPHEN: ("^u173", "^-"), NO_BREAK_HYPHEN: ("^u8209", "^~")}
WORD_FIND_MAX = 255  # characters Word's Find box takes


def two_forms(text: str) -> bool:
    """``text`` holds a soft or no-break hyphen: which Find code matches it depends on the
    form the document holds (see the module docstring)."""
    return SOFT_HYPHEN in text or NO_BREAK_HYPHEN in text


def word_find(text: str, typed: Collection[int] | None = None) -> str | None:
    """``text`` as typed in Word's Find box (``^p`` a paragraph mark, ``^l`` a line break,
    ``^t`` a tab, ``^s`` a no-break space, ``^^`` a caret); None when that is longer than
    the box takes.

    ``typed``: the offsets in ``text`` of the soft and no-break hyphens that are Word's own
    (written ``^-`` and ``^~``; the others are literal characters, ``^u173`` and
    ``^u8209``). Left out, the form is not known and a text holding such a hyphen gives
    None, since no one Find string matches both forms.
    """
    if not two_forms(text):
        for char, code in _WORD_CODES:
            text = text.replace(char, code)
    elif typed is None:
        return None
    else:
        codes = dict(_WORD_CODES)
        text = "".join(
            _HYPHEN_CODES[char][k in typed] if char in _HYPHEN_CODES else codes.get(char, char)
            for k, char in enumerate(text)
        )
    return text if len(text) <= WORD_FIND_MAX else None


def as_searched(text: str, typed: Collection[int]) -> tuple[str, list[int]]:
    """``text`` as a plain search in Word meets it, and the offset in ``text`` of each
    character kept: Word's own optional hyphens (at ``typed``) are skipped, and its own
    no-break hyphens are met by a plain ``-``. Literal ones stay as they are."""
    out: list[str] = []
    kept: list[int] = []
    for k, char in enumerate(text):
        if k in typed and char == SOFT_HYPHEN:
            continue
        out.append("-" if k in typed and char == NO_BREAK_HYPHEN else char)
        kept.append(k)
    return "".join(out), kept
