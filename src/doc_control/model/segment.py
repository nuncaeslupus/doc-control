"""Segmentation of visible paragraph text: sentences, dialogue lines, scene breaks, chapters.

Everything here is a pure function of text. Offsets are code points of the visible
text, the end exclusive, exactly as in :mod:`doc_control.model.index`, and are the
offsets the ``p<n>#<fp>.s<k>`` sentence address resolves to.

Sentences
    :func:`split_sentences` ends a sentence at a run of ``. ! ? …`` (plus the closing
    quotes and brackets that follow it) when whitespace or the end of the text comes
    next, and the next token can start a sentence: an upper-case letter, a digit or an
    opening mark (``¿ ¡ « “ " ( [``). It does not end after:

    * a known abbreviation (``Sr.``, ``Dra.``, ``pág.``, ``Mr.``…), a dotted
      abbreviation (``EE.UU.``, ``a.m.``) or an initial (``J. R. Tolkien``);
    * a terminator followed by a lower-case letter, including an ellipsis
      (``Pues... no sé``) and a ``?``/``!`` before a closing quote or a raya
      narrator interjection (``—¿Vienes? —preguntó``);
    * a number (``3.14``): no whitespace follows the dot.

    ``etc.`` and ``no.`` end a sentence only before a capital. A line break (``\\n``)
    always ends one. ``language`` selects the abbreviation list: ``"es"`` (the
    default) or ``"en"``/``"generic"``; any other value is ``SCHEMA_INVALID``.

Dialogue
    :func:`dialogue_lines` finds the lines that open with a raya (``—``) and splits
    each into speech and narrator parts: ``—Hola —dijo Ana—. Vamos.`` has speech
    ``Hola``, narration ``dijo Ana`` and speech ``. Vamos.``.

Scene breaks and chapters
    :func:`is_scene_break` recognises ornament paragraphs (``* * *``, ``---``, ``###``).
    :func:`parse_chapter_heading` recognises ``Capítulo 3``, ``CHAPTER IV. Title``,
    ``Prólogo`` and bare numerals; :func:`segment_document` applies both to a list of
    paragraph texts. Stable ids, signals and overrides belong to structure detection.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from doc_control.errors import DocControlError, ErrorCode

DEFAULT_LANGUAGE = "es"
LANGUAGES = ("es", "en", "generic")

_TERMINATORS = ".!?\N{HORIZONTAL ELLIPSIS}"
_CLOSERS = "\"'»\u201d\u2019)]}\u203a"
_RAYAS = "—―"


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_COMMON_ABBREVIATIONS = _words(
    "vs dr dra prof sr sra srta ud uds vd vds st jr mr mrs ms mt fig vol ed no etc cf pp tel apdo"
)
_SPANISH_ABBREVIATIONS = _words(
    "dña doña dn excmo excma ilmo ilma "
    "lic ing arq sres sras av avda pág págs art arts cap caps núm nº dpto depto aprox "
    "máx mín cía cia ej admón gral cnel cmdr mons fr izq dcha esq pl pza ctra"
)
_GENERIC_ABBREVIATIONS = _words(
    "inc ltd co corp gen col capt lt sgt rev hon esq ave blvd rd approx dept est "
    "ch sec pt pg a.m. p.m."
)
_ABBREVIATIONS = {
    "es": _COMMON_ABBREVIATIONS | _SPANISH_ABBREVIATIONS,
    "generic": _COMMON_ABBREVIATIONS | _GENERIC_ABBREVIATIONS,
}
# Abbreviations that may also close a sentence: they end one only before a capital.
_SOFT_ABBREVIATIONS = _words("etc no co est inc ltd corp ave blvd rd approx dept a.m. p.m.")


@dataclass(frozen=True)
class Span:
    """Characters ``[start, end)`` of a paragraph's visible text."""

    start: int
    end: int

    def text(self, source: str) -> str:
        return source[self.start : self.end]


@dataclass(frozen=True)
class DialoguePart:
    """A stretch of a dialogue line: the characters' ``speech`` or the ``narration``."""

    kind: Literal["speech", "narration"]
    start: int
    end: int


@dataclass(frozen=True)
class DialogueLine:
    """A line opening with a raya, from the raya to the end of the line."""

    start: int
    end: int
    parts: tuple[DialoguePart, ...]


