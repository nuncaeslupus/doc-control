"""Structure detection: chapters and scenes with stable ids, signals and overrides.

:func:`detect` is a pure function of per-paragraph facts (:class:`Para`), so the same
document always gives the same structure and the layout facts can come from anywhere;
:func:`paragraphs_of` reads them from an open :class:`~doc_control.model.document.Document`.

Chapters
    A non-blank paragraph starts a chapter when it carries a *primary* signal:

    ``heading_style``   a heading style of level ``<= profile.chapter_level``
    ``outline_level``   a direct outline level of level ``<= profile.chapter_level``
    ``chapter_word``    ``Capítulo 3``, ``CHAPTER IV. Title``, ``Parte II``
    ``section_name``    ``Prólogo``, ``Epílogo``, ``Introducción``
    ``numeral``         a bare number or Roman numeral, only when also centred, after a
                        page break or in a heading style (a lone ``3`` is often just text)
    ``override``        an override forced it

    Supporting signals ride along: ``centred``, ``page_break_before``, ``blank_lines``
    (empty paragraphs just before). A chapter whose text after the heading is shorter than
    ``profile.min_chapter_words`` is not a chapter (``suppressed``), unless it is forced or
    is a ``part`` heading, which holds chapters rather than text.

Scenes
    A scene starts at a chapter start, at a scene-break paragraph (``scene_marker``), or
    after ``profile.scene_blank_lines`` or more empty paragraphs (``blank_lines``; off by
    default). A break with no text on one side of it (next to a heading, another break or
    the end of the chapter) opens no scene.

Overrides
    ``{fp, kind, action, nth}``: ``force`` or ``deny`` a ``chapter`` or ``scene`` at every
    paragraph whose fingerprint is ``fp`` (or only the ``nth`` of them, counting from 1).
    They are keyed by fingerprint, not position, so they survive edits elsewhere; one that
    matches nothing (its paragraph was edited or removed) is reported in
    ``unmatched_overrides`` rather than guessed at.

Stable ids
    ``ch-<8 hex>`` hashes the heading's fingerprint and how many earlier chapters share it;
    ``sc-<8 hex>`` hashes the chapter id, the opening paragraph's fingerprint and how many
    earlier scenes of the same chapter open the same way. Neither depends on a position, a
    count of paragraphs or any other chapter's text, so editing or inserting text elsewhere
    leaves them unchanged. Editing a chapter's own heading changes its id (and its scenes').
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from doc_control.model.document import Document
from doc_control.model.segment import ChapterHeading, is_scene_break, parse_chapter_heading

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


@dataclass(frozen=True)
class Profile:
    """Per-document detection settings."""

    min_chapter_words: int = 0
    chapter_level: int = 1
    scene_blank_lines: int = 0  # 0 = empty paragraphs never start a scene
    heading_styles: bool = True
    chapter_patterns: bool = True
    scene_markers: bool = True


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
    words: int  # words after the heading
    signals: tuple[str, ...]


@dataclass(frozen=True)
class DetectedScene:
    id: str
    chapter: str | None
    first: int
    last: int
    words: int
    signals: tuple[str, ...]


@dataclass(frozen=True)
class Structure:
    chapters: tuple[DetectedChapter, ...]
    scenes: tuple[DetectedScene, ...]
    suppressed: tuple[tuple[int, Kind, str], ...]  # (paragraph, kind, reason)
    unmatched_overrides: tuple[Override, ...] = field(default=())


def paragraphs_of(doc: Document) -> list[Para]:
    """The detection facts of every paragraph of ``doc``."""
    out: list[Para] = []
    previous_break = False  # the paragraph above is empty and holds a page break
    for para in doc.index.paragraphs:
        props = doc.props[para.number - 1]
        level = doc.heading_level(para)
        source = None
        if level is not None:
            source = "outline_level" if props.outline_level is not None else "heading_style"
        out.append(
            Para(
                para.number,
                para.text,
                para.fp,
                level,
                source,
                props.centred,
                props.page_break_before or previous_break,
            )
        )
        previous_break = props.has_page_break and not para.text.strip()
    return out


def _words(text: str) -> int:
    return len(text.split())


def _id(prefix: str, *parts: str | int) -> str:
    data = "\0".join(str(p) for p in (prefix, *parts)).encode()
    return f"{'ch' if prefix == 'chapter' else 'sc'}-{hashlib.blake2s(data).hexdigest()[:8]}"


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
    blank = [not p.text.strip() for p in paras]
    blank_before = [0] * n
    run = 0
    for i in range(n):
        blank_before[i] = 0 if blank[i] else run
        run = run + 1 if blank[i] else 0

    actions, unmatched = _resolve_overrides(paras, blank, overrides)
    suppressed: list[tuple[int, Kind, str]] = []
    chapters = _chapter_starts(paras, blank, blank_before, profile, actions, suppressed)
    starts = sorted(chapters)

    scene_opener_signals = _scene_openers(paras, blank, blank_before, profile, actions, chapters)
    for i in scene_opener_signals:
        if "denied" in scene_opener_signals[i]:
            suppressed.append((paras[i].number, "scene", "override"))
    scene_opener_signals = {i: s for i, s in scene_opener_signals.items() if "denied" not in s}

    def content(i: int) -> bool:
        return not blank[i] and i not in chapters and not is_scene_break(paras[i].text)

    def words(lo: int, hi: int) -> int:
        return sum(_words(paras[i].text) for i in range(lo, hi) if content(i))

    # Chapters below the minimum word count fall back into the text before them.
    kept: list[int] = []
    for pos, i in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else n
        signals, heading = chapters[i]
        body = words(i + 1, end)
        if (
            profile.min_chapter_words
            and body < profile.min_chapter_words
            and "override" not in signals
            and heading.kind != "part"
        ):
            suppressed.append((paras[i].number, "chapter", "below_minimum_words"))
            continue
        kept.append(i)
    kept_set = set(kept)

    detected_chapters: list[DetectedChapter] = []
    chapter_ids: dict[int, str] = {}
    seen_chapters: dict[tuple[str, ...], int] = defaultdict(int)
    for pos, i in enumerate(kept):
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
                words(i + 1, end),
                tuple(signals),
            )
        )

    # Scenes: regions are the text before the first chapter, then one per chapter.
    regions: list[tuple[str | None, int, int]] = []
    first_text = next((i for i in range(n) if not blank[i]), n)
    lead_end = kept[0] if kept else n
    if first_text < lead_end:
        regions.append((None, first_text, lead_end))
    for pos, i in enumerate(kept):
        regions.append((chapter_ids[i], i, kept[pos + 1] if pos + 1 < len(kept) else n))

    scenes: list[DetectedScene] = []
    for owner, lo, hi in regions:
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
                    ("chapter_start",) if lo in kept_set else ("document_start",)
                )
            else:
                opening = tuple(scene_opener_signals[o])
            sid = _unique("scene", (owner or "-", paras[o].fp), seen_scenes)
            scenes.append(
                DetectedScene(
                    sid, owner, paras[o].number, paras[end - 1].number, words(o, end), opening
                )
            )
    suppressed.sort()
    return Structure(tuple(detected_chapters), tuple(scenes), tuple(suppressed), unmatched)


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
        primary = _primary_signals(para, profile)
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
        heading = parse_chapter_heading(para.text) or ChapterHeading("chapter", None, "")
        found[i] = (signals, heading)
    return found


def _primary_signals(para: Para, profile: Profile) -> list[str]:
    if is_scene_break(para.text):
        return []
    signals: list[str] = []
    styled = (
        profile.heading_styles
        and para.heading_level is not None
        and para.heading_level <= profile.chapter_level
    )
    if styled and para.heading_source:
        signals.append(para.heading_source)
    heading = parse_chapter_heading(para.text) if profile.chapter_patterns else None
    if heading is not None:
        if heading.kind in ("chapter", "part"):
            signals.append("chapter_word")
        elif heading.kind == "numeral":
            if styled or para.centred or para.page_break_before:
                signals.append("numeral")
        else:
            signals.append("section_name")
    return signals


def _scene_openers(
    paras: Sequence[Para],
    blank: Sequence[bool],
    blank_before: Sequence[int],
    profile: Profile,
    actions: dict[tuple[str, int], str],
    chapters: dict[int, tuple[list[str], ChapterHeading]],
) -> dict[int, list[str]]:
    """Paragraphs that would open a scene; one a deny override removed carries ``denied``."""
    out: dict[int, list[str]] = {}
    for i, para in enumerate(paras):
        if blank[i] or i in chapters:
            continue
        signals: list[str] = []
        if profile.scene_markers and is_scene_break(para.text):
            signals.append("scene_marker")
        elif profile.scene_blank_lines and blank_before[i] >= profile.scene_blank_lines:
            signals.append("blank_lines")
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
