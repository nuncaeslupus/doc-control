"""Tracked operations: ``text.replace``, ``para.insert``, ``para.delete``, ``para.move``,
``style.apply``, ``format.apply``.

:func:`apply_edits` applies a list of edits to a closed ``.docx`` in one verified
write. Every address in the list refers to the document as it was read (all are
resolved before anything changes), every change is a tracked revision and
nothing outside the edited paragraphs changes.

The six edits
    ``text.replace`` ``at`` is a range (``p3#fp:10-15``, a whole paragraph
    ``p3#fp``, or across paragraphs ``p3#fp:10..p5#fp:2``) and ``text`` the new
    text (``""`` deletes). A range across paragraphs deletes the tail of the
    first, whole middle paragraphs and the head of the last, tracks the
    deletion of the marks in between and puts the new text at the end of the
    first paragraph: accepted, the paragraphs join (the last one's properties
    win), rejected, they are all back. ``\\t`` and ``\\n`` in ``text`` become
    ``w:tab`` and ``w:br``, so the new visible text is exactly ``text``.

    ``para.insert`` adds a paragraph ``before`` or ``after`` a paragraph, with
    that paragraph's properties. Normally the new paragraph has an inserted mark
    (rejected, it merges into the paragraph after it and disappears). After the
    last paragraph of a table cell, the body or a run of paragraphs before a
    table, Word's own form is used: the *reference* paragraph's mark becomes the
    inserted one and the new paragraph keeps the original mark.

    ``para.delete`` deletes one paragraph or a run (``p3#fp..p5#fp``): content
    and mark are tracked deletions. The last paragraph of a cell or of the body
    cannot be deleted (Word cannot remove that mark): delete its text instead.

    ``para.move`` moves a run of paragraphs ``after`` or ``before`` another one
    as a tracked deletion of the originals plus a tracked insertion of copies
    (spec: v1 has no ``w:moveFrom``/``w:moveTo``). Bookmarks that start and end
    inside the moved paragraphs go with the text (same name, new id; ``_GoBack``
    is dropped; rejecting the move removes them, so REF/PAGEREF fields, TOC entries and
    links aimed at them are reported in ``warnings``); a bookmark reaching outside
    them, and comment anchors, are refused. Moving to after the last paragraph of a container
    uses Word's own form, as ``para.insert`` does (the target's mark becomes the inserted one).

    ``style.apply`` gives whole paragraphs (``p3#fp`` or ``p3#fp..p5#fp``) a paragraph
    style, or a range of text a character style, by id or name. Paragraph properties are
    a tracked ``w:pPrChange`` holding the previous properties (so the previous style);
    run properties a ``w:rPrChange`` on each run of the range, split at its ends.

    ``format.apply`` changes direct formatting the same way. Run properties (``bold``,
    ``italic``, ``underline``, ``small_caps``, ``font``, ``size_pt``) apply to a paragraph,
    a run of paragraphs or a range of text; paragraph properties (``align``,
    ``indent_left_cm``, ``indent_right_cm``, ``indent_first_line_cm``, ``spacing_before_pt``,
    ``spacing_after_pt``, ``line_spacing``) to whole paragraphs only. Setting what is already
    set writes nothing; a paragraph or run that already holds an unresolved tracked property
    change is refused (``UNSUPPORTED_CONTENT``). Rejecting the change gives the previous
    properties back.

Offsets
    Offsets are Python code points of the visible text. A write never splits a
    base character from what attaches to it: a boundary in front of a combining
    mark (Mn, Mc, Me), a zero width joiner or the character after one, an emoji
    modifier, a tag character, or inside a pair of regional indicators raises
    ``ADDRESS_INVALID``.

Several edits in one call
    Edits on different paragraphs are independent. Formatting edits on one paragraph
    combine (different properties, disjoint ranges) and may sit beside text replacements
    that neither overlap nor touch their range; the same property set twice, or overlapping
    ranges, raise ``BATCH_CONFLICT``. Text replacements in the
    same paragraph must not overlap or touch an insertion point. Two inserts at
    the same place, or any other edit on a paragraph already edited, raise
    ``BATCH_CONFLICT``.

Authorship
    ``w:author`` is ``author`` (default ``Claude``) followed by ``(tag)`` when a
    tag is given; ``w:date`` is the time of the call, ISO 8601 UTC.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import parse_address, parse_range
from doc_control.model.index import DocumentIndex, Paragraph, build_index
from doc_control.model.stories import BODY_STORY, Story, find_story, story_index
from doc_control.model.styles import StyleTable, load_styles
from doc_control.ooxml.formatting import ALIGNMENTS, PARAGRAPH_KEYS, RUN_KEYS
from doc_control.ooxml.revisions import (
    DEFAULT_AUTHOR,
    BookmarkScope,
    ParagraphEditor,
    RevisionContext,
    format_author,
    format_date,
    inserted_content,
    mark_state,
    new_paragraph,
    referenced_bookmarks,
    reject_revisions,
    used_ids,
)
from doc_control.ooxml.splice import W_NS, Container, DocumentSpans, scan
from doc_control.ooxml.zipio import Package
from doc_control.safety.history import has_live_text, record, to_text
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, Footprint, StoryFootprint, write_verified
from doc_control.schema import validate

__all__ = [
    "REQUEST_NAMES",
    "ApplyFormat",
    "ApplyStyle",
    "DeleteParagraphs",
    "Edit",
    "InsertParagraph",
    "MoveParagraphs",
    "PreparedEdits",
    "ReplaceText",
    "TrackedResult",
    "apply_edits",
    "edit_from_dict",
    "edits_as_request",
    "log_changes",
    "next_in_container",
    "prepare_edits",
]

PREVIEW_LIMIT = 200
# Rewrites a paragraph element in place without changing its text (comment anchors).
Annotator = Callable[[ET.Element, Paragraph, RevisionContext], None]
_ZWJ = "\N{ZERO WIDTH JOINER}"
_INERT_LOCALS = frozenset(
    name.encode()
    for name in (
        "bookmarkStart",
        "bookmarkEnd",
        "commentRangeStart",
        "commentRangeEnd",
        "permStart",
        "permEnd",
        "proofErr",
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
    )
)
_SKIPPABLE = re.compile(rb"<!--.*?-->|<\?.*?\?>", re.S)
_TAG = re.compile(rb"""<(/?)([^\s/>!?]+)(?:"[^"]*"|'[^']*'|[^>"'])*>""")


# -- the edits and their schemas -------------------------------------------------


@dataclass(frozen=True)
class ReplaceText:
    at: str
    text: str


@dataclass(frozen=True)
class InsertParagraph:
    text: str
    after: str | None = None
    before: str | None = None


@dataclass(frozen=True)
class DeleteParagraphs:
    at: str


@dataclass(frozen=True)
class MoveParagraphs:
    at: str
    after: str | None = None
    before: str | None = None


@dataclass(frozen=True)
class ApplyStyle:
    at: str
    style: str


@dataclass(frozen=True)
class ApplyFormat:
    at: str
    props: dict[str, Any]


Edit = ReplaceText | InsertParagraph | DeleteParagraphs | MoveParagraphs | ApplyStyle | ApplyFormat

