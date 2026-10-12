"""Text search over a paragraph's visible text, with offsets in the original text.

A :class:`Matcher` finds non-empty matches of a literal or a regular expression and
reports them as ``(start, end)`` code-point offsets into the *original* visible text,
end exclusive, whatever folding was used to find them.

Folding
    ``ignore_case`` compares case-folded text (``ß`` equals ``ss``); ``ignore_accents``
    drops the combining marks left after canonical decomposition (``á`` equals ``a``,
    ``ñ`` equals ``n``, and ``a`` followed by U+0301 equals ``á``). Folding works one
    original character at a time, so each folded character knows the original
    character it came from. A match is mapped back to whole original characters: it
    starts at the character that produced its first folded character and ends after
    the character that produced its last one, plus any combining marks that folded
    away right behind it (so a range never splits a letter from its mark).

Regular expressions
    Python ``re`` syntax, run per paragraph (a match never crosses a paragraph). With
    ``ignore_accents`` the pattern is matched against the text with accents removed, so
    write it without accents. ``ignore_case`` uses ``re.IGNORECASE``. ``whole_word``
    requires a non-word character (combining marks count as word characters) on both
    sides, and wraps the pattern in a group, so inline flags such as ``(?i)`` are not
    allowed with it. Python's engine cannot be interrupted, so patterns shaped like the
    classic catastrophic ones (an unbounded repeat whose body is itself an unbounded
    repeat, ``(a+)+``) and patterns over 1000 characters are refused up front; callers
    add a wall-clock budget between paragraphs.
"""

from __future__ import annotations

import bisect
import importlib
import itertools
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from doc_control.errors import DocControlError, ErrorCode

MAX_QUERY_LENGTH = 1000
# Combining marks, for word boundaries: a letter and its mark are one word character.
_MARKS = "\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
_WORD = rf"[\w{_MARKS}]"
_UNBOUNDED = 65535  # re's MAXREPEAT is far above this; bounded repeats stay below


@dataclass(frozen=True)
class SearchOptions:
    query: str
    regex: bool = False
    ignore_case: bool = False
    ignore_accents: bool = False
    whole_word: bool = False


class _Folder(dict[str, str]):
    """Character -> folded text, computed once per distinct character."""

    def __init__(self, accents: bool, case: bool) -> None:
        super().__init__()
        self.accents = accents
        self.case = case

    def __missing__(self, ch: str) -> str:
        out = ch
        if self.case:
            out = unicodedata.normalize("NFD", out.casefold())
        if self.accents:
            decomposed = unicodedata.normalize("NFD", out)
            out = "".join(c for c in decomposed if not unicodedata.combining(c))
        self[ch] = out
        return out

    def fold(self, text: str) -> str:
        return "".join(map(self.__getitem__, text))


_FOLDERS: dict[tuple[bool, bool], _Folder] = {}


def _folder(accents: bool, case: bool) -> _Folder:
    key = (accents, case)
    if key not in _FOLDERS:
        _FOLDERS[key] = _Folder(accents, case)
    return _FOLDERS[key]


def _invalid(message: str) -> DocControlError:
    return DocControlError(ErrorCode.SCHEMA_INVALID, message, path="$.query")


class Matcher:
    """A compiled search; reusable across paragraphs."""

    def __init__(self, options: SearchOptions) -> None:
        query = options.query
        if not query:
            raise _invalid("query must not be empty")
        if len(query) > MAX_QUERY_LENGTH:
            raise _invalid(f"query is longer than {MAX_QUERY_LENGTH} characters")
        flags = 0
        fold_case = False
        if options.regex:
            if options.ignore_case:
                flags = re.IGNORECASE
            _check_regex(query)
            pattern = query
        else:
            # Literals are compared on folded text, so ß matches ss and İ matches i.
            fold_case = options.ignore_case
            pattern = re.escape(_folder(options.ignore_accents, fold_case).fold(query))
            if not pattern:
                raise _invalid("query has no characters left after folding")
        if options.whole_word:
            pattern = f"(?<!{_WORD})(?:{pattern})(?!{_WORD})"
        try:
            self._pattern = re.compile(pattern, flags)
        except (re.error, RecursionError, OverflowError) as exc:
            raise _invalid(f"invalid regular expression: {exc}") from exc
        self._folder = (
            _folder(options.ignore_accents, fold_case)
            if options.ignore_accents or fold_case
            else None
        )

    def spans(self, text: str) -> Iterator[tuple[int, int]]:
        """Non-empty matches of ``text`` as ``(start, end)`` offsets into ``text``."""
        if self._folder is None:
            for m in self._pattern.finditer(text):
                if m.end() > m.start():
                    yield m.start(), m.end()
            return
        folded = self._folder.fold(text)
        found = [
            (m.start(), m.end()) for m in self._pattern.finditer(folded) if m.end() > m.start()
        ]
        if not found:
            return
        lens = [len(self._folder[c]) for c in text]
        if lens.count(1) == len(text):
            yield from found
            return
        starts = list(itertools.accumulate(lens, initial=0))
        done = 0  # a character that folds to several (ß -> ss) is reported once
        for fs, fe in found:
            first = bisect.bisect_right(starts, fs) - 1
            end = bisect.bisect_right(starts, fe - 1)  # one past the char holding fe - 1
            while end < len(text) and lens[end] == 0:
                end += 1
            if first >= done:
                done = end
                yield first, end


def _check_regex(pattern: str) -> None:
    try:
        parsed = importlib.import_module("re._parser").parse(pattern)
    except (re.error, RecursionError, OverflowError) as exc:
        raise _invalid(f"invalid regular expression: {exc}") from exc
    if _catastrophic(parsed):
        raise _invalid(
            "regular expression repeats a repeat (like (a+)+), which can take"
            " exponential time; rewrite it without nested unbounded repeats"
        )


def _catastrophic(parsed: Any) -> bool:
    """True for an unbounded repeat whose body repeats unboundedly with nothing to separate."""
    consts = importlib.import_module("re._constants")
    repeats = (consts.MAX_REPEAT, consts.MIN_REPEAT)

    def unbounded(op: Any, av: Any) -> bool:
        return bool(op in repeats and av[1] >= _UNBOUNDED)

    def flat(items: Any) -> list[tuple[Any, Any]]:
        out: list[tuple[Any, Any]] = []
        for op, av in items:
            if op == consts.SUBPATTERN:
                out += flat(av[3])
            else:
                out.append((op, av))
        return out

    def repeats_itself(body: list[tuple[Any, Any]]) -> bool:
        for i, (op, av) in enumerate(body):
            if unbounded(op, av):
                rest = body[:i] + body[i + 1 :]
                if all(r_op in repeats and r_av[0] == 0 for r_op, r_av in rest):
                    return True
        return False

    def walk(items: Any) -> bool:
        for op, av in items:
            if op in repeats:
                if unbounded(op, av) and repeats_itself(flat(av[2])):
                    return True
                if walk(av[2]):
                    return True
            elif op == consts.SUBPATTERN:
                if walk(av[3]):
                    return True
            elif op == consts.BRANCH and any(walk(branch) for branch in av[1]):
                return True
        return False

    return walk(parsed)
