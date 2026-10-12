"""Text search over a paragraph's visible text, with offsets in the original text.

A :class:`Matcher` finds non-empty matches of a literal or a regular expression and
reports them as ``(start, end)`` code-point offsets into the *original* visible text,
end exclusive, whatever folding was used to find them.

Units and folding
    The text is cut into *units*: a base character plus the combining marks that follow
    it (a lone run of marks at the very start is a unit of its own). Each unit is folded
    on its own and the folded pieces are concatenated; the search runs on that folded
    text. Folding is always canonical composition (NFC), so ``é`` and ``e`` + U+0301
    are the same thing to a search. ``ignore_case`` also case-folds (``ß`` becomes
    ``ss``); ``ignore_accents`` also drops the combining marks left after decomposition
    (``á`` becomes ``a``, ``ñ`` becomes ``n``).

    A match is kept only if it starts and ends on a unit boundary of the folded text:
    it never begins on a mark, never ends between a letter and its marks (so ``e`` does
    not match inside ``é`` unless accents are ignored), and never covers only part of
    what one character folds to (``s`` does not match ``ß``; ``ss`` does). Kept matches
    are mapped back to the whole original units they cover.

Regular expressions
    Python ``re`` syntax, run per paragraph (a match never crosses a paragraph). With
    ``ignore_accents`` the pattern is matched against the text with accents removed, so
    write it without accents. ``ignore_case`` uses ``re.IGNORECASE``. ``whole_word``
    requires a non-word character (combining marks count as word characters) on both
    sides, and wraps the pattern in a group, so inline flags such as ``(?i)`` are not
    allowed with it. Python's engine cannot be interrupted, so :class:`IsolatedScan`
    runs it in a child process that is killed at a hard deadline; patterns over 1000
    characters and the textbook ``(a+)+`` shape are still refused up front, as a cheap
    first line of defence.
"""

from __future__ import annotations

import bisect
import importlib
import itertools
import multiprocessing
import re
import time
import unicodedata
from collections.abc import Generator, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from doc_control.errors import DocControlError, ErrorCode

MAX_QUERY_LENGTH = 1000
Span = tuple[int, int]
# Combining marks, for units and word boundaries: a letter and its marks go together.
_MARK_RANGES = (
    (0x0300, 0x036F),
    (0x1AB0, 0x1AFF),
    (0x1DC0, 0x1DFF),
    (0x20D0, 0x20FF),
    (0xFE20, 0xFE2F),
)
_MARKS = "".join(f"{chr(a)}-{chr(b)}" for a, b in _MARK_RANGES)
_WORD = rf"[\w{_MARKS}]"
_HAS_MARK = re.compile(f"[{_MARKS}]")
_UNIT = re.compile(rf"(?s:[^{_MARKS}][{_MARKS}]*|[{_MARKS}]+)")
_UNBOUNDED = 65535  # re's MAXREPEAT is far above this; bounded repeats stay below


@dataclass(frozen=True)
class SearchOptions:
    query: str
    regex: bool = False
    ignore_case: bool = False
    ignore_accents: bool = False
    whole_word: bool = False


def _invalid(message: str) -> DocControlError:
    return DocControlError(ErrorCode.SCHEMA_INVALID, message, path="$.query")


class _Folder:
    """Folds text unit by unit, remembering where each unit ends."""

    def __init__(self, accents: bool, case: bool) -> None:
        self.accents = accents
        self.case = case
        self._cache: dict[str, str] = {}

    def unit(self, unit: str) -> str:
        out = self._cache.get(unit)
        if out is None:
            out = unit
            if self.case:
                out = out.casefold()
            if self.accents:
                decomposed = unicodedata.normalize("NFD", out)
                out = "".join(c for c in decomposed if not unicodedata.combining(c))
            out = unicodedata.normalize("NFC", out)
            self._cache[unit] = out
        return out

    def fold(self, text: str) -> tuple[str, list[str] | None, list[int] | None]:
        """``(folded, per-unit folds, original offset of each unit)``.

        Both lists are ``None`` when folding keeps one character per character at the
        same offsets (ASCII text), so offsets need no mapping.
        """
        if text.isascii():
            return (text.casefold() if self.case else text), None, None
        if _HAS_MARK.search(text):
            units = _UNIT.findall(text)
            bounds = list(itertools.accumulate(map(len, units), initial=0))
        else:
            units, bounds = list(text), None
        pieces = [self.unit(u) for u in units]
        return "".join(pieces), pieces, bounds


_FOLDERS: dict[tuple[bool, bool], _Folder] = {}