_STR: dict[str, Any] = {"type": "string", "minLength": 1}
OPERATION_SCHEMAS: dict[str, dict[str, Any]] = {
    "text.replace": {
        "type": "object",
        "properties": {
            "op": {"enum": ["text.replace"]},
            "at": {**_STR, "description": "Range p3#fp:10-15, p3#fp or p3#fp:10..p5#fp:2."},
            "text": {"type": "string", "description": "New text; empty deletes the range."},
        },
        "required": ["op", "at", "text"],
        "additionalProperties": False,
    },
    "para.insert": {
        "type": "object",
        "properties": {
            "op": {"enum": ["para.insert"]},
            "text": {"type": "string"},
            "after": {**_STR, "description": "Insert after this paragraph (or before)."},
            "before": {**_STR, "description": "Insert before this paragraph (or after)."},
        },
        "required": ["op", "text"],
        "additionalProperties": False,
    },
    "para.delete": {
        "type": "object",
        "properties": {
            "op": {"enum": ["para.delete"]},
            "at": {**_STR, "description": "Paragraph p3#fp or run p3#fp..p5#fp."},
        },
        "required": ["op", "at"],
        "additionalProperties": False,
    },
    "para.move": {
        "type": "object",
        "properties": {
            "op": {"enum": ["para.move"]},
            "at": {**_STR, "description": "Paragraph p3#fp or run p3#fp..p5#fp."},
            "after": {**_STR, "description": "Move after this paragraph (or before)."},
            "before": {**_STR, "description": "Move before this paragraph (or after)."},
        },
        "required": ["op", "at"],
        "additionalProperties": False,
    },
    "style.apply": {
        "type": "object",
        "properties": {
            "op": {"enum": ["style.apply"]},
            "at": {
                **_STR,
                "description": "Paragraph p3#fp or run p3#fp..p5#fp (paragraph style), or a "
                "range p3#fp:10-15 (character style).",
            },
            "style": {**_STR, "description": "Style id or name (see style.list)."},
        },
        "required": ["op", "at", "style"],
        "additionalProperties": False,
    },
    "format.apply": {
        "type": "object",
        "properties": {
            "op": {"enum": ["format.apply"]},
            "at": {
                **_STR,
                "description": "Paragraph p3#fp, run p3#fp..p5#fp or range p3#fp:10-15 "
                "(run properties only for a range).",
            },
            "bold": {"type": "boolean"},
            "italic": {"type": "boolean"},
            "underline": {"type": "boolean"},
            "small_caps": {"type": "boolean"},
            "font": {**_STR, "description": "Font family name."},
            "size_pt": {"type": "number", "minimum": 1, "maximum": 1638},
            "align": {"enum": list(ALIGNMENTS)},
            "indent_left_cm": {"type": "number", "minimum": -100, "maximum": 100},
            "indent_right_cm": {"type": "number", "minimum": -100, "maximum": 100},
            "indent_first_line_cm": {
                "type": "number",
                "minimum": -100,
                "maximum": 100,
                "description": "Negative makes a hanging indent.",
            },
            "spacing_before_pt": {"type": "number", "minimum": 0, "maximum": 1584},
            "spacing_after_pt": {"type": "number", "minimum": 0, "maximum": 1584},
            "line_spacing": {
                "type": "number",
                "minimum": 0.1,
                "maximum": 20,
                "description": "Line height as a multiple of single spacing.",
            },
        },
        "required": ["op", "at"],
        "additionalProperties": False,
    },
}


def edit_from_dict(raw: Mapping[str, Any]) -> Edit:
    """Validate a request object (``{"op": "text.replace", ...}``) and build the edit."""
    op = raw.get("op")
    if not isinstance(op, str) or op not in OPERATION_SCHEMAS:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"unknown tracked operation: {op!r}",
            expected=sorted(OPERATION_SCHEMAS),
        )
    validate(dict(raw), OPERATION_SCHEMAS[op])
    if op == "text.replace":
        return ReplaceText(raw["at"], raw["text"])
    if op == "para.delete":
        return DeleteParagraphs(raw["at"])
    if op == "style.apply":
        check_name(raw["style"], "style")
        return ApplyStyle(raw["at"], raw["style"])
    if op == "format.apply":
        props = {k: raw[k] for k in (*RUN_KEYS, *PARAGRAPH_KEYS) if k in raw}
        if not props:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                "format.apply needs at least one property",
                expected=[*RUN_KEYS, *PARAGRAPH_KEYS],
            )
        if "font" in props:
            check_name(props["font"], "font")
        return ApplyFormat(raw["at"], props)
    after, before = raw.get("after"), raw.get("before")
    if (after is None) == (before is None):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f"{op} needs exactly one of 'after' and 'before'"
        )
    if op == "para.insert":
        return InsertParagraph(raw["text"], after, before)
    return MoveParagraphs(raw["at"], after, before)


# -- results ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackedResult:
    applied: int
    dry_run: bool
    preview: list[dict[str, Any]]
    address_map: dict[str, str]
    footprint: list[str]
    revision_ids: list[int]
    author: str
    date: str
    before_sha256: str
    after_sha256: str | None = None
    backup: Path | None = None
    warnings: list[str] = field(default_factory=list)
    op_id: str | None = None
    parts: dict[str, list[str]] = field(default_factory=dict)  # other parts touched, with ids
    comment_ids: list[str] = field(default_factory=list)
    tracked: bool = True
    """False for a plain write (D-21): this call's revisions were accepted in the same write."""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "applied": self.applied,
            "dry_run": self.dry_run,
            "preview": self.preview,
            "address_map": self.address_map,
            "footprint": {DOCUMENT_PART: self.footprint, **self.parts},
            "revision_ids": self.revision_ids,
            "author": self.author,
            "date": self.date,
        }
        if self.comment_ids:
            out["comment_ids"] = self.comment_ids
        if not self.tracked:
            out["tracked"] = False
        if self.warnings:
            out["warnings"] = self.warnings
        if not self.dry_run:
            out["sha256"] = self.after_sha256
            out["backup"] = str(self.backup) if self.backup else None
            out["op_id"] = self.op_id
        return out


# -- checks on offsets and text ------------------------------------------------------


def boundary_ok(text: str, i: int) -> bool:
    """True if offset ``i`` of ``text`` does not split a character from its attachments."""
    if i <= 0 or i >= len(text):
        return True
    ch, prev = text[i], text[i - 1]
    if unicodedata.category(ch) in ("Mn", "Mc", "Me") or _ZWJ in (ch, prev):
        return False
    if "\U0001f3fb" <= ch <= "\U0001f3ff" or "\U000e0020" <= ch <= "\U000e007f":
        return False
    if "\U0001f1e6" <= ch <= "\U0001f1ff":  # inside a flag: an odd run of indicators before it
        run = 0
        while run < i and "\U0001f1e6" <= text[i - 1 - run] <= "\U0001f1ff":
            run += 1
        return run % 2 == 0
    return True


def _check_boundary(para: Paragraph, offset: int, at: str) -> None:
    if not boundary_ok(para.text, offset):
        raise DocControlError(
            ErrorCode.ADDRESS_INVALID,
            f"offset {offset} of {para.address} splits a character from its combining marks",
            address=at,
            offset=offset,
        )