@dataclass(frozen=True)
class ChapterHeading:
    """A recognised chapter heading.

    ``kind`` is ``chapter``, ``part``, ``prologue``, ``epilogue``, ``introduction`` or
    ``numeral`` (a bare number); ``number`` is ``None`` when the heading has none.
    """

    kind: str
    number: int | None
    title: str


@dataclass(frozen=True)
class Chapter:
    """Paragraphs ``first``..``last`` (1-based, inclusive) under one heading."""

    first: int
    last: int
    heading: ChapterHeading


@dataclass(frozen=True)
class Segmentation:
    """Paragraph-level segmentation of a document; paragraph numbers are 1-based."""

    sentences: tuple[tuple[Span, ...], ...]  # per paragraph, in order
    dialogue: tuple[tuple[int, DialogueLine], ...]
    scene_breaks: tuple[int, ...]
    chapters: tuple[Chapter, ...]


# --------------------------------------------------------------------------- sentences


def split_sentences(text: str, language: str = DEFAULT_LANGUAGE) -> list[Span]:
    """The sentences of one paragraph's ``text``, in order, trimmed of whitespace."""
    abbreviations = _abbreviations(language)
    spans: list[Span] = []
    base = 0
    for line in text.split("\n"):
        spans.extend(_split_line(line, base, abbreviations))
        base += len(line) + 1
    return spans


def _abbreviations(language: str) -> frozenset[str]:
    if language not in LANGUAGES:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"unknown language {language!r}; expected one of {', '.join(LANGUAGES)}",
            language=language,
        )
    return _ABBREVIATIONS["es" if language == "es" else "generic"]


def sentence_span(text: str, k: int, language: str = DEFAULT_LANGUAGE) -> Span | None:
    """Sentence ``k`` (1-based) of ``text``, or ``None`` when there are fewer."""
    spans = split_sentences(text, language)
    return spans[k - 1] if 1 <= k <= len(spans) else None


_SPACED_ELLIPSIS = re.compile(r"\.(?: \.){2,}")


def _split_line(line: str, base: int, abbreviations: frozenset[str]) -> list[Span]:
    spans: list[Span] = []
    n = len(line)
    start = _skip_space(line, 0)
    i = start
    while i < n:
        if line[i] not in _TERMINATORS:
            i += 1
            continue
        run_start = i
        spaced = _SPACED_ELLIPSIS.match(line, i)
        if spaced is not None:
            i = spaced.end()
        while i < n and line[i] in _TERMINATORS:
            i += 1
        while i < n and line[i] in _CLOSERS:
            i += 1
        if i < n and not line[i].isspace():
            continue  # 3.14, www.x.com, «Vamos.», dijo
        nxt = _skip_space(line, i)
        if nxt < n and not _ends_sentence(line, start, run_start, nxt, abbreviations):
            continue
        spans.append(Span(base + start, base + i))
        start = i = nxt
    if start < n and not line[start:].isspace():
        spans.append(Span(base + start, base + len(line.rstrip())))
    return spans


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _ends_sentence(
    line: str, sentence_start: int, run_start: int, nxt: int, abbreviations: frozenset[str]
) -> bool:
    """Whether the terminator run at ``run_start`` ends the sentence, given what follows."""
    follower = _classify_follower(line, nxt)
    if (
        line[run_start] == "."
        and not line.startswith("..", run_start)
        and _SPACED_ELLIPSIS.match(line, run_start) is None
    ):
        token = _word_before(line, sentence_start, run_start)
        folded = token.lower()
        start = run_start - len(token)
        if start > 0 and line[start - 1].isdigit():
            return follower == "start"  # 1st. 2nd. (an ordinal, not an abbreviation)
        if folded == "st" and token == "St" and _capitalised_before(line, sentence_start, start):
            return follower == "start"  # Elm St. (a street), not St. John
        if folded in abbreviations or ("." in token and len(token) <= 8):  # EE.UU. S.A.
            soft = folded in _SOFT_ABBREVIATIONS or ("." in token and folded not in abbreviations)
            return soft and follower == "start" and not line[nxt].isdigit()
        if len(token) == 1 and token.isupper() and follower == "start":
            return False  # initial: J. R. Tolkien
    return follower == "start"


def _capitalised_before(line: str, floor: int, end: int) -> bool:
    """Whether the word ending just before ``end`` starts with a capital letter."""
    word = _word_before(line, floor, len(line[:end].rstrip()))
    return word[:1].isupper()