def _folder(accents: bool, case: bool) -> _Folder:
    key = (accents, case)
    if key not in _FOLDERS:
        _FOLDERS[key] = _Folder(accents, case)
    return _FOLDERS[key]


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
            folded = _folder(options.ignore_accents, fold_case).fold(query)[0]
            if not folded:
                raise _invalid("query has no characters left after folding")
            pattern = re.escape(folded)
        if options.whole_word:
            pattern = f"(?<!{_WORD})(?:{pattern})(?!{_WORD})"
        try:
            self._pattern = re.compile(pattern, flags)
        except (re.error, RecursionError, OverflowError) as exc:
            raise _invalid(f"invalid regular expression: {exc}") from exc
        self._folder = _folder(options.ignore_accents, fold_case)

    def spans(self, text: str) -> Iterator[Span]:
        """Non-empty matches of ``text`` as ``(start, end)`` offsets into ``text``."""
        folded, pieces, bounds = self._folder.fold(text)
        found = [
            (m.start(), m.end()) for m in self._pattern.finditer(folded) if m.end() > m.start()
        ]
        if pieces is None:
            yield from found
            return
        starts = list(itertools.accumulate(map(len, pieces), initial=0))
        boundaries = set(starts)
        for fs, fe in found:
            if fs not in boundaries or fe not in boundaries:
                continue  # inside a unit, or inside what one character folds to
            first = bisect.bisect_right(starts, fs) - 1
            end = bisect.bisect_left(starts, fe)
            if bounds is None:
                yield first, end
            else:
                yield bounds[first], bounds[end]


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


# -- scanning many paragraphs -------------------------------------------------------------

Item = tuple[int, str]  # (paragraph number, visible text)


class LocalScan:
    """Search paragraphs in this process, checking the deadline between paragraphs.

    Iterating yields ``(number, spans)`` for each paragraph that has matches, in order.
    ``stopped_at`` is the first paragraph left unsearched when the deadline passed
    (at least one paragraph is always searched), else ``None``.
    """

    def __init__(self, matcher: Matcher, items: Sequence[Item], deadline: float) -> None:
        self._matcher = matcher
        self._items = items
        self._deadline = deadline
        self.stopped_at: int | None = None

    def __iter__(self) -> Generator[tuple[int, list[Span]], None, None]:
        for done, (number, text) in enumerate(self._items):
            if done and time.monotonic() > self._deadline:
                self.stopped_at = number
                return
            found = list(self._matcher.spans(text)) if text else []
            if found:
                yield number, found


def _worker(options: SearchOptions, items: Sequence[Item], progress: Any, conn: Any) -> None:
    """Child process body: stream matches back, publishing the paragraph being searched."""
    try:
        matcher = Matcher(options)
        for number, text in items:
            progress.value = number
            found = list(matcher.spans(text)) if text else []
            if found:
                conn.send((number, found))
        conn.send(None)
    except BaseException as exc:  # reported to the parent, which raises INTERNAL
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


class IsolatedScan:
    """Like :class:`LocalScan`, but in a child process killed at the deadline.

    Python's regex engine cannot be interrupted, so a pathological pattern on a short
    paragraph would otherwise hang the caller. The child (``spawn`` context, so it
    behaves the same on Windows) publishes the paragraph it is in through shared
    memory; on timeout it is terminated, matches it already sent are kept, and
    ``stopped_at`` is the paragraph it was stuck in. Close the generator to stop early.
    """

    def __init__(self, options: SearchOptions, items: Sequence[Item], deadline: float) -> None:
        self._options = options
        self._items = list(items)
        self._deadline = deadline
        self.stopped_at: int | None = None

    def __iter__(self) -> Generator[tuple[int, list[Span]], None, None]:
        if not self._items:
            return
        ctx = multiprocessing.get_context("spawn")
        progress = ctx.Value("q", self._items[0][0], lock=False)
        recv, send = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=_worker, args=(self._options, self._items, progress, send), daemon=True
        )
        proc.start()
        send.close()
        try:
            while True:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0 or not recv.poll(remaining):
                    break
                try:
                    message = recv.recv()
                except (EOFError, OSError) as exc:
                    raise DocControlError(ErrorCode.INTERNAL, "regex worker ended early") from exc
                if message is None:
                    return
                if message[0] == "error":
                    raise DocControlError(ErrorCode.INTERNAL, f"regex worker failed: {message[1]}")
                yield message
            proc.terminate()
            proc.join(5)
            self.stopped_at = int(progress.value)
            while True:  # matches the child sent before it was killed
                try:
                    if not recv.poll(0):
                        break
                    message = recv.recv()
                except (EOFError, OSError):
                    break
                if message is not None and message[0] != "error" and message[0] < self.stopped_at:
                    yield message
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(5)
                if proc.is_alive():
                    proc.kill()
                    proc.join()
            recv.close()