def check_text(text: str, what: str) -> None:
    for ch in text:
        code = ord(ch)
        bad = (
            (code < 0x20 and ch not in "\t\n")
            or 0xD800 <= code <= 0xDFFF
            or code in (0xFFFE, 0xFFFF)
        )
        if bad:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                f"{what} holds U+{code:04X}, which Word text cannot contain",
            )


def check_name(value: str, what: str) -> None:
    if not value.strip():
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"{what} must not be empty")
    check_text(value, what)
    if "\t" in value or "\n" in value:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"{what} must be one line")


def _short(text: str) -> str:
    return text if len(text) <= PREVIEW_LIMIT else text[: PREVIEW_LIMIT - 1] + "…"


# -- layout: which paragraphs are neighbours ----------------------------------------------


def _inert_gap(gap: bytes) -> bool:
    """True if the bytes between two paragraphs hold only whitespace and markers."""
    gap = _SKIPPABLE.sub(b"", gap)
    pos = 0
    for m in _TAG.finditer(gap):
        if gap[pos : m.start()].strip():
            return False
        if m.group(2).rsplit(b":", 1)[-1] not in _INERT_LOCALS:
            return False
        pos = m.end()
    return not gap[pos:].strip()


class _Layout:
    """Neighbour test: paragraph ``b`` follows ``a`` in the same container."""

    def __init__(self, index: DocumentIndex) -> None:
        self.index = index

    def next_adjacent(self, para: Paragraph) -> Paragraph | None:
        if para.number >= len(self.index):
            return None
        nxt = self.index.paragraphs[para.number]
        spans = self.index.spans
        a, b = spans.paragraphs[para.span_index], spans.paragraphs[nxt.span_index]
        if b.start < a.end or b.depth != a.depth or b.parent != a.parent:
            return None
        return nxt if _inert_gap(spans.data[a.end : b.start]) else None


# -- the plan --------------------------------------------------------------------------------


@dataclass
class _Interval:
    start: int
    end: int
    text: str
    edit: int

    def clashes(self, other: _Interval) -> bool:
        a, b = self, other
        if a.start == a.end and b.start == b.end:
            return a.start == b.start
        if a.start == a.end:
            return b.start <= a.start <= b.end
        if b.start == b.end:
            return a.start <= b.start <= a.end
        return a.start < b.end and b.start < a.end


@dataclass
class _Format:
    """Run properties to set on visible characters ``[start, end)`` of a paragraph."""

    start: int
    end: int
    props: dict[str, Any]
    edit: int


@dataclass
class _ParaPlan:
    para: Paragraph
    intervals: list[_Interval] = field(default_factory=list)
    formats: list[_Format] = field(default_factory=list)
    ppr: dict[str, Any] = field(default_factory=dict)  # paragraph properties to set
    ppr_by: dict[str, int] = field(default_factory=dict)  # property -> edit that sets it
    delete_all: bool = False
    mark: str | None = None
    touchers: set[int] = field(default_factory=set)
    exclusive: bool = False
    moved_by: int | None = None  # the move edit that carries this paragraph away


@dataclass
class _NewParagraph:
    """A paragraph to add: its text, or the paragraph whose runs it copies."""

    edit: int
    slot: tuple[str, int]  # ("before" | "after", span index of the anchor)
    ref: Paragraph  # properties come from here
    text: str | None
    source: Paragraph | None  # a moved paragraph (copy its runs), else None
    native: bool  # the reference paragraph's mark is the inserted one
    expected: str = ""


