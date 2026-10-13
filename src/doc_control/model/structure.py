"""Structure detection: chapters and scenes with stable ids, signals and overrides.

:func:`detect` is a pure function of per-paragraph facts (:class:`Para`), so the same
document always gives the same structure and the layout facts can come from anywhere;
:func:`paragraphs_of` reads them from an open :class:`~doc_control.model.document.Document`.

Chapters
    A non-blank paragraph starts a chapter when it carries a *primary* signal:

    ``heading_style``   a heading style of level ``<= profile.chapter_level`` (``auto``:
                        the shallowest level in use, or the next when the shallowest has
                        at most ``AUTO_FEW`` headings over twice as many)
    ``outline_level``   a direct outline level of level ``<= profile.chapter_level``
    ``chapter_word``    ``Capítulo 3``, ``CHAPTER IV. Title``, ``Parte II``
    ``section_name``    ``Prólogo``, ``Epílogo``, ``Introducción``
    ``numeral``         a bare number or Roman numeral, only when also centred, after a
                        page break or in a heading style (a lone ``3`` is often just text)
    ``chapter_style``   a body paragraph whose style name holds a chapter word
                        (``Normal inicio capítulo``): untitled chapters marked by style;
                        the paragraph is the chapter's first text. A style on more than
                        a third of the text paragraphs is a body style and marks none;
                        so does one that only ever follows a heading, once per heading
                        (it opens titled chapters)
    ``isolated``        a short line (``ISOLATED_WORDS``, no final punctuation, not
                        dialogue) with ``profile.chapter_blank_lines`` or more empty
                        paragraphs above and one below (or below its subtitle line): a
                        plain-text heading; off by default. It is also layout for numerals
    ``override``        an override forced it

    Numerals may be decorated (``-I-``, ``— 3 —``, ``[IV]``). An untitled heading (a
    numeral, ``Capítulo 3``) takes the short line right under it as its title
    (``heading_last``): at most ``SUBTITLE_WORDS`` words, no final punctuation, no dialogue.

    Supporting signals ride along: ``centred``, ``page_break_before``, ``blank_lines``
    (empty paragraphs just before). A chapter whose text after the heading is shorter than
    ``profile.min_chapter_words`` is not a chapter (``suppressed``), unless it is forced or
    is a ``part`` heading, which holds chapters rather than text.

Parts
    A heading-like start (any primary signal but ``chapter_style``) is a *part* when
    chapters start inside it before the next heading-like start: style-marked chapters,
    or any chapter after a ``Parte II`` / ``Libro I`` heading. A part holds the chapters up
    to the next part; it is listed in ``parts``, not ``chapters``, and each chapter names
    its part. Text before the first part or chapter is the *front* matter (title page,
    dedication), reported as ``front``.

Scenes
    A scene starts at a chapter start, at a scene-break paragraph (``scene_marker``),
    after ``profile.scene_blank_lines`` or more empty paragraphs (``blank_lines``; off by
    default), or at a paragraph whose style name holds a scene word as a whole word
    (``Normal tras pausa``: ``scene_style``) unless it follows a heading or a scene-break
    paragraph, or the style is a body style (as for ``chapter_style``). A break with no
    text on one side of it (next to a heading, another break or the end of the chapter)
    opens no scene.

Overrides
    ``{fp, kind, action, nth}``: ``force`` or ``deny`` a ``chapter`` or ``scene`` at every
    paragraph whose fingerprint is ``fp`` (or only the ``nth`` of them, counting from 1).
    They are keyed by fingerprint, not position, so they survive edits elsewhere; one that
    matches nothing (its paragraph was edited or removed) is reported in
    ``unmatched_overrides`` rather than guessed at.

Stable ids
    ``ch-<8 hex>`` hashes the heading's fingerprint and how many earlier chapters share it;
    ``sc-<8 hex>`` hashes the chapter id and the fingerprint of the scene's first text
    paragraph (the one after its break), plus a count only when two scenes of a chapter
    begin with identical text; a chapter's first scene is keyed on the chapter alone.
    Neither depends on a position, a
    count of paragraphs or any other chapter's text, so editing or inserting text elsewhere
    leaves them unchanged. Editing a chapter's own heading changes its id (and its scenes').
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from doc_control.model.document import Document
from doc_control.model.layout import _break_only, blank_before, centred, is_empty, page_break_before
from doc_control.model.segment import (
    ChapterHeading,
    is_scene_break,
    number_word,
    parse_chapter_heading,
)

#: Words that mark a chapter-opening paragraph style, matched in the style name folded to
#: lower case without accents (any language: a style name rarely says "chapter" by chance).
CHAPTER_STYLE_WORDS: tuple[str, ...] = (
    "capitulo",
    "chapter",
    "chapitre",
    "kapitel",
    "capitolo",
    "hoofdstuk",
)

#: Words that mark a scene-opening paragraph style (``Normal tras pausa``, ``After break``),
#: matched at the start of a word of the style name folded like ``CHAPTER_STYLE_WORDS``.
SCENE_STYLE_WORDS: tuple[str, ...] = ("after", "tras", "despues")

#: A chapter-word style marking more than one text paragraph in this many is a body style.
BODY_STYLE_SHARE = 3

#: A heading subtitle's most words.
SUBTITLE_WORDS = 8

_DECORATION = "-\u2013\u2014[]()*\u00b7~ \t"  # around a decorated numeral: -I-, — 3 —, [IV]
_CLOSERS = "\"'\u00bb\u201d\u2019)]"
_TERMINAL = ".,;:!?\u2026"
_TITLED_KINDS = ("chapter", "part", "numeral")  # a Prólogo's next line is a dateline, say
_DIALOGUE = '-\u2013\u2014\u00ab"\u201c'

Kind = Literal["chapter", "scene"]
Action = Literal["force", "deny"]
KINDS: tuple[str, ...] = ("chapter", "scene")
ACTIONS: tuple[str, ...] = ("force", "deny")


@dataclass(frozen=True)
class Para:
    """What detection needs to know about one paragraph (``number`` is 1-based)."""

    number: int
    text: str
    fp: str
    heading_level: int | None = None  # 1-9 when styled or outlined as a heading
    heading_source: str | None = None  # "outline_level" or "heading_style"
    centred: bool = False
    page_break_before: bool = False
    blank_before: int | None = None  # empty paragraphs just above; None = count from the texts
    empty: bool | None = None  # None = no visible text
    style_name: str | None = None  # the paragraph style's name, for ``chapter_style``


@dataclass(frozen=True)
class Profile:
    """Per-document detection settings."""

    min_chapter_words: int = 0
    chapter_level: int | Literal["auto"] = 1
    chapter_blank_lines: int = 0  # 0 = blank lines alone never make a heading
    scene_blank_lines: int = 0  # 0 = empty paragraphs never start a scene
    heading_styles: bool = True
    chapter_patterns: bool = True
    scene_markers: bool = True
    language: str = "es"  # "es" accepts a lone "I" as a numeral; others need more signals
    chapter_styles: bool = True
    chapter_style_words: tuple[str, ...] = CHAPTER_STYLE_WORDS
    scene_styles: bool = True
    scene_style_words: tuple[str, ...] = SCENE_STYLE_WORDS
    heading_subtitles: bool = True


@dataclass(frozen=True)
class Override:
    fp: str
    kind: Kind
    action: Action
    nth: int | None = None


@dataclass(frozen=True)
class DetectedChapter:
    id: str
    first: int  # paragraph numbers, inclusive
    last: int
    kind: str  # chapter, part, prologue, epilogue, introduction, numeral
    number: int | None
    words: int  # words after the heading (with the first paragraph for ``chapter_style``)
    signals: tuple[str, ...]
    part: str | None = None  # the id of the part that holds it
    heading_last: int | None = None  # the heading's last paragraph (its subtitle); None: no heading


@dataclass(frozen=True)
class DetectedPart:
    id: str  # ``pt-<8 hex>``
    first: int  # its heading
    last: int  # the last paragraph of its last chapter
    kind: str  # part, or the heading's kind when a chapter-like heading holds chapters
    number: int | None
    words: int  # words between the heading and its first chapter
    chapters: int
    signals: tuple[str, ...]
    heading_last: int | None = None


@dataclass(frozen=True)
class DetectedFront:
    first: int
    last: int
    words: int


@dataclass(frozen=True)
class DetectedScene:
    id: str
    chapter: str | None
    first: int
    last: int
    words: int
    signals: tuple[str, ...]
    part: str | None = None


@dataclass(frozen=True)
class Structure:
    chapters: tuple[DetectedChapter, ...]
    scenes: tuple[DetectedScene, ...]
    suppressed: tuple[tuple[int, Kind, str], ...]  # (paragraph, kind, reason)
    unmatched_overrides: tuple[Override, ...] = field(default=())
    parts: tuple[DetectedPart, ...] = field(default=())
    front: DetectedFront | None = None


def paragraphs_of(doc: Document) -> list[Para]:
    """The detection facts of every paragraph of ``doc``, from :mod:`doc_control.model.layout`
    (computed once per open document)."""
    return doc.memo("structure-paragraphs", lambda: _paragraphs_of(doc))


def detect_in(doc: Document, profile: Profile, overrides: Sequence[Override]) -> Structure:
    """:func:`detect` over ``doc``, once per open document, profile and overrides."""
    key = ("structure", profile, tuple(overrides))
    return doc.memo(key, lambda: detect(paragraphs_of(doc), profile, overrides))


def _paragraphs_of(doc: Document) -> list[Para]:
    out: list[Para] = []
    pending = False  # a page break was seen and only empty paragraphs have followed
    for para in doc.index.paragraphs:
        props = doc.props[para.number - 1]
        level = doc.heading_level(para)
        source = None
        if level is not None:
            source = "outline_level" if props.outline_level is not None else "heading_style"
        empty = is_empty(doc, para)
        style_id = doc.style_id(para)
        style = doc.styles.get(style_id) if style_id is not None else None
        breaks = page_break_before(doc, para) or pending
        pending = empty and (breaks or _break_only(doc, para))
        out.append(
            Para(
                para.number,
                para.text,
                para.fp,
                level,
                source,
                centred(doc, para),
                breaks,
                blank_before(doc, para),
                empty,
                style.name if style is not None else style_id,
            )
        )
    return out


def _words(text: str) -> int:
    return len(text.split())


def _id(prefix: str, *parts: str | int) -> str:
    data = "\0".join(str(p) for p in (prefix, *parts)).encode()
    short = {"chapter": "ch", "part": "pt"}.get(prefix, "sc")
    return f"{short}-{hashlib.blake2s(data).hexdigest()[:8]}"


def _unique(prefix: str, key: tuple[str, ...], seen: dict[tuple[str, ...], int]) -> str:
    """The id for ``key``, counting earlier uses of the same key."""
    count = seen[key]
    seen[key] += 1
    return _id(prefix, *key, count)


def detect(
    paras: Sequence[Para],
    profile: Profile = Profile(),  # noqa: B008 - frozen, shared on purpose
    overrides: Sequence[Override] = (),
) -> Structure:
    n = len(paras)
    # A heading with no text (auto-numbered) is a heading, not a blank line.
    blank = [
        (p.empty if p.empty is not None else not p.text.strip()) and p.heading_level is None
        for p in paras
    ]
    blank_before = [0] * n
    run = 0
    for i, para in enumerate(paras):
        blank_before[i] = para.blank_before if para.blank_before is not None else run
        if para.blank_before is None and blank[i]:
            run += 1
        elif para.blank_before is None:
            run = 0

    if profile.chapter_level == "auto":
        profile = replace(profile, chapter_level=_auto_level(paras, blank))
    actions, unmatched = _resolve_overrides(paras, blank, overrides)
    suppressed: list[tuple[int, Kind, str]] = []
    chapters = _chapter_starts(paras, blank, blank_before, profile, actions, suppressed)
    _drop_style_openers(paras, blank, chapters, suppressed)
    starts = sorted(chapters)
    subtitles = _subtitles(paras, blank, chapters) if profile.heading_subtitles else {}
    subtitle_set = set(subtitles.values())

    scene_opener_signals = _scene_openers(
        paras, blank, blank_before, profile, actions, chapters, subtitle_set
    )
    for i in scene_opener_signals:
        if "denied" in scene_opener_signals[i]:
            suppressed.append((paras[i].number, "scene", "override"))
    scene_opener_signals = {i: s for i, s in scene_opener_signals.items() if "denied" not in s}

    # A style-marked chapter starts at its first text paragraph, which is content.
    body_start = {i for i, (signals, _) in chapters.items() if _style_only(signals)}

    def content(i: int) -> bool:
        text = paras[i].text
        return (
            bool(text.strip())
            and (i not in chapters or i in body_start)
            and i not in subtitle_set
            and not is_scene_break(text)
        )

    def words(lo: int, hi: int) -> int:
        return sum(_words(paras[i].text) for i in range(lo, hi) if content(i))

    holders = _parts(starts, chapters, body_start)
    # Chapters below the minimum word count fall back into the text before them.
    kept: list[int] = []
    for pos, i in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else n
        signals, heading = chapters[i]
        body = words(i if i in body_start else i + 1, end)
        if (
            profile.min_chapter_words
            and body < profile.min_chapter_words
            and "override" not in signals
            and heading.kind != "part"
            and i not in holders
        ):
            suppressed.append((paras[i].number, "chapter", "below_minimum_words"))
            continue
        kept.append(i)
    held = _parts(kept, chapters, body_start)
    # A heading exempted as a part whose chapters were all dropped is a chapter again.
    for i in [i for i in kept if i in holders and i not in held]:
        signals, heading = chapters[i]
        end = next((k for k in kept if k > i), n)
        if (
            profile.min_chapter_words
            and words(i + 1, end) < profile.min_chapter_words
            and "override" not in signals
            and heading.kind != "part"
        ):
            suppressed.append((paras[i].number, "chapter", "below_minimum_words"))
            kept.remove(i)
    held = _parts(kept, chapters, body_start)
    kept_set = set(kept)
    part_starts = sorted(held)
    part_set = set(part_starts)
    owner_of = {k: i for i, children in held.items() for k in children}

    def unit_end(i: int) -> int:
        """Where the unit starting at kept start ``i`` ends (exclusive)."""
        pos = kept.index(i)
        return kept[pos + 1] if pos + 1 < len(kept) else n

    detected_parts: list[DetectedPart] = []
    part_ids: dict[int, str] = {}
    seen_parts: dict[tuple[str, ...], int] = defaultdict(int)
    for i in part_starts:
        children = held[i]
        end = unit_end(children[-1])
        signals, heading = chapters[i]
        pid = _unique("part", (paras[i].fp,), seen_parts)
        part_ids[i] = pid
        detected_parts.append(
            DetectedPart(
                pid,
                paras[i].number,
                paras[end - 1].number,
                heading.kind,
                heading.number,
                words(i + 1, children[0]),
                len(children),
                tuple(signals),
                paras[subtitles.get(i, i)].number,
            )
        )

    def part_of(i: int) -> str | None:
        if i in part_set:
            return part_ids[i]
        return part_ids[owner_of[i]] if i in owner_of else None

    detected_chapters: list[DetectedChapter] = []
    chapter_ids: dict[int, str] = {}
    seen_chapters: dict[tuple[str, ...], int] = defaultdict(int)
    for pos, i in enumerate(kept):
        if i in part_set:
            continue
        end = kept[pos + 1] if pos + 1 < len(kept) else n
        signals, heading = chapters[i]
        cid = _unique("chapter", (paras[i].fp,), seen_chapters)
        chapter_ids[i] = cid
        detected_chapters.append(
            DetectedChapter(
                cid,
                paras[i].number,
                paras[end - 1].number,
                heading.kind,
                heading.number,
                words(i if i in body_start else i + 1, end),
                tuple(signals),
                part_of(i),
                None if i in body_start else paras[subtitles.get(i, i)].number,
            )
        )

    # Scenes: regions are the text before the first chapter, then one per chapter.
    regions: list[tuple[str | None, int, int]] = []
    first_text = next((i for i in range(n) if not blank[i]), n)
    lead_end = kept[0] if kept else n
    front = None
    if first_text < lead_end:
        regions.append((None, first_text, lead_end))
        if kept:
            front = DetectedFront(
                paras[first_text].number, paras[lead_end - 1].number, words(first_text, lead_end)
            )
    for pos, i in enumerate(kept):
        regions.append((chapter_ids.get(i), i, kept[pos + 1] if pos + 1 < len(kept) else n))

    scenes: list[DetectedScene] = []
    for owner, lo, hi in regions:
        if lo in part_set and not any(content(i) for i in range(lo, hi)):
            continue  # a part heading straight before its first chapter has no scene
        openers = [i for i in sorted(scene_opener_signals) if lo < i < hi and i not in kept_set]
        taken = [lo]
        for pos, o in enumerate(openers):
            forced = "override" in scene_opener_signals[o]
            nxt = openers[pos + 1] if pos + 1 < len(openers) else hi
            before = any(content(i) for i in range(taken[-1], o))
            after = any(content(i) for i in range(o, nxt))
            if forced or (before and after):
                taken.append(o)
            else:
                suppressed.append((paras[o].number, "scene", "no_text_between"))
        seen_scenes: dict[tuple[str, ...], int] = defaultdict(int)
        for pos, o in enumerate(taken):
            end = taken[pos + 1] if pos + 1 < len(taken) else hi
            if o == lo:
                opening: tuple[str, ...] = (
                    ("document_start",)
                    if lo not in kept_set
                    else ("part_start",)
                    if lo in part_set
                    else ("chapter_start",)
                )
            else:
                opening = tuple(scene_opener_signals[o])
            if o == lo:
                key = "start"
            else:  # the first text of the scene, so adding scenes elsewhere moves no id
                text_at = next((i for i in range(o, end) if content(i)), o)
                key = paras[text_at].fp
            region_key = owner or (part_ids.get(lo) if lo in part_set else None) or "-"
            sid = _unique("scene", (region_key, key), seen_scenes)
            scenes.append(
                DetectedScene(
                    sid,
                    owner,
                    paras[o].number,
                    paras[end - 1].number,
                    words(o, end),
                    opening,
                    part_of(lo) if lo in kept_set else None,
                )
            )
    suppressed.sort()
    return Structure(
        tuple(detected_chapters),
        tuple(scenes),
        tuple(suppressed),
        unmatched,
        tuple(detected_parts),
        front,
    )


def _style_only(signals: Sequence[str]) -> bool:
    """A start whose only primary signal is ``chapter_style`` (overrides aside)."""
    return "chapter_style" in signals and not any(s in _HEADING_SIGNALS for s in signals)


_HEADING_SIGNALS = frozenset(
    {"heading_style", "outline_level", "chapter_word", "section_name", "numeral", "isolated"}
)


def _parts(
    starts: Sequence[int],
    chapters: dict[int, tuple[list[str], ChapterHeading]],
    body_start: set[int],
) -> dict[int, list[int]]:
    """Each start that holds chapters, with the starts it holds.

    A heading (not a section name like ``Prólogo``) holds the style-marked chapters that
    follow it; a ``Parte``/``Libro`` heading holds the chapters up to the next part or
    section (``Epílogo``).
    """
    out: dict[int, list[int]] = {}
    inside_part: set[int] = set()  # held by a Parte heading, so not a part itself
    for pos, i in sorted(enumerate(starts), key=lambda e: (chapters[e[1]][1].kind != "part", e[0])):
        kind = chapters[i][1].kind
        if i in body_start or i in inside_part or kind not in ("chapter", "part", "numeral"):
            continue
        children: list[int] = []
        for k in starts[pos + 1 :]:
            if kind == "part":
                # up to the next part, or a section such as an epilogue
                if k not in body_start and chapters[k][1].kind not in ("chapter", "numeral"):
                    break
            elif k not in body_start:
                break
            children.append(k)
        if children:
            out[i] = children
            if kind == "part":
                inside_part.update(children)
    return dict(sorted(out.items()))


def _resolve_overrides(
    paras: Sequence[Para], blank: Sequence[bool], overrides: Sequence[Override]
) -> tuple[dict[tuple[str, int], str], tuple[Override, ...]]:
    by_fp: dict[str, list[int]] = defaultdict(list)
    for i, para in enumerate(paras):
        if not blank[i]:
            by_fp[para.fp].append(i)
    actions: dict[tuple[str, int], str] = {}
    unmatched: list[Override] = []
    for o in overrides:
        hits = by_fp.get(o.fp, [])
        if o.nth is not None:
            hits = hits[o.nth - 1 : o.nth]
        for i in hits:
            actions[(o.kind, i)] = o.action  # a later override wins
        if not hits:
            unmatched.append(o)
    return actions, tuple(unmatched)


def _chapter_starts(
    paras: Sequence[Para],
    blank: Sequence[bool],
    blank_before: Sequence[int],
    profile: Profile,
    actions: dict[tuple[str, int], str],
    suppressed: list[tuple[int, Kind, str]],
) -> dict[int, tuple[list[str], ChapterHeading]]:
    found: dict[int, tuple[list[str], ChapterHeading]] = {}
    for i, para in enumerate(paras):
        if blank[i]:
            continue
        primary = _primary_signals(para, profile, _isolated(paras, blank, blank_before, i, profile))
        action = actions.get(("chapter", i))
        if action == "deny":
            if primary:
                suppressed.append((para.number, "chapter", "override"))
            continue
        if action == "force":
            primary = ["override", *primary]
        elif not primary:
            continue
        signals = [*primary]
        if para.centred:
            signals.append("centred")
        if para.page_break_before:
            signals.append("page_break_before")
        if blank_before[i]:
            signals.append("blank_lines")
        heading = _parse_heading(para.text)
        if heading is None and (value := number_word(para.text)) is not None:
            heading = ChapterHeading("numeral", value, "")
        heading = heading or ChapterHeading("chapter", None, "")
        found[i] = (signals, heading)
    return found


def _primary_signals(para: Para, profile: Profile, isolated: bool = False) -> list[str]:
    if is_scene_break(para.text):
        return []
    signals: list[str] = []
    if profile.chapter_styles and para.style_name and para.heading_level is None:
        folded = _fold(para.style_name)
        words = (_fold(w.strip()) for w in profile.chapter_style_words)
        if any(word and word in folded for word in words):
            signals.append("chapter_style")
    level = profile.chapter_level
    styled = (
        profile.heading_styles
        and para.heading_level is not None
        and isinstance(level, int)
        and para.heading_level <= level
    )
    if styled and para.heading_source:
        signals.append(para.heading_source)
    if isolated and not styled:
        signals.append("isolated")
    heading = _parse_heading(para.text) if profile.chapter_patterns else None
    laid_out = styled or isolated or para.centred or para.page_break_before
    if heading is not None:
        if heading.kind in ("chapter", "part"):
            signals.append("chapter_word")
        elif heading.kind == "numeral":
            lone_i = para.text.strip().strip(_DECORATION).rstrip(".)") == "I"
            if lone_i and profile.language != "es":  # "I" is a pronoun in English
                laid_out = styled or isolated or (para.centred and para.page_break_before)
            if laid_out:
                signals.append("numeral")
        else:
            signals.append("section_name")
    elif profile.chapter_patterns and laid_out and number_word(para.text) is not None:
        signals.append("numeral")  # Uno, Dos… Veinte; Primera…
    return signals


def _drop_style_openers(
    paras: Sequence[Para],
    blank: Sequence[bool],
    chapters: dict[int, tuple[list[str], ChapterHeading]],
    suppressed: list[tuple[int, Kind, str]],
) -> None:
    """Drop ``chapter_style`` starts that are really a titled chapter's opening paragraph.

    When every style-marked start comes right after a heading start (blank lines aside)
    and no heading is followed by two of them, the style is the house style for a
    chapter's first paragraph, not a chapter marker.
    """
    styled = sorted(i for i, (signals, _) in chapters.items() if _style_only(signals))
    if not styled:
        return
    text = sum(1 for b in blank if not b)
    if len(styled) >= 3 and len(styled) * BODY_STYLE_SHARE > text:  # a body style
        for i in styled:
            if "override" not in chapters[i][0]:
                del chapters[i]
                suppressed.append((paras[i].number, "chapter", "body_style"))
        return
    headed: dict[int, int] = defaultdict(int)  # heading start -> styled starts after it
    directly = True
    for i in styled:
        prev = next((k for k in range(i - 1, -1, -1) if not blank[k]), None)
        if prev is None or prev not in chapters or prev in styled:
            directly = False
        owner = max((k for k in chapters if k < i and k not in styled), default=None)
        if owner is not None:
            headed[owner] += 1
    if directly and all(count == 1 for count in headed.values()):
        for i in styled:
            if "override" not in chapters[i][0]:
                del chapters[i]
                suppressed.append((paras[i].number, "chapter", "opening_paragraph_style"))


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _scene_openers(
    paras: Sequence[Para],
    blank: Sequence[bool],
    blank_before: Sequence[int],
    profile: Profile,
    actions: dict[tuple[str, int], str],
    chapters: dict[int, tuple[list[str], ChapterHeading]],
    subtitles: set[int],
) -> dict[int, list[str]]:
    """Paragraphs that would open a scene; one a deny override removed carries ``denied``."""
    styled = _scene_styled(paras, blank, profile)
    out: dict[int, list[str]] = {}
    for i, para in enumerate(paras):
        if blank[i] or i in chapters or i in subtitles:
            continue
        signals: list[str] = []
        if profile.scene_markers and is_scene_break(para.text):
            signals.append("scene_marker")
        elif profile.scene_blank_lines and blank_before[i] >= profile.scene_blank_lines:
            signals.append("blank_lines")
        elif i in styled:
            prev = next((k for k in range(i - 1, -1, -1) if not blank[k]), None)
            heading = prev is not None and (
                (prev in chapters and not _style_only(chapters[prev][0])) or prev in subtitles
            )
            marked = prev is not None and profile.scene_markers and is_scene_break(paras[prev].text)
            if prev is not None and not heading and not marked:
                signals.append("scene_style")
        action = actions.get(("scene", i))
        if action == "deny":
            if signals:
                out[i] = ["denied"]
            continue
        if action == "force":
            signals.insert(0, "override")
        elif not signals:
            continue
        if para.centred and "scene_marker" in signals:
            signals.append("centred")
        if blank_before[i] and "blank_lines" not in signals:
            signals.append("blank_lines")
        out[i] = signals
    return out


def _scene_styled(paras: Sequence[Para], blank: Sequence[bool], profile: Profile) -> set[int]:
    """Text paragraphs whose style name marks a scene start, unless it is a body style."""
    if not profile.scene_styles:
        return set()
    words = [w for w in (_fold(w.strip()) for w in profile.scene_style_words) if w]
    if not words:
        return set()
    wanted = set(words)
    marking: dict[str, bool] = {}
    by_style: dict[str, list[int]] = defaultdict(list)
    for i, para in enumerate(paras):
        name = para.style_name
        if blank[i] or not name or para.heading_level is not None:
            continue
        if name not in marking:
            split = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)  # AfterBreak: After Break
            marking[name] = not wanted.isdisjoint(re.findall(r"[a-z0-9]+", _fold(split)))
        if marking[name]:
            by_style[name].append(i)
    text = sum(1 for b in blank if not b)
    out: set[int] = set()
    for found in by_style.values():
        if not (len(found) >= 3 and len(found) * BODY_STYLE_SHARE > text):
            out.update(found)
    return out


def _parse_heading(text: str) -> ChapterHeading | None:
    """:func:`parse_chapter_heading`, also through a decorated numeral (``-I-``, ``— 3 —``)."""
    heading = parse_chapter_heading(text)
    if heading is None:
        core = text.strip().strip(_DECORATION)
        if core and core != text.strip():
            heading = parse_chapter_heading(core)
    return heading


def _subtitles(
    paras: Sequence[Para],
    blank: Sequence[bool],
    chapters: dict[int, tuple[list[str], ChapterHeading]],
) -> dict[int, int]:
    """Untitled headings (``-I-``, ``Capítulo 3``) with the short title line right under them."""
    out: dict[int, int] = {}
    for i, (signals, _) in chapters.items():
        j = i + 1
        if _style_only(signals) or j >= len(paras) or blank[j] or j in chapters:
            continue
        text = paras[i].text
        parsed = _parse_heading(text)
        untitled = (
            parsed is not None and not parsed.title and parsed.kind in _TITLED_KINDS
        ) or number_word(text) is not None
        if untitled and _is_subtitle(paras[j].text):
            out[i] = j
    return out


def _is_subtitle(text: str) -> bool:
    words = text.split()
    if not 1 <= len(words) <= SUBTITLE_WORDS or is_scene_break(text):
        return False
    tail = text.rstrip().rstrip(_CLOSERS)
    if not tail or tail[-1] in _TERMINAL or text.lstrip()[0] in _DIALOGUE:
        return False
    return _parse_heading(text) is None


#: A heading found by blank lines alone has at most this many words.
ISOLATED_WORDS = 12

#: ``chapter_level: "auto"`` skips a shallowest level with at most this many headings when
#: the next level used has at least twice as many (parts above chapters, a title).
AUTO_FEW = 5


def _isolated(
    paras: Sequence[Para],
    blank: Sequence[bool],
    blank_before: Sequence[int],
    i: int,
    profile: Profile,
) -> bool:
    """A short line set off by ``profile.chapter_blank_lines`` empty paragraphs above and one
    below (or below its subtitle line): a plain-text heading."""
    need = profile.chapter_blank_lines
    if not need or blank_before[i] < need or is_scene_break(paras[i].text):
        return False
    text = paras[i].text.strip()
    if len(text.split()) > ISOLATED_WORDS:
        return False
    named = _parse_heading(text) is not None or number_word(text) is not None
    if not named and (text.rstrip(_CLOSERS)[-1:] in tuple(_TERMINAL) or text[0] in _DIALOGUE):
        return False  # a short sentence or a line of dialogue set apart
    after = i + 1
    if after < len(paras) and not blank[after] and _is_subtitle(paras[after].text):
        after += 1
    return after >= len(paras) or blank[after]


def _auto_level(paras: Sequence[Para], blank: Sequence[bool]) -> int:
    """The shallowest heading level in use, or the next one used when the shallowest holds
    only a few headings over many (``AUTO_FEW``)."""
    counts: dict[int, int] = defaultdict(int)
    for i, para in enumerate(paras):
        if para.heading_level is not None and para.text.strip() and not blank[i]:
            counts[para.heading_level] += 1
    levels = sorted(counts)
    if not levels:
        return 1
    few = len(levels) > 1 and counts[levels[0]] <= AUTO_FEW
    if few and counts[levels[1]] >= 2 * counts[levels[0]]:
        return levels[1]
    return levels[0]
