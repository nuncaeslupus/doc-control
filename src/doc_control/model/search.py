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

    Marks are every character of Unicode category M (Latin and Greek accents, Hebrew
    points, Arabic harakat, Indic vowel signs). Units are not full graphemes: ZWJ
    sequences and flag pairs may be split, and Hangul conjoining jamo are separate units, so
    they are not unified with precomposed syllables.

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
import functools
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
from doc_control.model import loosefold
from doc_control.model.loosefold import LEVELS, Level

MAX_QUERY_LENGTH = 1000
Span = tuple[int, int]


def _mark_class() -> str:
    """Regex class body of every mark (general category M*) in the planes that have any."""
    out: list[str] = []
    start = prev = -2
    for cp in (*range(0x30000), *range(0xE0000, 0xE0200)):
        if unicodedata.category(chr(cp)).startswith("M"):
            if cp != prev + 1:
                if start >= 0:
                    out.append(f"{chr(start)}-{chr(prev)}")
                start = cp
            prev = cp
    out.append(f"{chr(start)}-{chr(prev)}")
    return "".join(out)


# Marks (Latin and Greek accents, Hebrew points, Arabic harakat, Indic vowel signs, ...) for
# units and word boundaries: a letter and its marks go together.
# Built on first use: scanning the code points costs ~40 ms, too much for every import.
@functools.cache
def _marks() -> tuple[str, re.Pattern[str], re.Pattern[str]]:
    """The mark class, as (word class, has-a-mark pattern, unit pattern)."""
    marks = _mark_class()
    return (
        rf"[\w{marks}]",
        re.compile(f"[{marks}]"),
        re.compile(rf"(?s:[^{marks}][{marks}]*|[{marks}]+)"),
    )


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


_FOLD_CACHE = 1 << 16  # paragraph texts whose folding is kept


class _Folder:
    """Folds text unit by unit, remembering where each unit ends."""

    def __init__(self, accents: bool, case: bool) -> None:
        self.accents = accents
        self.case = case
        self._cache: dict[str, str] = {}
        self._folded: dict[str, tuple[str, list[str] | None]] = {}

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

    def fold(self, text: str) -> tuple[str, list[str] | None]:
        """``(folded, units)``; ``units`` is ``None`` when each character is its own unit.

        Results are kept per text (a document's paragraphs are searched again and again
        when it is held open); callers must not change the returned list.
        """
        hit = self._folded.get(text)
        if hit is None:
            if len(self._folded) >= _FOLD_CACHE:
                self._folded.clear()
            hit = self._folded[text] = self._fold(text)
        return hit

    def _fold(self, text: str) -> tuple[str, list[str] | None]:
        if text.isascii():
            return (text.casefold() if self.case else text), None
        _, has_mark, unit = _marks()
        if has_mark.search(text):
            units = unit.findall(text)
            return "".join(map(self.unit, units)), units
        if not (self.case or self.accents) and unicodedata.is_normalized("NFC", text):
            return text, None  # folding would only apply NFC, a no-op here
        return "".join(map(self.unit, text)), None


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
            word = _marks()[0]
            pattern = f"(?<!{word})(?:{pattern})(?!{word})"
        try:
            self._pattern = re.compile(pattern, flags)
        except (re.error, RecursionError, OverflowError) as exc:
            raise _invalid(f"invalid regular expression: {exc}") from exc
        self._folder = _folder(options.ignore_accents, fold_case)

    @property
    def pattern(self) -> re.Pattern[str]:
        """The compiled pattern (a regex's groups are read from it)."""
        return self._pattern

    def spans(self, text: str) -> Iterator[Span]:
        """Non-empty matches of ``text`` as ``(start, end)`` offsets into ``text``."""
        for start, end, _ in self.matches(text):
            yield start, end

    def matches(self, text: str) -> Iterator[Match]:
        """Non-empty matches as ``(start, end, groups)``, offsets into ``text``.

        Groups are the capture groups' text ("" for one that did not take part). A group
        is read from ``text`` itself when its edges map back to it; when an edge falls
        inside what one character folds to (``ß`` is ``ss``) the folded text is used,
        so a match is never dropped for its groups.
        """
        folded, units = self._folder.fold(text)
        found = [m for m in self._pattern.finditer(folded) if m.end() > m.start()]
        if not found:
            return
        if text.isascii():
            for m in found:
                yield m.start(), m.end(), tuple(g or "" for g in m.groups())
            return
        parts = units if units is not None else text
        pieces = [self._folder.unit(u) for u in parts]
        bounds = list(itertools.accumulate(map(len, units), initial=0)) if units else None
        starts = list(itertools.accumulate(map(len, pieces), initial=0))
        boundaries = set(starts)

        def origin(pos: int, end: bool) -> int:
            i = bisect.bisect_left(starts, pos) if end else bisect.bisect_right(starts, pos) - 1
            return i if bounds is None else bounds[i]

        for m in found:
            fs, fe = m.span()
            if fs not in boundaries or fe not in boundaries:
                continue  # inside a unit, or inside what one character folds to
            groups: list[str] = []
            for n in range(1, (self._pattern.groups or 0) + 1):
                gs, ge = m.span(n)
                if gs < 0:
                    groups.append("")
                elif gs in boundaries and ge in boundaries:
                    groups.append(text[origin(gs, False) : origin(ge, True)])
                else:
                    groups.append(folded[gs:ge])
            yield origin(fs, False), origin(fe, True), tuple(groups)