class _Planner:
    def __init__(
        self, index: DocumentIndex, *, layered: bool = False, pkg: Package | None = None
    ) -> None:
        self.index = index
        self.pkg = pkg
        self._styles: StyleTable | None = None
        self.layered = layered
        self.layout = _Layout(index)
        self.plans: dict[int, _ParaPlan] = {}
        self.new: list[_NewParagraph] = []
        self.slots: dict[tuple[str, int], int] = {}
        self.previews: list[dict[str, Any]] = []
        self.removed: dict[int, int] = {}  # paragraph number -> op that deletes or moves it
        self.anchored: dict[int, set[int]] = {}  # paragraph number -> ops placed relative to it

    # -- bookkeeping --

    def _conflict(self, para: Paragraph, a: int, b: int) -> DocControlError:
        return DocControlError(
            ErrorCode.BATCH_CONFLICT,
            f"edit {max(a, b)} is placed relative to {para.address}, which edit "
            f"{min(a, b)} deletes or moves: addresses refer to the document as read",
            paragraph=str(para.address),
            edits=sorted((a, b)),
        )

    def _anchor(self, para: Paragraph, edit: int) -> None:
        """Edit ``edit`` inserts or moves relative to ``para``."""
        other = self.removed.get(para.number)
        if other is not None and other != edit and not self.layered:
            raise self._conflict(para, other, edit)
        self.anchored.setdefault(para.number, set()).add(edit)

    def _remove(self, para: Paragraph, edit: int) -> None:
        """Edit ``edit`` deletes or moves ``para`` away."""
        for other in sorted(self.anchored.get(para.number, ())):
            if other != edit and not self.layered:
                raise self._conflict(para, other, edit)
        self.removed[para.number] = edit

    def _plan(self, para: Paragraph, edit: int, *, exclusive: bool) -> _ParaPlan:
        plan = self.plans.setdefault(para.number, _ParaPlan(para))
        others = plan.touchers - {edit}
        if others and (exclusive or plan.exclusive):
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                f"edits {min(others)} and {edit} both change {para.address}",
                paragraph=str(para.address),
                edits=sorted(others | {edit}),
            )
        plan.touchers.add(edit)
        plan.exclusive = plan.exclusive or exclusive
        return plan

    def _check_overlap(self, plan: _ParaPlan, new: _Interval) -> None:
        olds = [*plan.intervals, *(_Interval(f.start, f.end, "", f.edit) for f in plan.formats)]
        for old in olds:
            if old.edit != new.edit and old.clashes(new):
                raise DocControlError(
                    ErrorCode.BATCH_CONFLICT,
                    f"edits {old.edit} and {new.edit} overlap in {plan.para.address}",
                    paragraph=str(plan.para.address),
                    edits=sorted([old.edit, new.edit]),
                )

    def _add_interval(self, plan: _ParaPlan, new: _Interval) -> None:
        self._check_overlap(plan, new)
        plan.intervals.append(new)

    def _add_format(self, plan: _ParaPlan, new: _Format) -> None:
        self._check_overlap(plan, _Interval(new.start, new.end, "", new.edit))
        plan.formats.append(new)

    def _claim_slot(self, slot: tuple[str, int], edit: int) -> None:
        if slot in self.slots and self.slots[slot] != edit and not self.layered:
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                f"edits {self.slots[slot]} and {edit} insert at the same place",
                edits=[self.slots[slot], edit],
            )
        self.slots.setdefault(slot, edit)

    def _run_of(self, at: str) -> list[Paragraph]:
        """Paragraphs of ``pN#fp`` or ``pN#fp..pM#fp`` (adjacent, in one container)."""
        first, sep, last = at.partition("..")
        a = self.index.resolve_address(parse_address(first))
        if not sep:
            return [a]
        b = self.index.resolve_address(parse_address(last))
        if b.number < a.number:
            raise DocControlError(
                ErrorCode.ADDRESS_INVALID, f"{at!r} ends before it starts", address=at
            )
        paras = list(self.index.paragraphs[a.number - 1 : b.number])
        self._require_adjacent(paras, at)
        return paras

    def _require_adjacent(self, paras: Sequence[Paragraph], at: str) -> None:
        for prev, nxt in itertools.pairwise(paras):
            if self.layout.next_adjacent(prev) is not nxt:
                raise DocControlError(
                    ErrorCode.UNSUPPORTED_CONTENT,
                    f"{prev.address} and {nxt.address} are not neighbours in the same "
                    "container (a table, cell edge or content control lies between)",
                    address=at,
                )

    def _slot_for(self, anchor: Paragraph, where: str, edit: int) -> tuple[tuple[str, int], bool]:
        """``(slot, native)`` for inserting before/after ``anchor``."""
        if where == "before":
            slot = ("before", anchor.span_index)
            self._claim_slot(slot, edit)
            return slot, False
        nxt = self.layout.next_adjacent(anchor)
        if nxt is not None:
            slot = ("before", nxt.span_index)
            self._claim_slot(slot, edit)
            return slot, False
        slot = ("after", anchor.span_index)
        self._claim_slot(slot, edit)
        return slot, True

    # -- the four edits --

    def replace(self, i: int, edit: ReplaceText) -> None:
        check_text(edit.text, "text")
        rng = self.index.resolve_range(edit.at)
        if rng.sentence is not None:
            raise DocControlError(
                ErrorCode.ADDRESS_INVALID,
                "sentence addresses need segmentation, which is not available yet",
                address=edit.at,
            )
        first, last = rng.first, rng.last
        assert rng.start_offset is not None and rng.end_offset is not None
        s, e = rng.start_offset, rng.end_offset
        _check_boundary(first, s, edit.at)
        _check_boundary(last, e, edit.at)
        if first is last:
            if s == e and not edit.text:
                raise DocControlError(ErrorCode.SCHEMA_INVALID, "text.replace would change nothing")
            plan = self._plan(first, i, exclusive=False)
            self._add_interval(plan, _Interval(s, e, edit.text, i))
            self.previews.append(
                {
                    "op": i,
                    "at": edit.at,
                    "before": _short(first.text[s:e]),
                    "after": _short(edit.text),
                }
            )
            return
        paras = list(self.index.paragraphs[first.number - 1 : last.number])
        self._require_adjacent(paras, edit.at)
        for para in paras:
            if para is not last:
                self._remove(para, i)
            plan = self._plan(para, i, exclusive=para is not last and not self.layered)
            if para is first:
                self._add_interval(plan, _Interval(s, len(para.text), edit.text, i))
            elif para is last:
                if e:
                    self._add_interval(plan, _Interval(0, e, "", i))
            else:
                plan.delete_all = True
            if para is not last:
                plan.mark = "del"
        removed = "\n".join([first.text[s:], *(p.text for p in paras[1:-1]), last.text[:e]])
        self.previews.append(
            {"op": i, "at": edit.at, "before": _short(removed), "after": _short(edit.text)}
        )

    def delete(self, i: int, edit: DeleteParagraphs) -> None:
        paras = self._run_of(edit.at)
        if self.layout.next_adjacent(paras[-1]) is None:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"{paras[-1].address} is the last paragraph of its cell, section or the "
                "body: Word cannot delete that mark; delete its text with text.replace",
                address=edit.at,
            )
        for para in paras:
            self._remove(para, i)
            plan = self._plan(para, i, exclusive=True)
            plan.delete_all = True
            plan.mark = "del"
        self.previews.append(
            {
                "op": i,
                "at": edit.at,
                "before": _short("\n".join(p.text for p in paras)),
                "after": "(deleted)",
            }
        )

    def insert(self, i: int, edit: InsertParagraph) -> None:
        check_text(edit.text, "text")
        where, anchor_at = (
            ("after", edit.after) if edit.after is not None else ("before", edit.before)
        )
        assert anchor_at is not None
        anchor = self.index.resolve_address(parse_address(anchor_at))
        self._anchor(anchor, i)
        slot, native = self._slot_for(anchor, where, i)
        if native:
            self._plan(anchor, i, exclusive=not self.layered).mark = "ins"
        self.new.append(_NewParagraph(i, slot, anchor, edit.text, None, native, edit.text))
        self.previews.append(
            {"op": i, "at": f"{where} {anchor.address}", "before": "", "after": _short(edit.text)}
        )

    def move(self, i: int, edit: MoveParagraphs) -> None:
        paras = self._run_of(edit.at)
        where, target_at = (
            ("after", edit.after) if edit.after is not None else ("before", edit.before)
        )
        assert target_at is not None
        target = self.index.resolve_address(parse_address(target_at))
        lo, hi = paras[0].number, paras[-1].number
        inside = (
            (lo - 1 <= target.number <= hi) if where == "after" else (lo <= target.number <= hi + 1)
        )
        if inside:
            raise DocControlError(
                ErrorCode.ADDRESS_INVALID,
                f"the target {target.address} is inside or right next to the moved paragraphs",
                address=target_at,
            )
        if self.layout.next_adjacent(paras[-1]) is None:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"{paras[-1].address} is the last paragraph of its cell, section or the body: "
                "it cannot be moved away",
                address=edit.at,
            )
        self._anchor(target, i)
        slot, native = self._slot_for(target, where, i)
        if native:
            # After the end of a container (the body's last paragraph): Word's own form. The
            # target's mark becomes the inserted one and the last copy keeps the original mark
            # (and the target's properties), so rejecting gives the target back untouched.
            self._plan(target, i, exclusive=not self.layered).mark = "ins"
        for para in paras:
            self._remove(para, i)
            plan = self._plan(para, i, exclusive=True)
            plan.delete_all = True
            plan.mark = "del"
            plan.moved_by = i
            last = native and para is paras[-1]
            self.new.append(
                _NewParagraph(i, slot, target if last else para, None, para, last, para.text)
            )
        self.previews.append(
            {
                "op": i,
                "at": edit.at,
                "before": _short("\n".join(p.text for p in paras)),
                "after": f"(moved {where} {target.address})",
            }
        )

    # -- formatting --

    @property
    def styles(self) -> StyleTable:
        if self._styles is None:
            assert self.pkg is not None
            self._styles = load_styles(self.pkg)
        return self._styles

    def _format_targets(self, at: str) -> list[tuple[Paragraph, int, int, bool]]:
        """``(paragraph, start, end, whole)`` for a paragraph, a run of paragraphs or a range."""
        if ".." in at and ":" not in at:
            first_at, _, last_at = at.partition("..")
            a = self.index.resolve_address(parse_address(first_at))
            b = self.index.resolve_address(parse_address(last_at))
            if b.number < a.number:
                raise DocControlError(
                    ErrorCode.ADDRESS_INVALID, f"{at!r} ends before it starts", address=at
                )
            return [
                (p, 0, len(p.text), True) for p in self.index.paragraphs[a.number - 1 : b.number]
            ]
        rng = self.index.resolve_range(at)
        if rng.sentence is not None:
            raise DocControlError(
                ErrorCode.ADDRESS_INVALID,
                "sentence addresses need segmentation, which is not available yet",
                address=at,
            )
        assert rng.start_offset is not None and rng.end_offset is not None
        whole = rng.range.start_offset is None
        if not whole:
            _check_boundary(rng.first, rng.start_offset, at)
            _check_boundary(rng.last, rng.end_offset, at)
        paras = self.index.paragraphs[rng.first.number - 1 : rng.last.number]
        return [
            (
                p,
                rng.start_offset if p is rng.first else 0,
                rng.end_offset if p is rng.last else len(p.text),
                whole,
            )
            for p in paras
        ]

    def _set_ppr(self, i: int, para: Paragraph, props: dict[str, Any]) -> None:
        plan = self._plan(para, i, exclusive=False)
        for key in props:
            other = plan.ppr_by.get(key)
            if other is not None and other != i:
                raise DocControlError(
                    ErrorCode.BATCH_CONFLICT,
                    f"edits {other} and {i} both set {key} of {para.address}",
                    paragraph=str(para.address),
                    edits=sorted([other, i]),
                )
            plan.ppr_by[key] = i
        plan.ppr.update(props)

    def _set_runs(self, i: int, para: Paragraph, s: int, e: int, props: dict[str, Any]) -> None:
        if s == e:
            return  # no text, no run to format
        plan = self._plan(para, i, exclusive=False)
        self._add_format(plan, _Format(s, e, props, i))

    def _previous_styles(self, paras: Sequence[Paragraph]) -> str:
        found: list[str] = []
        for para in paras:
            elem = self.index.spans.parse_paragraph(para.span_index)
            ppr = elem.find(f"{{{W_NS}}}pPr")
            style = ppr.find(f"{{{W_NS}}}pStyle") if ppr is not None else None
            value = (style.get(f"{{{W_NS}}}val") if style is not None else None) or "(default)"
            if value not in found:
                found.append(value)
        return ", ".join(found)

    def style(self, i: int, edit: ApplyStyle) -> None:
        found = self.styles.find(edit.style)
        if found is None:
            raise DocControlError(
                ErrorCode.STYLE_NOT_FOUND,
                f"no style {edit.style!r} in the document",
                style=edit.style,
            )
        targets = self._format_targets(edit.at)
        if found.type == "paragraph":
            if not all(whole for *_, whole in targets):
                raise DocControlError(
                    ErrorCode.SCHEMA_INVALID,
                    f"{found.id!r} is a paragraph style: address whole paragraphs, not text",
                    address=edit.at,
                )
            for para, _, _, _ in targets:
                self._set_ppr(i, para, {"style": found.id})
            before = self._previous_styles([t[0] for t in targets])
        elif found.type == "character":
            for para, s, e, _ in targets:
                self._set_runs(i, para, s, e, {"style": found.id})
            before = _short(targets[0][0].text[targets[0][1] : targets[0][2]])
        else:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                f"{found.id!r} is a {found.type} style: only paragraph and character "
                "styles can be applied here",
                style=found.id,
            )
        self.previews.append({"op": i, "at": edit.at, "before": before, "after": found.id})

    def format(self, i: int, edit: ApplyFormat) -> None:
        run_props = {k: v for k, v in edit.props.items() if k in RUN_KEYS}
        para_props = {k: v for k, v in edit.props.items() if k in PARAGRAPH_KEYS}
        targets = self._format_targets(edit.at)
        if para_props and not all(whole for *_, whole in targets):
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                f"{sorted(para_props)} apply to whole paragraphs, not a text range",
                address=edit.at,
            )
        for para, s, e, _ in targets:
            if para_props:
                self._set_ppr(i, para, para_props)
            if run_props:
                self._set_runs(i, para, s, e, run_props)
        first, s, e, _ = targets[0]
        self.previews.append(
            {
                "op": i,
                "at": edit.at,
                "before": _short(first.text[s:e]),
                "after": ", ".join(f"{k}={v}" for k, v in edit.props.items()),
            }
        )


