"""Paragraphs of several stories in reading order (STORIES T4).

The body in order, each note's paragraphs right after the body paragraph that holds its
reference mark, then each section's headers and footers. Rows outside the body carry
what tells their story apart: ``story`` and, for a note, ``note`` (its ``w:id``, the
number Word shows and the body address of its mark); for a header or footer
``section``, ``kind`` and ``used_by_sections``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from doc_control.model.document import Document
from doc_control.model.index import Paragraph
from doc_control.model.parts import W
from doc_control.model.stories import Story, list_stories, section_properties

#: The request field that opts a read into other stories.
STORIES_PROPERTY: dict[str, Any] = {
    "type": "array",
    "items": {"enum": ["body", "notes", "headers"]},
    "description": (
        'Stories to read (default ["body"]): "notes" adds footnotes and endnotes, each '
        'after the paragraph holding its mark; "headers" adds headers and footers, after '
        "the body. Their addresses carry a prefix (fn3/p1#…, hdr1.default/p1#…)."
    ),
}


@dataclass(frozen=True)
class Row:
    view: Document  # the document seen through the row's story
    para: Paragraph
    fields: dict[str, Any]  # story facts to add to the row ({} for the body)


def kinds_of(request: dict[str, Any]) -> tuple[str, ...]:
    return tuple(request.get("stories", ["body"]))


def _roman(n: int) -> str:
    out = ""
    for value, digits in (
        (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
        (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
    ):  # fmt: skip
        while n >= value:
            out, n = out + digits, n - value
    return out


def _letter(n: int) -> str:
    """Word's letter numbering: a…z, then aa…zz."""
    return chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)


def _shown(n: int, fmt: str) -> str:
    if fmt == "lowerRoman":
        return _roman(n)
    if fmt == "upperRoman":
        return _roman(n).upper()
    if fmt == "lowerLetter":
        return _letter(n)
    if fmt == "upperLetter":
        return _letter(n).upper()
    return str(n)


def _note_setting(doc: Document, kind: str, child: str) -> str | None:
    """A ``w:footnotePr``/``w:endnotePr`` value: a section's, else the settings part's."""
    from doc_control.model.parts import OFFICE_REL, parse_part, related_part

    holders = list(section_properties(doc.pkg))
    settings = related_part(doc.pkg, OFFICE_REL + "settings", "word/settings.xml")
    if settings is not None:
        holders.append(parse_part(doc.pkg, settings))
    for holder in holders:
        pr = holder.find(W + f"{kind}Pr")
        found = pr.find(W + child) if pr is not None else None
        if found is not None:
            return found.get(W + "val")
    return None


def _note_numbers(doc: Document) -> dict[str, dict[str, Any]]:
    """Per note key, the number Word shows and the body address of its mark.

    ponytail: one number format and restart rule per kind (the first a section or the
    settings part gives), not per section."""
    restart = _note_setting(doc, "footnote", "numRestart") == "eachSect"
    formats = {
        "footnote": _note_setting(doc, "footnote", "numFmt") or "decimal",
        "endnote": _note_setting(doc, "endnote", "numFmt") or "lowerRoman",  # Word's default
    }
    out: dict[str, dict[str, Any]] = {}
    counters = {"footnote": 0, "endnote": 0}
    for para in doc.index.paragraphs:
        elem = doc.paragraph_element(para)
        for run in elem.iter(W + "r"):
            for kind in ("footnote", "endnote"):
                ref = run.find(W + f"{kind}Reference")
                if ref is None:
                    continue
                ident = ref.get(W + "id", "")
                key = ("fn" if kind == "footnote" else "en") + ident
                if ref.get(W + "customMarkFollows") in ("1", "true", "on"):
                    shown = "".join(t.text or "" for t in run.iter(W + "t"))
                else:
                    counters[kind] += 1
                    shown = _shown(counters[kind], formats[kind])
                out.setdefault(key, {"id": int(ident), "number": shown, "of": str(para.address)})
        ppr = elem.find(W + "pPr")
        if restart and ppr is not None and ppr.find(W + "sectPr") is not None:
            counters["footnote"] = 0  # numRestart eachSect: footnotes count again per section
    return out


def _fields(story: Story, notes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if story.kind in ("footnote", "endnote"):
        return {"story": story.kind, "note": notes.get(story.key)}
    return {
        "story": story.kind,
        "section": story.section,
        "kind": story.hf_kind,
        "used_by_sections": list(story.used_by_sections),
    }


def rows(doc: Document, kinds: tuple[str, ...]) -> Iterator[Row]:
    """Every paragraph of the stories of ``kinds``, in reading order."""
    others = [s for s in list_stories(doc.pkg, kinds) if s.kind != "body"]
    notes = [s for s in others if s.kind in ("footnote", "endnote")]
    pages = [s for s in others if s.kind in ("header", "footer")]
    numbers = _note_numbers(doc) if notes else {}
    after: dict[str, list[Story]] = {}
    unplaced: list[Story] = []
    for story in notes:
        mark = numbers.get(story.key)
        if mark is not None and "body" in kinds:
            after.setdefault(mark["of"], []).append(story)
        else:
            unplaced.append(story)  # no mark in the body, or the body not asked for

    def story_rows(story: Story) -> Iterator[Row]:
        view = doc.story_view(story.key)
        fields = _fields(story, numbers)
        for para in view.index.paragraphs:
            yield Row(view, para, fields)

    if "body" in kinds:
        for para in doc.index.paragraphs:
            yield Row(doc, para, {})
            for story in after.get(str(para.address), ()):
                yield from story_rows(story)
    for story in unplaced + pages:
        yield from story_rows(story)


def fields_for(doc: Document, key: str) -> dict[str, Any]:
    """The story facts of one story's rows ({} for the body)."""
    if not key:
        return {}
    from doc_control.model.stories import find_story

    story = find_story(doc.pkg, key)
    numbers = _note_numbers(doc) if story.kind in ("footnote", "endnote") else {}
    return _fields(story, numbers)