def _word_before(line: str, floor: int, end: int) -> str:
    i = end
    while i > floor and (line[i - 1].isalpha() or line[i - 1] == "."):
        i -= 1
    return line[i:end]


def _classify_follower(line: str, nxt: int) -> Literal["start", "continue"]:
    """Whether the text at ``nxt`` can open a new sentence or continues the old one."""
    ch = line[nxt]
    if ch in _RAYAS:
        after = _skip_space(line, nxt + 1)
        if after >= len(line):
            return "start"
        ch = line[after]
    return "continue" if ch.islower() else "start"


# ---------------------------------------------------------------------------- dialogue


def dialogue_lines(text: str) -> list[DialogueLine]:
    """The dialogue lines of ``text``: each physical line opening with a raya."""
    lines: list[DialogueLine] = []
    base = 0
    for line in text.split("\n"):
        first = _skip_space(line, 0)
        if first < len(line) and line[first] in _RAYAS:
            lines.append(_dialogue_line(line, base, first))
        base += len(line) + 1
    return lines


def _dialogue_line(line: str, base: int, first: int) -> DialogueLine:
    parts: list[DialoguePart] = []

    def add(kind: Literal["speech", "narration"], lo: int, hi: int) -> None:
        while lo < hi and line[lo].isspace():
            lo += 1
        while hi > lo and line[hi - 1].isspace():
            hi -= 1
        if lo < hi:
            parts.append(DialoguePart(kind, base + lo, base + hi))

    kind: Literal["speech", "narration"] = "speech"
    pos = first + 1
    for i in range(first + 1, len(line)):
        if line[i] not in _RAYAS:
            continue
        add(kind, pos, i)
        if kind == "narration":
            kind = "speech"
        elif not _new_turn(line, pos, i):
            kind = "narration"
        pos = i + 1
    add(kind, pos, len(line))
    return DialogueLine(base + first, base + len(line.rstrip()), tuple(parts))


def _new_turn(line: str, lo: int, raya: int) -> bool:
    """Whether a raya in speech starts another turn: ``—Vamos. —Ya voy``."""
    before = line[lo:raya].rstrip()
    if not before or before[-1] not in _TERMINATORS:
        return False
    after = _skip_space(line, raya + 1)
    return after < len(line) and not line[after].islower()


# ------------------------------------------------------------------------ scene breaks

_BREAK_CHARS = frozenset("*#~-_=•·+◊◆◇✦✧\N{EN DASH}\N{EM DASH}")
_ORNAMENTS = frozenset("⁂❦❧※§")


def is_scene_break(text: str) -> bool:
    """Whether a paragraph is only a scene-break ornament: ``* * *``, ``---``, ``⁂``."""
    marks = "".join(text.split())
    if not marks or len(set(marks)) != 1:
        return False
    if len(marks) == 1 and marks in "*#":
        return True
    if marks[0] in _ORNAMENTS:
        return True
    return len(marks) >= 3 and marks[0] in _BREAK_CHARS


# --------------------------------------------------------------------------- chapters