# -- applying ---------------------------------------------------------------------------------


def _serialize(spans: DocumentSpans, elem: ET.Element, at: int) -> bytes:
    try:
        return spans.serialize(elem, at)
    except ValueError as exc:
        raise DocControlError(ErrorCode.INTERNAL, f"cannot write the paragraph: {exc}") from exc


def _merge_marks(texts: Sequence[str], carry_forward: Sequence[bool]) -> list[str]:
    """Join paragraphs whose mark is gone into the paragraph after them."""
    out: list[str] = []
    carry = ""
    for text, joins in zip(texts, carry_forward, strict=True):
        text = carry + text
        if joins:
            carry = text
        else:
            out.append(text)
            carry = ""
    if carry:
        out.append(carry)
    return out


@dataclass(frozen=True)
class PreparedEdits:
    """The edited ``word/document.xml`` of a package, checked but not written anywhere."""

    pkg: Package
    new_data: bytes
    footprint: Footprint
    result: TrackedResult
    changes: list[dict[str, Any]] = field(default_factory=list)
    """The undo-log record of the edit: each touched paragraph's bytes before and after."""
    written: dict[str, bytes] = field(default_factory=dict)
    """Every part the edits change (the body, notes, headers, footers), with its new bytes."""


def next_in_container(index: DocumentIndex, para: Paragraph) -> Paragraph | None:
    """The paragraph right after ``para`` in the same container, or None if ``para`` is the
    last of its cell, section or the body (Word cannot delete such a mark)."""
    return _Layout(index).next_adjacent(para)


def story_of(at: str | None) -> str:
    """The story prefix of an address or range (``""``: the body, or an address that does
    not parse, whose error the planner raises as before)."""
    if at is None:
        return ""
    try:
        return parse_range(at).start.story
    except DocControlError:
        pass
    try:
        return parse_address(at.partition("..")[0]).story
    except DocControlError:
        return ""


def _edit_story(edit: Any) -> str | None:
    """The story an edit works in; None for an edit the planner does not handle."""
    if isinstance(edit, MoveParagraphs):
        if any(story_of(a) for a in (edit.at, edit.after, edit.before)):
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                "para.move works in the body only, not in notes, headers or footers",
                address=edit.at,
            )
        return ""
    if isinstance(edit, InsertParagraph):
        return story_of(edit.after if edit.after is not None else edit.before)
    if isinstance(edit, ReplaceText | DeleteParagraphs | ApplyStyle | ApplyFormat):
        return story_of(edit.at)
    return None


@dataclass
class _StoryOutcome:
    """One story's share of :func:`prepare_edits`, spliced into its part as read."""

    story: Story
    new_data: bytes
    replacements: dict[int, bytes]
    after: dict[int, list[bytes]]
    before: dict[int, list[bytes]]
    previews: list[dict[str, Any]]
    address_map: dict[str, str]
    footprint: list[str]
    changes: list[dict[str, Any]]
    scopes: dict[int, BookmarkScope]


