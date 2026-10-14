"""Word's Find codes read back into characters (the inverse of :func:`wordfind.word_find`)."""

from __future__ import annotations

import re

__all__ = ["translate_codes"]

#: Codes with a fixed character: the set ``wordfind`` writes, plus Word's two hyphen
#: elements (``^-`` optional, ``^~`` no-break).
_CODES = {
    "^": "^",
    "p": "\r",
    "l": "\n",
    "t": "\t",
    "s": "\N{NO-BREAK SPACE}",
    "-": "\N{SOFT HYPHEN}",
    "~": "\N{NON-BREAKING HYPHEN}",
}
_CODE = re.compile(r"\^(?:u(\d+)|(.))", re.DOTALL)


def _one(match: re.Match[str]) -> str:
    digits, code = match.groups()
    if digits is not None:
        try:
            return chr(int(digits))
        except (ValueError, OverflowError):
            return match.group(0)
    return _CODES.get(code, match.group(0))


def translate_codes(phrase: str) -> str:
    """``phrase`` with Word's Find codes replaced by their characters, left to right.

    ``^^`` a caret, ``^p`` a paragraph mark, ``^l`` a line break, ``^t`` a tab, ``^s`` a
    no-break space, ``^-`` an optional hyphen, ``^~`` a no-break hyphen, ``^uNNN`` the
    character with that decimal code. Any other ``^x`` (and a trailing ``^``) stays as
    written, so ``^^p`` is a caret followed by ``p``.
    """
    return _CODE.sub(_one, phrase)