_WORDS = (
    "uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince "
    "dieciséis diecisiete dieciocho diecinueve veinte",
    "primero segundo tercero cuarto quinto sexto séptimo octavo noveno décimo",
    "primera segunda tercera cuarta quinta sexta séptima octava novena décima",
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty",
    "first second third fourth fifth sixth seventh eighth ninth tenth",
)
_NUMBER_WORDS: dict[str, int] = {
    word: i for group in _WORDS for i, word in enumerate(group.split(), 1)
}
_NUMBER_WORDS["una"] = 1
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_ROMAN_FORMS = (
    ("M", 1000), ("CM", 900), ("D", 500), ("CD", 400), ("C", 100), ("XC", 90),
    ("L", 50), ("XL", 40), ("X", 10), ("IX", 9), ("V", 5), ("IV", 4), ("I", 1),
)  # fmt: skip
_HEADING = re.compile(
    r"(?P<word>cap[ií]tulo|cap\.|chapter|chap\.|parte|part|libro|book)\s*"
    r"(?P<num>\d{1,4}|[ivxlcdm]{1,8}|[^\W\d_]{3,12})(?![^\W\d_])\s*[.:)\-\u2013\u2014]?\s*(?P<title>.*)",
    re.IGNORECASE,
)
_REVERSED = re.compile(
    r"(?P<num>[^\W\d_]{4,8})\s+(?P<word>cap[ií]tulo|chapter|parte|part|libro|book)"
    r"\s*(?:[.:\-\u2013\u2014]\s*(?P<title>.*))?",
    re.IGNORECASE,
)
_NAMED = re.compile(
    r"(?P<word>pr[óo]logo|ep[íi]logo|prologue|epilogue|prefacio|preface|"
    r"introducci[óo]n|introduction)\s*(?:[.:\-\u2013\u2014]\s*(?P<title>.*))?",
    re.IGNORECASE,
)
_BARE = re.compile(r"(?P<num>\d{1,3}|[IVXLCDM]{1,8})[.)]?")
_KIND_BY_WORD = {
    "cap": "chapter", "cap.": "chapter", "chap": "chapter", "chap.": "chapter",
    "capítulo": "chapter", "capitulo": "chapter", "chapter": "chapter",
    "parte": "part", "part": "part", "libro": "part", "book": "part",
    "prólogo": "prologue", "prologo": "prologue", "prologue": "prologue",
    "prefacio": "prologue", "preface": "prologue",
    "epílogo": "epilogue", "epilogo": "epilogue", "epilogue": "epilogue",
    "introducción": "introduction", "introduccion": "introduction",
    "introduction": "introduction",
}  # fmt: skip
_MAX_HEADING = 100


def _to_roman(value: int) -> str:
    out = ""
    for symbol, size in _ROMAN_FORMS:
        while value >= size:
            out += symbol
            value -= size
    return out


def _roman(text: str) -> int | None:
    """The value of a well-formed Roman numeral, else ``None``."""
    upper = text.upper()
    if not upper or any(c not in _ROMAN for c in upper):
        return None
    total = 0
    for i, c in enumerate(upper):
        value = _ROMAN[c]
        total += -value if i + 1 < len(upper) and _ROMAN[upper[i + 1]] > value else value
    return total if _to_roman(total) == upper else None


def _number(text: str) -> int | None:
    if text.isdigit():
        return int(text) or None
    return _NUMBER_WORDS.get(text.lower()) or _roman(text)


def parse_chapter_heading(text: str) -> ChapterHeading | None:
    """The heading a paragraph's ``text`` is, or ``None`` if it is not one."""
    stripped = " ".join(text.split())
    if not stripped or len(stripped) > _MAX_HEADING:
        return None
    m = _HEADING.fullmatch(stripped)
    if m is not None:
        number = _number(m["num"])
        if number is None:
            return None
        return ChapterHeading(_KIND_BY_WORD[m["word"].lower()], number, m["title"].strip())
    m = _REVERSED.fullmatch(stripped)
    if m is not None:
        number = _number(m["num"])
        if number is None:
            return None
        return ChapterHeading(_KIND_BY_WORD[m["word"].lower()], number, (m["title"] or "").strip())
    m = _NAMED.fullmatch(stripped)
    if m is not None:
        return ChapterHeading(_KIND_BY_WORD[m["word"].lower()], None, (m["title"] or "").strip())
    m = _BARE.fullmatch(stripped)
    if m is not None and m["num"] not in ("L", "C", "D", "M"):  # a lone letter is no numeral
        number = _number(m["num"])
        if number is not None:
            return ChapterHeading("numeral", number, "")
    return None


def segment_document(texts: Sequence[str], language: str = DEFAULT_LANGUAGE) -> Segmentation:
    """Segment a document given each main-story paragraph's visible text, in order."""
    _abbreviations(language)
    sentences = tuple(tuple(split_sentences(t, language)) for t in texts)
    dialogue = tuple((n, line) for n, t in enumerate(texts, 1) for line in dialogue_lines(t))
    breaks = tuple(n for n, t in enumerate(texts, 1) if is_scene_break(t))
    headings = [
        (n, h) for n, t in enumerate(texts, 1) if (h := parse_chapter_heading(t)) is not None
    ]
    chapters = tuple(
        Chapter(n, headings[i + 1][0] - 1 if i + 1 < len(headings) else len(texts), h)
        for i, (n, h) in enumerate(headings)
    )
    return Segmentation(sentences, dialogue, breaks, chapters)