def prepare_edits(
    pkg: Package,
    edits: Sequence[Any],
    *,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    layered: bool = False,
    index: DocumentIndex | None = None,
    annotations: Mapping[int, Annotator] | Mapping[tuple[str, int], Annotator] | None = None,
    reserved_ids: Collection[int] = (),
) -> PreparedEdits:
    """Plan and build ``edits`` on ``pkg`` without writing: the part of :func:`apply_edits`
    that works on bytes, self-checked (see there).

    Edits may sit in any story (``fn3/p1#…``, ``hdr1.default/p1#…``): each story is planned
    and checked on its own, with one revision id counter for all, and every part they touch
    is in :attr:`PreparedEdits.written`. ``para.move`` is body only (``UNSUPPORTED_CONTENT``).

    With ``layered`` several paragraphs may be inserted at the same place (they
    keep the order of the edits), a paragraph may be edited and also be the anchor of an
    insertion after the end of its container, and the first paragraph of a range across
    paragraphs may carry other (non-overlapping) edits; used by ``doc.compare``.

    ``annotations`` maps paragraphs (a body paragraph number, or ``(story, number)``) to
    functions that rewrite that paragraph's element without changing its text (comment
    anchors); such a paragraph must not also be edited (``BATCH_CONFLICT``).
    ``reserved_ids`` are ``w:id`` values already taken for other markup, which no revision
    gets. ``edits`` may be empty when annotations are given. ``index`` is the body's.
    """
    by_story: dict[str, dict[int, Annotator]] = {}
    for where, annotate in (annotations or {}).items():
        story_key, number = where if isinstance(where, tuple) else ("", where)
        by_story.setdefault(story_key, {})[number] = annotate
    if not edits and not by_story:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "no edits given")
    check_name(author, "author")
    if tag is not None:
        check_name(tag, "tag")

    groups: dict[str, list[tuple[int, Any]]] = {}
    for i, edit in enumerate(edits):
        key = _edit_story(edit)
        if key is not None:
            groups.setdefault(key, []).append((i, edit))
    for key in by_story:
        groups.setdefault(key, [])
    if not groups:
        groups[""] = []  # nothing to plan (comment replies, resolutions): the body as read
    order = sorted(groups, key=lambda k: (k != "", groups[k][0][0] if groups[k] else len(edits)))

    ctx = RevisionContext(
        used_ids(pkg) | set(reserved_ids), format_author(author, tag), format_date(now)
    )
    outcomes: list[_StoryOutcome] = []
    places: dict[tuple[str, Container], str] = {}
    for key in order:
        if key:
            story = find_story(pkg, key)
            other = places.setdefault((story.part, story.container), key)
            if other != key:
                raise DocControlError(
                    ErrorCode.UNSUPPORTED_CONTENT,
                    f"{other} and {key} are the same {story.kind}: address it one way",
                    story=key,
                )
            story_idx = story_index(pkg, story)
        else:
            story = BODY_STORY
            story_idx = index if index is not None else DocumentIndex.from_package(pkg)
        outcomes.append(
            _prepare_story(
                pkg, story, story_idx, groups[key], ctx, by_story.get(key, {}), layered=layered
            )
        )
    if not ctx.revision_ids and all(isinstance(e, ApplyStyle | ApplyFormat) for e in edits):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            "the operations change nothing: the properties are already set",
        )

    new_data = pkg.read(DOCUMENT_PART) if outcomes[0].story.key else outcomes[0].new_data
    written: dict[str, bytes] = {}
    body = next((o for o in outcomes if not o.story.key), None)
    story_footprints: list[StoryFootprint] = []
    parts: dict[str, list[str]] = {}
    for out in outcomes:
        story = out.story
        if not story.key:
            written[DOCUMENT_PART] = out.new_data
            continue
        part = story.part
        # Stories sharing a part (two footnotes) are spliced one after the other.
        written[part] = (
            scan(written[part], story.container, part).splice(
                out.replacements, insert_after=out.after, insert_before=out.before
            )
            if part in written
            else out.new_data
        )
        story_footprints.append(
            StoryFootprint(
                part,
                story.container,
                replaced=frozenset(out.replacements),
                inserted_after=frozenset(out.after),
                inserted_before=frozenset(out.before),
            )
        )
        parts.setdefault(part, []).extend(out.footprint)
    footprint = Footprint(
        replaced=frozenset(body.replacements) if body else frozenset(),
        inserted_after=frozenset(body.after) if body else frozenset(),
        inserted_before=frozenset(body.before) if body else frozenset(),
        stories=tuple(story_footprints),
    )
    scopes = {e: s for out in outcomes for e, s in out.scopes.items()}
    result = TrackedResult(
        applied=len(edits),
        dry_run=dry_run,
        preview=sorted((p for out in outcomes for p in out.previews), key=lambda p: p["op"]),
        address_map={k: v for out in outcomes for k, v in out.address_map.items()},
        footprint=body.footprint if body else [],
        revision_ids=[int(r) for r in ctx.revision_ids],
        author=ctx.author,
        date=ctx.date,
        before_sha256=pkg.sha256,
        warnings=_reference_warnings(pkg, scopes),
        parts=parts,
    )
    changes = [c for out in outcomes for c in out.changes]
    return PreparedEdits(pkg, new_data, footprint, result, changes, written)


