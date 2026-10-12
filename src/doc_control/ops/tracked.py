"""Tracked text operations: ``text.replace``, ``para.insert``, ``para.delete``, ``para.move``.

:func:`apply_edits` applies a list of edits to a closed ``.docx`` in one verified
write. Every address in the list refers to the document as it was read (all are
resolved before anything changes), every change is a tracked revision and
nothing outside the edited paragraphs changes.

The four edits
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
    is dropped); a bookmark reaching outside them, and comment anchors, are
    refused. Moving to after the last paragraph of a container
    is refused (use ``before`` its successor or insert text).

Offsets
    Offsets are Python code points of the visible text. A write never splits a
    base character from what attaches to it: a boundary in front of a combining
    mark (Mn, Mc, Me), a zero width joiner or the character after one, an emoji
    modifier, a tag character, or inside a pair of regional indicators raises
    ``ADDRESS_INVALID``.

Several edits in one call
    Edits on different paragraphs are independent. Text replacements in the
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import parse_address
from doc_control.model.index import DocumentIndex, Paragraph, build_index
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
    reject_revisions,
    used_ids,
)
from doc_control.ooxml.splice import W_NS, DocumentSpans
from doc_control.ooxml.zipio import Package
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, Footprint, write_verified
from doc_control.schema import validate

__all__ = [
    "DeleteParagraphs",
    "Edit",
    "InsertParagraph",
    "MoveParagraphs",
    "ReplaceText",
    "TrackedResult",
    "apply_edits",
    "edit_from_dict",
]

PREVIEW_LIMIT = 200
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


Edit = ReplaceText | InsertParagraph | DeleteParagraphs | MoveParagraphs

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

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "applied": self.applied,
            "dry_run": self.dry_run,
            "preview": self.preview,
            "address_map": self.address_map,
            "footprint": {DOCUMENT_PART: self.footprint},
            "revision_ids": self.revision_ids,
            "author": self.author,
            "date": self.date,
        }
        if not self.dry_run:
            out["sha256"] = self.after_sha256
            out["backup"] = str(self.backup) if self.backup else None
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


def _check_text(text: str, what: str) -> None:
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


def _check_name(value: str, what: str) -> None:
    if not value.strip():
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"{what} must not be empty")
    _check_text(value, what)
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
class _ParaPlan:
    para: Paragraph
    intervals: list[_Interval] = field(default_factory=list)
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
    def __init__(self, index: DocumentIndex) -> None:
        self.index = index
        self.layout = _Layout(index)
        self.plans: dict[int, _ParaPlan] = {}
        self.new: list[_NewParagraph] = []
        self.slots: dict[tuple[str, int], int] = {}
        self.previews: list[dict[str, Any]] = []

    # -- bookkeeping --

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

    def _add_interval(self, plan: _ParaPlan, new: _Interval) -> None:
        for old in plan.intervals:
            if old.edit != new.edit and old.clashes(new):
                raise DocControlError(
                    ErrorCode.BATCH_CONFLICT,
                    f"edits {old.edit} and {new.edit} overlap in {plan.para.address}",
                    paragraph=str(plan.para.address),
                    edits=[old.edit, new.edit],
                )
        plan.intervals.append(new)

    def _claim_slot(self, slot: tuple[str, int], edit: int) -> None:
        if slot in self.slots and self.slots[slot] != edit:
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                f"edits {self.slots[slot]} and {edit} insert at the same place",
                edits=[self.slots[slot], edit],
            )
        self.slots[slot] = edit

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
        _check_text(edit.text, "text")
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
            plan = self._plan(para, i, exclusive=para is not last)
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
        _check_text(edit.text, "text")
        where, anchor_at = (
            ("after", edit.after) if edit.after is not None else ("before", edit.before)
        )
        assert anchor_at is not None
        anchor = self.index.resolve_address(parse_address(anchor_at))
        slot, native = self._slot_for(anchor, where, i)
        if native:
            self._plan(anchor, i, exclusive=True).mark = "ins"
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
        slot, native = self._slot_for(target, where, i)
        if native:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"cannot move after {target.address}, the last paragraph of its container: "
                "move before the paragraph that follows it, or insert the text",
                address=target_at,
            )
        for para in paras:
            plan = self._plan(para, i, exclusive=True)
            plan.delete_all = True
            plan.mark = "del"
            plan.moved_by = i
            self.new.append(_NewParagraph(i, slot, para, None, para, False, para.text))
        self.previews.append(
            {
                "op": i,
                "at": edit.at,
                "before": _short("\n".join(p.text for p in paras)),
                "after": f"(moved {where} {target.address})",
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
) -> TrackedResult:
    """Apply ``edits`` to ``doc_path`` as tracked changes in one verified write.

    Raises ``DOC_NOT_FOUND``, ``NOT_A_DOCX``, ``DOC_LOCKED`` or
    ``DOC_CHANGED_ON_DISK`` (also when ``expect_sha256`` differs),
    ``ADDRESS_INVALID``, ``STALE_ADDRESS``, ``UNSUPPORTED_CONTENT``,
    ``BATCH_CONFLICT`` and ``SCHEMA_INVALID`` before touching the file; later
    failures are ``VERIFY_FAILED_RESTORED`` (see :func:`write_verified`). Before the
    write the result is checked: the paragraph texts must be the intended ones
    and rejecting this call's revisions must give back the original texts, or
    ``INTERNAL`` is raised and nothing is written. A dry run does everything but
    the write.
    """
    if not edits:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "no edits given")
    _check_name(author, "author")
    if tag is not None:
        _check_name(tag, "tag")
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    index = DocumentIndex.from_package(pkg)

    planner = _Planner(index)
    for i, edit in enumerate(edits):
        if isinstance(edit, ReplaceText):
            planner.replace(i, edit)
        elif isinstance(edit, InsertParagraph):
            planner.insert(i, edit)
        elif isinstance(edit, DeleteParagraphs):
            planner.delete(i, edit)
        else:
            planner.move(i, edit)

    ctx = RevisionContext(used_ids(pkg), format_author(author, tag), format_date(now))
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
        for part in sorted(plan.intervals, key=lambda x: (x.start, x.end), reverse=True):
            editor.replace(part.start, part.end, part.text)
        if plan.mark:
            editor.mark(plan.mark)
        replacements[para.span_index] = _serialize(spans, elem, para.span_index)

    after: dict[int, list[bytes]] = {}
    before: dict[int, list[bytes]] = {}
    for item in planner.new:
        ref_elem = spans.parse_paragraph(item.ref.span_index)
        p, props = new_paragraph(ref_elem, ctx, inserted_mark=not item.native)
        content: ET.Element | None
        if item.source is not None:
            runs = ParagraphEditor(ref_elem, item.source, ctx).copy_runs(scopes.get(item.edit))
            content = inserted_content(ctx, runs=runs)
        else:
            content = inserted_content(ctx, text=item.text, run_props=props)
        if content is not None:
            p.append(content)
        target = after if item.slot[0] == "after" else before
        target.setdefault(item.slot[1], []).append(_serialize(spans, p, item.slot[1]))

    new_data = spans.splice(replacements, insert_after=after, insert_before=before)
    footprint = Footprint(
        replaced=frozenset(replacements),
        inserted_after=frozenset(after),
        inserted_before=frozenset(before),
    )

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
    new_index = _self_check(index, new_data, ctx, expected, sequence, planner)

    address_map: dict[str, str] = {}
    for number, (old, _) in enumerate(sequence, start=1):
        if old is not None and old.number != number:
            address_map[str(old.address)] = str(new_index.paragraph(number).address)
    result = TrackedResult(
        applied=len(edits),
        dry_run=dry_run,
        preview=planner.previews,
        address_map=address_map,
        footprint=[
            f"p{number}"
            for number, (old, item) in enumerate(sequence, start=1)
            if item is not None or (old is not None and old.number in planner.plans)
        ],
        revision_ids=[int(r) for r in ctx.revision_ids],
        author=ctx.author,
        date=ctx.date,
        before_sha256=pkg.sha256,
    )
    if dry_run:
        return result
    written = write_verified(
        doc_path,
        {DOCUMENT_PART: new_data},
        footprint,
        pkg.sha256,
        now=now,
        env=env,
        stage=stage,
    )
    return replace(
        result,
        before_sha256=written.before_sha256,
        after_sha256=written.after_sha256,
        backup=written.backup,
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


def _self_check(
    old: DocumentIndex,
    new_data: bytes,
    ctx: RevisionContext,
    expected: list[str],
    sequence: list[tuple[Paragraph | None, _NewParagraph | None]],
    planner: _Planner,
) -> DocumentIndex:
    """Fail with ``INTERNAL`` unless accepting and rejecting this call's revisions behave.

    Returns the index of the edited document.
    """
    new = build_index(new_data)
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
        rejected[para.span_index] = new.spans.serialize(elem, para.span_index)
    restored = build_index(new.spans.splice(rejected))
    merged = _merge_marks([p.text for p in restored.paragraphs], joins)
    if merged != [p.text for p in old.paragraphs]:
        raise DocControlError(
            ErrorCode.INTERNAL, "self-check: rejecting the revisions does not restore the text"
        )
    return new