class LevelMatcher:
    """A search at a :mod:`~doc_control.model.loosefold` level, each span tagged.

    ``exact`` is today's :class:`Matcher` with the options as given (a regex included);
    ``default`` and ``loose`` fold the literal query and the text (case and accents are
    implied by the level). :meth:`spans` gives what :meth:`Matcher.spans` gives, and leaves
    in :attr:`levels` the strictest level each span of that text meets. ``whole_word``
    looks at the original characters on either side of a folded span.
    """

    def __init__(self, options: SearchOptions, level: Level) -> None:
        self._options = options
        self._level = level
        self._exact: Matcher | None = None
        self.levels: dict[Span, Level] = {}
        if level == "exact":
            self._exact = Matcher(options)
            return
        if len(options.query) > MAX_QUERY_LENGTH:
            raise _invalid(f"query is longer than {MAX_QUERY_LENGTH} characters")
        try:
            loosefold.fold_phrase(options.query, level)
        except DocControlError as exc:
            raise _invalid(exc.message) from exc

    def spans(self, text: str) -> Iterator[Span]:
        """The spans of ``text``, in order; :attr:`levels` tags them."""
        query = self._options.query
        found: list[tuple[int, int, Level]]
        if self._exact is not None:
            found = [
                (s, e, "exact" if self._options.regex else _strictest(text[s:e], query))
                for s, e in self._exact.spans(text)
            ]
        else:
            found = [
                (h.start, h.end, h.level)
                for h in loosefold.hits([text], query, self._level)
                if not self._options.whole_word or _whole(text, h.start, h.end)
            ]
        self.levels = {(s, e): level for s, e, level in found}
        return iter([(s, e) for s, e, _ in found])


def _strictest(matched: str, query: str) -> Level:
    """The strictest level at which ``matched`` and ``query`` fold to the same string."""
    if matched == query:
        return "exact"
    for level in LEVELS[1:]:
        folded = loosefold.fold(query, level).text
        if folded and loosefold.fold(matched, level).text == folded:
            return level
    return "loose"


@functools.cache
def _word_char() -> re.Pattern[str]:
    return re.compile(_marks()[0])


def _whole(text: str, start: int, end: int) -> bool:
    """No letter, digit or mark touches ``text[start:end]`` on either side."""
    word = _word_char()
    return not (start > 0 and word.match(text, start - 1)) and not (
        end < len(text) and word.match(text, end)
    )


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

Match = tuple[int, int, tuple[str, ...]]  # (start, end, capture groups)
Item = tuple[int, str]  # (paragraph number, visible text)


class LocalScan:
    """Search paragraphs in this process, checking the deadline between paragraphs.

    Iterating yields ``(number, spans)`` for each paragraph that has matches, in order.
    ``stopped_at`` is the first paragraph left unsearched when the deadline passed
    (at least one paragraph is always searched), else ``None``.
    """

    def __init__(
        self, matcher: Matcher | LevelMatcher, items: Sequence[Item], deadline: float
    ) -> None:
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


def _worker(
    options: SearchOptions, items: Sequence[Item], progress: Any, conn: Any, detail: bool = False
) -> None:
    """Child process body: stream matches back, publishing the paragraph being searched."""
    try:
        matcher = Matcher(options)
        for number, text in items:
            progress.value = number
            found: list[Any] = []
            if text:
                found = list(matcher.matches(text) if detail else matcher.spans(text))
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
    A script that calls this (through ``find``) must guard its entry point with
    ``if __name__ == "__main__":``, as ``spawn`` re-imports the main module.
    """

    def __init__(
        self,
        options: SearchOptions,
        items: Sequence[Item],
        deadline: float,
        detail: bool = False,
    ) -> None:
        self._detail = detail
        self._options = options
        self._items = list(items)
        self._deadline = deadline
        self.stopped_at: int | None = None

    def __iter__(self) -> Generator[tuple[int, list[Any]], None, None]:
        if not self._items:
            return
        ctx = multiprocessing.get_context("spawn")
        progress = ctx.Value("q", self._items[0][0], lock=False)
        recv, send = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=_worker,
            args=(self._options, self._items, progress, send, self._detail),
            daemon=True,
        )
        try:
            proc.start()
            send.close()
            last = 0  # number of the last paragraph yielded
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
                last = message[0]
                yield message
            proc.terminate()
            proc.join(5)
            late: list[Any] = []  # what the child sent before it was killed
            finished = False
            while True:
                try:
                    if not recv.poll(0):
                        break
                    message = recv.recv()
                except (EOFError, OSError):
                    break
                if message is None:
                    finished = True
                elif message[0] != "error":
                    late.append(message)
            if not finished:
                self.stopped_at = max([int(progress.value), last + 1, *(m[0] + 1 for m in late)])
            yield from late
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(5)
                if proc.is_alive():
                    proc.kill()
                    proc.join()
            send.close()
            recv.close()