def _prepare_story(
    pkg: Package,
    story: Story,
    index: DocumentIndex,
    items: Sequence[tuple[int, Any]],
    ctx: RevisionContext,
    annotations: Mapping[int, Annotator],
    *,
    layered: bool,
) -> _StoryOutcome:
    """Plan, build and self-check the edits of one story (``items``: (edit number, edit))."""
    planner = _Planner(index, layered=layered, pkg=pkg)
    for i, edit in items:
        if isinstance(edit, ReplaceText):
            planner.replace(i, edit)
        elif isinstance(edit, InsertParagraph):
            planner.insert(i, edit)
        elif isinstance(edit, DeleteParagraphs):
            planner.delete(i, edit)
        elif isinstance(edit, MoveParagraphs):
            planner.move(i, edit)
        elif isinstance(edit, ApplyStyle):
            planner.style(i, edit)
        elif isinstance(edit, ApplyFormat):
            planner.format(i, edit)
    for number in sorted(annotations):
        if number in planner.plans:
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                f"a comment is placed on {index.paragraph(number).address}, which another "
                "operation of the batch edits, deletes or moves: apply them in separate batches",
                paragraph=str(index.paragraph(number).address),
            )

    spans = index.spans
    scopes = _bookmark_scopes(pkg, index, planner, ctx)
    replacements: dict[int, bytes] = {}
    for number in sorted(planner.plans):
        plan = planner.plans[number]
        para = plan.para
        elem = spans.parse_paragraph(para.span_index)
        editor = ParagraphEditor(elem, para, ctx)
        if plan.delete_all:
            if plan.moved_by is not None:
                editor.detach_bookmarks(scopes[plan.moved_by])
            editor.delete_content()
            _require_no_live_text(elem, para)
        steps: list[_Interval | _Format] = [*plan.intervals, *plan.formats]
        for part in sorted(steps, key=lambda x: (x.start, x.end), reverse=True):
            if isinstance(part, _Interval):
                editor.replace(part.start, part.end, part.text)
            else:
                editor.format_range(part.start, part.end, part.props)
        if plan.mark:
            editor.mark(plan.mark)
        if plan.ppr:
            editor.format_paragraph(plan.ppr)
        replacements[para.span_index] = _serialize(spans, elem, para.span_index)
    for number in sorted(annotations):
        para = index.paragraph(number)
        elem = spans.parse_paragraph(para.span_index)
        annotations[number](elem, para, ctx)
        replacements[para.span_index] = _serialize(spans, elem, para.span_index)

    after: dict[int, list[bytes]] = {}
    before: dict[int, list[bytes]] = {}
    last_native = {item.slot: item for item in planner.new if item.native}
    for item in planner.new:
        ref_elem = spans.parse_paragraph(item.ref.span_index)
        # Several paragraphs after the end of a container: only the last keeps the original mark.
        native = item.native and last_native[item.slot] is item
        p, props = new_paragraph(ref_elem, ctx, inserted_mark=not native)
        content: ET.Element | None
        if item.source is not None:
            source_elem = spans.parse_paragraph(item.source.span_index)
            runs = ParagraphEditor(source_elem, item.source, ctx).copy_runs(scopes.get(item.edit))
            content = inserted_content(ctx, runs=runs)
        else:
            content = inserted_content(ctx, text=item.text, run_props=props)
        if content is not None:
            p.append(content)
        target = after if item.slot[0] == "after" else before
        target.setdefault(item.slot[1], []).append(_serialize(spans, p, item.slot[1]))

    new_data = spans.splice(replacements, insert_after=after, insert_before=before)

    # The new numbering: each old paragraph, with the inserted ones around it.
    by_slot: dict[tuple[str, int], list[_NewParagraph]] = {}
    for item in planner.new:
        by_slot.setdefault(item.slot, []).append(item)
    sequence: list[tuple[Paragraph | None, _NewParagraph | None]] = []
    expected: list[str] = []
    for para in index.paragraphs:
        for item in by_slot.get(("before", para.span_index), []):
            sequence.append((None, item))
            expected.append(item.expected)
        sequence.append((para, None))
        expected.append(_expected_text(para, planner.plans.get(para.number)))
        for item in by_slot.get(("after", para.span_index), []):
            sequence.append((None, item))
            expected.append(item.expected)
    new_index = _self_check(index, new_data, ctx, expected, sequence, planner, story)

    address_map: dict[str, str] = {}
    moved_to: dict[int, int] = {}  # old number of a moved paragraph -> number of its copy
    for number, (_, copy) in enumerate(sequence, start=1):
        if copy is not None and copy.source is not None:
            moved_to[copy.source.number] = number
    for number, (old, _) in enumerate(sequence, start=1):
        if old is None:
            continue
        number = moved_to.get(old.number, number)
        if old.number != number:
            address_map[str(old.address)] = str(new_index.paragraph(number).address)
    prefix = f"{story.key}/" if story.key else ""
    footprint = [
        f"{prefix}p{number}"
        for number, (old, item) in enumerate(sequence, start=1)
        if item is not None
        or (old is not None and (old.number in planner.plans or old.number in annotations))
    ]
    changes = log_changes(
        spans, index, replacements, after, before, part=story.part, story=story.key
    )
    return _StoryOutcome(
        story,
        new_data,
        replacements,
        after,
        before,
        planner.previews,
        address_map,
        footprint,
        changes,
        scopes,
    )


def log_changes(
    spans: DocumentSpans,
    index: DocumentIndex,
    replacements: Mapping[int, bytes],
    after: Mapping[int, Sequence[bytes]],
    before: Mapping[int, Sequence[bytes]],
    *,
    part: str = DOCUMENT_PART,
    story: str = "",
) -> list[dict[str, Any]]:
    """The undo-log records of an edit, in document order (see ``safety.history``); a
    change outside the body names its ``part`` and ``story``."""
    where = {"part": part, **({"story": story} if story else {})}
    texts = {para.span_index: para.text for para in index.paragraphs}
    changes: list[dict[str, Any]] = []
    for at in sorted(replacements):
        changes.append(
            {
                **where,
                "kind": "replace",
                "index": at,
                "before": to_text(spans.paragraph_bytes(at)),
                "after": to_text(replacements[at]),
                "text_before": texts[at],
                "deleted": not has_live_text(replacements[at]),
            }
        )
    for slot, groups in (("before", before), ("after", after)):
        for at in sorted(groups):
            changes.extend(
                {
                    **where,
                    "kind": "insert",
                    "slot": slot,
                    "index": at,
                    "after": to_text(data),
                }
                for data in groups[at]
            )
    return changes


#: Request names of edit kinds defined in other modules (``ops.comments`` adds its own).
REQUEST_NAMES: dict[type, str] = {}


def edits_as_request(edits: Sequence[Edit]) -> list[dict[str, Any]]:
    """The edits as the ``ops`` objects a batch request carries (for the undo log)."""
    names: dict[type, str] = {
        ReplaceText: "text.replace",
        InsertParagraph: "para.insert",
        DeleteParagraphs: "para.delete",
        MoveParagraphs: "para.move",
        ApplyStyle: "style.apply",
        ApplyFormat: "format.apply",
        **REQUEST_NAMES,
    }
    out: list[dict[str, Any]] = []
    for edit in edits:
        fields = {k: v for k, v in vars(edit).items() if v is not None}
        if isinstance(edit, ApplyFormat):
            fields = {"at": edit.at, **edit.props}  # the request carries the properties flat
        out.append({"op": names[type(edit)], **fields})
    return out


def apply_edits(
    doc_path: Path,
    edits: Sequence[Edit],
    *,
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    stage: Callable[[str], None] | None = None,
    kind: str = "batch",
    request: Any = None,
    log_extra: Mapping[str, Any] | None = None,
    tracked: bool = True,
) -> TrackedResult:
    """Apply ``edits`` to ``doc_path`` as tracked changes in one verified write.

    With ``tracked`` false (a plain write, D-21) the edit is built the same way and this
    call's own revisions are accepted before the write (:mod:`doc_control.ops.plain`).

    Raises ``DOC_NOT_FOUND``, ``NOT_A_DOCX``, ``DOC_LOCKED`` or
    ``DOC_CHANGED_ON_DISK`` (also when ``expect_sha256`` differs),
    ``ADDRESS_INVALID``, ``STALE_ADDRESS``, ``UNSUPPORTED_CONTENT``,
    ``BATCH_CONFLICT`` and ``SCHEMA_INVALID`` before touching the file; later
    failures are ``VERIFY_FAILED_RESTORED`` (see :func:`write_verified`). Before the
    write the result is checked: the paragraph texts must be the intended ones
    and rejecting this call's revisions must give back the original texts, or
    ``INTERNAL`` is raised and nothing is written. A dry run does everything but
    the write. A real write is appended to the undo log (``kind``, ``request`` and
    ``log_extra`` describe it there); if that append fails the write stands and
    the result carries a warning instead of ``op_id``.
    """
    if not edits:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "no edits given")
    check_name(author, "author")
    if tag is not None:
        check_name(tag, "tag")
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    prepared = prepare_edits(pkg, edits, author=author, tag=tag, now=now, dry_run=dry_run)
    if not tracked:
        from doc_control.ops.plain import make_plain

        prepared, plain_extra = make_plain(pkg, prepared)
        log_extra = {**(log_extra or {}), **plain_extra}
    result = prepared.result
    if dry_run:
        return result
    written = write_verified(
        doc_path,
        prepared.written,
        prepared.footprint,
        pkg.sha256,
        now=now,
        env=env,
        stage=stage,
    )
    warnings = list(result.warnings)
    op_id: str | None = None
    try:
        op_id = record(
            doc_path,
            kind,
            before_sha256=written.before_sha256,
            after_sha256=written.after_sha256,
            backup=written.backup,
            changes=prepared.changes,
            request=request if request is not None else edits_as_request(edits),
            author=result.author,
            tag=tag,
            footprint={DOCUMENT_PART: result.footprint, **result.parts},
            extra=log_extra,
            now=now,
            env=env,
        )
    except (DocControlError, OSError) as exc:
        warnings.append(f"HISTORY_NOT_RECORDED: the write succeeded but could not be logged: {exc}")
    return replace(
        result,
        before_sha256=written.before_sha256,
        after_sha256=written.after_sha256,
        backup=written.backup,
        warnings=warnings,
        op_id=op_id,
    )


def _bookmark_scopes(
    pkg: Package, index: DocumentIndex, planner: _Planner, ctx: RevisionContext
) -> dict[int, BookmarkScope]:
    """For each move edit, the checked bookmarks of the paragraphs it moves."""
    sources: dict[int, list[Paragraph]] = {}
    for item in planner.new:
        if item.source is not None:
            sources.setdefault(item.edit, []).append(item.source)
    names: dict[str, str] | None = None

    def name_of(bid: str) -> str | None:
        nonlocal names
        if names is None:
            root = ET.fromstring(pkg.read(DOCUMENT_PART))
            tag = f"{{{W_NS}}}bookmarkStart"
            names = {
                e.get(f"{{{W_NS}}}id", ""): e.get(f"{{{W_NS}}}name", "") for e in root.iter(tag)
            }
        return names.get(bid)

    return {
        edit: BookmarkScope(
            [index.spans.parse_paragraph(p.span_index) for p in paras],
            [str(p.address) for p in paras],
            ctx,
            name_of,
        )
        for edit, paras in sources.items()
    }


def _reference_warnings(pkg: Package, scopes: Mapping[int, BookmarkScope]) -> list[str]:
    """A warning naming moved bookmarks that fields or links refer to (rejecting breaks them)."""
    carried = sorted({name for scope in scopes.values() for name in scope.names})
    if not carried:
        return []
    hit = [name for name in carried if name in referenced_bookmarks(pkg)]
    if not hit:
        return []
    return [
        "moved bookmarks are referenced by fields or links: "
        + ", ".join(hit)
        + "; rejecting the move removes them and leaves those references broken"
    ]


def _require_no_live_text(elem: ET.Element, para: Paragraph) -> None:
    """A deleted paragraph must keep no text outside a deletion, hidden text included."""
    live = {"t", "tab", "br", "cr", "noBreakHyphen", "softHyphen", "sym", "ptab"}

    def walk(node: ET.Element, deleted: bool) -> bool:
        for child in node:
            if not isinstance(child.tag, str):
                continue
            local = child.tag.rpartition("}")[2]
            if local == "pPr":
                continue
            if not deleted and local in live:
                return True
            if walk(child, deleted or local == "del"):
                return True
        return False

    if walk(elem, False):
        raise DocControlError(
            ErrorCode.INTERNAL,
            f"self-check: deleting {para.address} left text outside a deletion",
        )


def _expected_text(para: Paragraph, plan: _ParaPlan | None) -> str:
    """The visible text a touched paragraph should have once the edits are in (current state)."""
    if plan is None:
        return para.text
    if plan.delete_all:
        return ""
    text = para.text
    for part in sorted(plan.intervals, key=lambda x: (x.start, x.end), reverse=True):
        text = text[: part.start] + part.text + text[part.end :]
    return text


def _formatted(plan: _ParaPlan | None) -> bool:
    return plan is not None and bool(plan.ppr or plan.formats)


def _properties(p: ET.Element) -> tuple[str, list[tuple[str, str]]]:
    """The paragraph's own properties and the (run properties, character) of its text."""
    ppr = p.find(f"{{{W_NS}}}pPr")
    skip = {f"{{{W_NS}}}{n}" for n in ("rPr", "sectPr", "pPrChange")}
    para = "".join(
        ET.tostring(c, "unicode") for c in (ppr if ppr is not None else []) if c.tag not in skip
    )
    chars: list[tuple[str, str]] = []
    for run in p.iter(f"{{{W_NS}}}r"):
        rpr = run.find(f"{{{W_NS}}}rPr")
        key = ""
        if rpr is not None:
            kept = [c for c in rpr if c.tag != f"{{{W_NS}}}rPrChange"]
            key = "".join(ET.tostring(c, "unicode") for c in kept)
        for child in run:
            local = child.tag.rpartition("}")[2]
            if local in ("t", "delText"):
                chars.extend((key, ch) for ch in "".join(child.itertext()))
            elif local != "rPr":
                chars.append((key, f"<{local}>"))
    return para, chars


def _self_check(
    old: DocumentIndex,
    new_data: bytes,
    ctx: RevisionContext,
    expected: list[str],
    sequence: list[tuple[Paragraph | None, _NewParagraph | None]],
    planner: _Planner,
    story: Story = BODY_STORY,
) -> DocumentIndex:
    """Fail with ``INTERNAL`` unless accepting and rejecting this call's revisions behave.

    Returns the index of the edited story.
    """
    new = build_index(new_data, story.container, story.part, story.key)
    got = [p.text for p in new.paragraphs]
    if got != expected:
        bad = next((n for n, (a, b) in enumerate(zip(got, expected, strict=False), 1) if a != b), 0)
        raise DocControlError(
            ErrorCode.INTERNAL,
            f"self-check: paragraph {bad} of the edited document has unexpected text",
            paragraph=bad,
        )
    ids = set(ctx.revision_ids)
    touched = {
        n
        for n, (para, item) in enumerate(sequence, start=1)
        if item is not None or (para is not None and para.number in planner.plans)
    }
    rejected: dict[int, bytes] = {}
    joins = [False] * len(new)
    for number in sorted(touched):
        para = new.paragraph(number)
        elem = new.spans.parse_paragraph(para.span_index)
        joins[number - 1] = any(kind == "ins" and rid in ids for kind, rid in mark_state(elem))
        reject_revisions(elem, ids)
        original = sequence[number - 1][0]
        if original is not None and _formatted(planner.plans.get(original.number)):
            before = old.spans.parse_paragraph(original.span_index)
            if _properties(before) != _properties(elem):
                raise DocControlError(
                    ErrorCode.INTERNAL,
                    f"self-check: rejecting the revisions does not restore the formatting "
                    f"of {original.address}",
                )
        rejected[para.span_index] = new.spans.serialize(elem, para.span_index)
    restored = build_index(new.spans.splice(rejected), story.container, story.part, story.key)
    merged = _merge_marks([p.text for p in restored.paragraphs], joins)
    if merged != [p.text for p in old.paragraphs]:
        raise DocControlError(
            ErrorCode.INTERNAL, "self-check: rejecting the revisions does not restore the text"
        )
    return new
