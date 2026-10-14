"""``revision.accept`` and ``revision.reject``: resolve tracked changes in one verified write.

Both are batch operations (``batch`` with ``{"op": "revision.accept", ...}``); they work on
revisions the tool wrote and on revisions the document already had, whoever the author.

Selection (at least one; they narrow each other)
    ``ids``     revision ids as ``revisions.list`` reports them;
    ``author``  the exact ``w:author`` value (``Claude (tag)`` for a tagged call);
    ``at``      a paragraph address ``p3#fp`` or a run ``p3#fp..p9#fp`` (inclusive);
    ``all``     true: every revision.

``ids``, ``author`` and ``all`` reach every story (body, notes, headers, footers); ``at`` may
be a story range (``fn3/p1#fp..fn3/p2#fp``) and stays in that story.

What happens is described in :mod:`doc_control.ooxml.resolve`. In short: an insertion or
move-in is kept (accept) or removed (reject), a deletion or move-away the other way
round; a formatting change keeps the new (accept) or restores the recorded (reject)
properties; a paragraph mark that goes away joins the paragraph with the next one in
its container (the next paragraph's properties win, as in Word). A move is two
revisions, ``move_from`` and ``move_to``: select both. The last paragraph of a cell or
of the body cannot lose its mark: it stays, and the response says so in ``warnings``. Nor
can a paragraph that ends a section, unless the break is one ``layout.apply`` tracked (its
history log names the mark revision): then accepting the deletion of its mark (or rejecting
the insertion of the break) removes the section break, and the text before it joins the
section after it. A section property change (``section_format``, a ``w:sectPrChange`` in a
paragraph's ``w:sectPr`` or in the body's own, which is reported at the last paragraph) puts
the recorded page setup back when rejected; header and footer references are not part of it.

Several operations in one batch are all resolved against the document as read; the
same revision accepted by one and rejected by another is a ``BATCH_CONFLICT``. They
cannot be combined with text edits (``BATCH_CONFLICT``): addresses would shift.

Table row revisions (``w:trPr/w:ins``, ``w:trPr/w:del``) are revisions like the others,
reported (and selected by ``at``) at the first paragraph of the row. Accepting a deleted row
or rejecting an inserted one removes the ``w:tr``, with the paragraphs in it; accepting an
inserted row or rejecting a deleted one drops the marker and leaves the row's own text
revisions to their own decisions. A decision that leaves a table without rows removes the
whole table, and is ``UNSUPPORTED_CONTENT`` when no paragraph follows the table. Table
cell revisions (``w:tcPr``) and revisions inside text boxes are not resolved; selecting
them by id is ``UNSUPPORTED_CONTENT``, and with other selectors they are left as they are
and a warning names the fact.

Before the write the result is checked: no revision appears that was not there, the
selected ones are gone, the paragraph count is right, accepting insertions and
deletions leaves every paragraph's current text unchanged (merged as the marks say) and
no field is left open. A failed check raises ``INTERNAL`` and nothing is written.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex, Paragraph, build_index
from doc_control.model.stories import BODY_STORY, Story, find_story, list_stories, story_index
from doc_control.ooxml import resolve as xml
from doc_control.ooxml.revisions import DATE, PPR
from doc_control.ooxml.splice import Container, scan
from doc_control.ooxml.zipio import Package
from doc_control.ops.revision_rows import RowPlan, RowScan, apply_rows, plan_rows, scan_rows
from doc_control.ops.tracked import next_in_container, story_of
from doc_control.safety.history import has_live_text, log_path_for, read_log, record, to_text
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, Footprint, StoryFootprint, write_verified
from doc_control.schema import validate

__all__ = [
    "OPERATION_SCHEMAS",
    "PreparedResolution",
    "ResolutionResult",
    "ResolveRevisions",
    "apply_resolutions",
    "prepare_resolutions",
    "resolution_from_dict",
]

LISTED = 100  # revisions named one by one in a response
_REVISION_TAG = re.compile(
    rb"<(?:[\w.-]+:)?(?:ins|del|moveFrom|moveTo|rPrChange|pPrChange|sectPrChange)[\s/>]"
)
_SECT_CHANGE_TAG = re.compile(rb"<(?:[\w.-]+:)?sectPrChange[\s/>]")
_CELL_REVISION = re.compile(rb"<(?:[\w.-]+:)?(?:cellIns|cellDel|cellMerge)[\s/>]")
_ROW_REVISION = re.compile(
    rb"<(?:[\w.-]+:)?trPr[\s>](?:(?!</(?:[\w.-]+:)?trPr>).)*?<(?:[\w.-]+:)?(?:ins|del)[\s/>]",
    re.DOTALL,
)
_NOTES_PART = re.compile(r"word/(?:foot|end)notes\d*\.xml")
_PAGE_PART = re.compile(r"word/(?:header|footer)\d*\.xml")
_MOVE_RANGE = re.compile(rb"<(?:[\w.-]+:)?move(?:From|To)Range(?:Start|End)[\s/>]")
_FIELD_CHAR = re.compile(rb"fldCharType\s*=\s*[\"'](begin|end)[\"']")
_MARK_KINDS = frozenset({"mark_insert", "mark_delete", "mark_move_from", "mark_move_to"})
_TEXT_NEUTRAL = frozenset(
    {
        "insert",
        "delete",
        "format",
        "mark_insert",
        "mark_delete",
        "mark_format",
        "paragraph_format",
        "section_format",
    }
)


def _schema(action: str, what: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "op": {"enum": [f"revision.{action}"]},
            "ids": {
                "type": "array",
                "items": {"type": ["string", "integer"]},
                "description": f"{what} the revisions with these ids (from revisions.list).",
            },
            "author": {"type": "string", "minLength": 1, "description": "Exact revision author."},
            "at": {
                "type": "string",
                "minLength": 1,
                "description": "Paragraph p3#fp or run p3#fp..p9#fp.",
            },
            "all": {"type": "boolean", "description": "true: every revision in the document."},
        },
        "required": ["op"],
        "additionalProperties": False,
    }


OPERATION_SCHEMAS: dict[str, dict[str, Any]] = {
    "revision.accept": _schema("accept", "Accept"),
    "revision.reject": _schema("reject", "Reject"),
}


@dataclass(frozen=True)
class ResolveRevisions:
    action: str  # "accept" | "reject"
    ids: tuple[str, ...] | None = None
    author: str | None = None
    at: str | None = None
    all: bool = False


def resolution_from_dict(raw: Mapping[str, Any]) -> ResolveRevisions:
    """Validate a ``revision.accept``/``revision.reject`` object and build the operation."""
    op = raw.get("op")
    if not isinstance(op, str) or op not in OPERATION_SCHEMAS:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"unknown revision operation: {op!r}",
            expected=sorted(OPERATION_SCHEMAS),
        )
    validate(dict(raw), OPERATION_SCHEMAS[op])
    ids = raw.get("ids")
    if ids is not None and not ids:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"{op}: 'ids' must not be empty")
    everything = raw.get("all") is True
    if ids is None and raw.get("author") is None and raw.get("at") is None and not everything:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"{op} needs a selection: 'ids', 'author', 'at' or 'all': true",
        )
    return ResolveRevisions(
        op.removeprefix("revision."),
        tuple(str(i).strip() for i in ids) if ids is not None else None,
        raw.get("author"),
        raw.get("at"),
        everything,
    )


# -- results ------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolutionResult:
    applied: int
    dry_run: bool
    preview: list[dict[str, Any]]
    revisions: list[dict[str, str]]
    accepted: int
    rejected: int
    merged_paragraphs: int
    address_map: dict[str, str]
    footprint: list[str]
    before_sha256: str
    after_sha256: str | None = None
    backup: Path | None = None
    warnings: list[str] = field(default_factory=list)
    op_id: str | None = None
    parts: dict[str, list[str]] = field(default_factory=dict)  # story parts touched

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "applied": self.applied,
            "dry_run": self.dry_run,
            "preview": self.preview,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "revisions": self.revisions,
            "merged_paragraphs": self.merged_paragraphs,
            "address_map": self.address_map,
            "footprint": {DOCUMENT_PART: self.footprint, **self.parts},
        }
        if self.accepted + self.rejected > len(self.revisions):
            out["revisions_omitted"] = self.accepted + self.rejected - len(self.revisions)
        if self.warnings:
            out["warnings"] = self.warnings
        if not self.dry_run:
            out["sha256"] = self.after_sha256
            out["backup"] = str(self.backup) if self.backup else None
            if self.op_id:
                out["op_id"] = self.op_id
        return out


@dataclass(frozen=True)
class PreparedResolution:
    """The resolved ``word/document.xml`` of a package, checked but not written."""

    pkg: Package
    new_data: bytes | None  # None: nothing to change
    footprint: Footprint
    result: ResolutionResult
    changes: list[dict[str, Any]] = field(default_factory=list)
    """The undo-log record: each rewritten paragraph's bytes before and after."""
    joined: bool = False  # paragraphs were merged; the log cannot restore them
    written: dict[str, bytes] = field(default_factory=dict)
    """Every part the resolution changes (the body, notes, headers), with its new bytes."""
    rows: RowPlan = field(default_factory=RowPlan)
    """The table rows removed or unmarked; the undo log cannot restore them."""


# -- selection --------------------------------------------------------------------------


@dataclass(eq=False)
class _Parsed:
    para: Paragraph
    elem: ET.Element
    revs: list[xml.Revision]


def _parse_revisions(index: DocumentIndex) -> list[_Parsed]:
    spans = index.spans
    out: list[_Parsed] = []
    for para in index.paragraphs:
        if not _REVISION_TAG.search(spans.paragraph_bytes(para.span_index)):
            continue
        elem = spans.parse_paragraph(para.span_index)
        revs = xml.scan(elem)
        if revs:
            out.append(_Parsed(para, elem, revs))
    return out


def _range(index: DocumentIndex, at: str) -> tuple[int, int]:
    first, sep, last = at.partition("..")
    a = index.resolve_address(first)
    if not sep:
        return a.number, a.number
    b = index.resolve_address(last)
    if b.number < a.number:
        raise DocControlError(
            ErrorCode.ADDRESS_INVALID, f"{at!r} ends before it starts", address=at
        )
    return a.number, b.number


def _table_revisions(data: bytes, rows: RowScan) -> bool:
    """Table revisions this operation cannot resolve are in ``data``: cell revisions, or a
    marked row the row scan did not find."""
    marked = sum(1 for _ in _ROW_REVISION.finditer(data))
    return _CELL_REVISION.search(data) is not None or marked > len(
        {r.ordinal for r in rows.revisions}
    )


def _choose_rows(
    ops: Sequence[ResolveRevisions], index: DocumentIndex, rows: RowScan
) -> tuple[dict[int, str], list[int]]:
    """Which row revisions each operation selects: ``({marker: action}, per-op counts)``."""
    chosen: dict[int, tuple[str, int]] = {}
    picked = [0] * len(ops)
    for i, op in enumerate(ops):
        if op.at is not None and story_of(op.at) != index.story:
            continue
        low, high = _range(index, op.at) if op.at is not None else (0, len(index))
        wanted = set(op.ids) if op.ids is not None else None
        for j, r in enumerate(rows.revisions):
            if not low <= r.first <= high:
                continue
            if (wanted is not None and r.id not in wanted) or (
                op.author is not None and r.author != op.author
            ):
                continue
            old = chosen.get(j)
            if old is not None and old[0] != op.action:
                where = str(index.paragraph(r.first).address)
                raise DocControlError(
                    ErrorCode.BATCH_CONFLICT,
                    f"edits {old[1]} and {i} accept and reject the same revision "
                    f"({r.kind} {r.id}) in {where}",
                    paragraph=where,
                    revision=r.id,
                    edits=sorted((old[1], i)),
                )
            chosen.setdefault(j, (op.action, i))
            picked[i] += 1
    return {j: action for j, (action, _) in chosen.items()}, picked


def _select(
    op: ResolveRevisions,
    index: DocumentIndex,
    parsed: Sequence[_Parsed],
    dead: frozenset[int] = frozenset(),
) -> list[tuple[_Parsed, xml.Revision]]:
    if op.at is not None and story_of(op.at) != index.story:
        return []  # a range in another story
    low, high = _range(index, op.at) if op.at is not None else (0, len(index))
    wanted = set(op.ids) if op.ids is not None else None
    return [
        (p, r)
        for p in parsed
        if low <= p.para.number <= high and p.para.number not in dead
        for r in p.revs
        if (wanted is None or r.id in wanted) and (op.author is None or r.author == op.author)
    ]


def _check_ids(ops: Sequence[ResolveRevisions], known: set[str], table: bool) -> None:
    for op in ops:
        for missing in [i for i in op.ids or () if i not in known]:
            if table:
                raise DocControlError(
                    ErrorCode.UNSUPPORTED_CONTENT,
                    f"no paragraph or row revision has id {missing}; table cell revisions "
                    "are not resolved by this operation",
                    revision=missing,
                )
            raise DocControlError(
                ErrorCode.STALE_ADDRESS,
                f"the document has no revision with id {missing}",
                revision=missing,
            )


def _stories(
    pkg: Package, ops: Sequence[ResolveRevisions], index: DocumentIndex | None
) -> list[tuple[Story, DocumentIndex]]:
    """The stories ``ops`` may select in: the one a range names, else every story that
    holds a revision (the body always)."""
    keys = {story_of(op.at) for op in ops if op.at is not None}
    everywhere = any(op.at is None for op in ops)
    out: list[tuple[Story, DocumentIndex]] = []
    if everywhere or "" in keys:
        out.append((BODY_STORY, index if index is not None else DocumentIndex.from_package(pkg)))
    if everywhere:
        kinds = tuple(
            kind
            for kind, pattern in (("notes", _NOTES_PART), ("headers", _PAGE_PART))
            if any(pattern.fullmatch(n) and _REVISION_TAG.search(pkg.read(n)) for n in pkg.names)
        )
        stories = list_stories(pkg, kinds) if kinds else []
    else:
        stories = [find_story(pkg, key) for key in sorted(keys - {""})]
    seen: set[tuple[str, Container]] = set()  # two sections may name one header part
    for story in stories:
        if (story.part, story.container) in seen:
            continue
        if everywhere and not _REVISION_TAG.search(pkg.read(story.part)):
            continue
        seen.add((story.part, story.container))
        out.append((story, story_index(pkg, story)))
    return out


@dataclass
class _StoryResolution:
    story: Story
    index: DocumentIndex
    parsed: list[_Parsed]
    chosen: dict[tuple[int, int], tuple[str, int]]
    picked: list[int]  # per operation, how many revisions it selected here
    replacements: dict[int, bytes] = field(default_factory=dict)
    deletes: set[int] = field(default_factory=set)
    gone: set[int] = field(default_factory=set)
    elems: dict[int, ET.Element] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    new_data: bytes | None = None
    address_map: dict[str, str] = field(default_factory=dict)


@dataclass
class _BodySect:
    """The ``w:sectPrChange`` of the body's own ``w:sectPr`` (the last section's), which no
    paragraph holds: reported at the last paragraph, resolved on its own bytes."""

    rev: xml.Revision
    at: str  # address of the last paragraph
    action: str | None = None
    op: int = -1


def _body_section(index: DocumentIndex) -> _BodySect | None:
    spans = index.spans
    if spans.body_sect is None or not len(index):
        return None
    start, end = spans.body_sect
    if not _SECT_CHANGE_TAG.search(spans.data, start, end):
        return None
    found = [r for r in xml.scan_section(spans.parse_body_sect())]
    if not found:
        return None
    return _BodySect(found[0], str(index.paragraph(len(index)).address))


def _choose_body_section(
    ops: Sequence[ResolveRevisions], index: DocumentIndex, sect: _BodySect, picked: list[int]
) -> None:
    for i, op in enumerate(ops):
        if op.at is not None:
            if story_of(op.at) != index.story:
                continue
            low, high = _range(index, op.at)
            if not low <= len(index) <= high:
                continue
        if op.ids is not None and sect.rev.id not in op.ids:
            continue
        if op.author is not None and sect.rev.author != op.author:
            continue
        if sect.action is not None and sect.action != op.action:
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                f"edits {sect.op} and {i} accept and reject the same revision "
                f"({sect.rev.kind} {sect.rev.id}) in {sect.at}",
                paragraph=sect.at,
                revision=sect.rev.id,
                edits=sorted((sect.op, i)),
            )
        if sect.action is None:
            sect.action, sect.op = op.action, i
        picked[i] += 1


def _resolve_body_section(data: bytes, action: str) -> bytes:
    """``data`` (a ``word/document.xml``) with the body's section property change resolved."""
    spans = scan(data)
    elem = spans.parse_body_sect()
    change = elem.find(xml.SECT_CHANGE)
    if change is None:
        return data
    xml.resolve_section(elem, change, action)
    return spans.replace_body_sect(spans.serialize_body_sect(elem))


# -- preparing ------------------------------------------------------------------------------


def _fields_balanced(data: bytes) -> bool:
    kinds = [m.group(1) for m in _FIELD_CHAR.finditer(data)]
    return kinds.count(b"begin") == kinds.count(b"end")


def _with_content(elem: ET.Element, content: list[ET.Element]) -> None:
    """Put ``content`` first among the paragraph's content (after its properties)."""
    at = 1 if len(elem) and elem[0].tag == PPR else 0
    elem[at:at] = content


def prepare_resolutions(
    pkg: Package,
    ops: Sequence[ResolveRevisions],
    *,
    dry_run: bool = False,
    index: DocumentIndex | None = None,
    own_breaks: frozenset[tuple[str, str, str]] = frozenset(),
) -> PreparedResolution:
    """Plan and build ``ops`` on ``pkg`` without writing: the part of
    :func:`apply_resolutions` that works on bytes, self-checked (see the module docstring).

    ``ids``, ``author`` and ``all`` select in every story (the body, notes, headers and
    footers); a range ``at`` in its own story. ``index`` is the body's. ``own_breaks``: the
    (id, author, date) of the section-break marks the section writer tracked, the only
    section-closer marks that may go away (:func:`own_break_marks`)."""
    if not ops:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "no operations given")
    found = _stories(pkg, ops, index)
    body_index = next((idx for story, idx in found if not story.key), None)
    rows = scan_rows(body_index) if body_index is not None else RowScan([], {})
    row_actions, row_picked = (
        _choose_rows(ops, body_index, rows) if body_index is not None else ({}, [0] * len(ops))
    )
    plan = plan_rows(rows, row_actions)
    table = any(_table_revisions(idx.spans.data, rows) for story, idx in found if not story.key)
    resolved: list[_StoryResolution] = []
    for story, idx in found:
        parsed = _parse_revisions(idx)
        resolved.append(_choose(story, idx, parsed, ops, frozenset() if story.key else plan.dead))
    body = next((res for res in resolved if not res.story.key), None)
    body_sect = _body_section(body.index) if body is not None else None
    if body is not None:
        body.picked = [a + b for a, b in zip(body.picked, row_picked, strict=True)]
    if body is not None and body_sect is not None:
        _choose_body_section(ops, body.index, body_sect, body.picked)
    known = {r.id for res in resolved for p in res.parsed for r in p.revs}
    known |= {r.id for r in rows.revisions}
    _check_ids(ops, known | ({body_sect.rev.id} if body_sect else set()), table)

    warnings: list[str] = []
    if table:
        warnings.append(
            "some table cell or row revisions are not resolved by this operation; "
            "they stay as they are"
        )
    for res in resolved:
        _build(pkg, res, frozenset() if res.story.key else own_breaks)
        warnings += res.warnings

    preview = [
        {"op": i, "action": op.action, "revisions": sum(res.picked[i] for res in resolved)}
        for i, op in enumerate(ops)
    ]
    shown: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    if body_sect is not None and body_sect.action is not None:
        counts[body_sect.action] += 1
        shown.append(
            {
                "id": body_sect.rev.id,
                "type": body_sect.rev.kind,
                "author": body_sect.rev.author,
                "at": body_sect.at,
                "action": body_sect.action,
            }
        )
    if body is not None:
        for j, action in sorted(row_actions.items()):
            row = rows.revisions[j]
            counts[action] += 1
            if len(shown) < LISTED:
                shown.append(
                    {
                        "id": row.id,
                        "type": row.kind,
                        "author": row.author,
                        "at": str(body.index.paragraph(row.first).address),
                        "action": action,
                    }
                )
    for res in resolved:
        counts.update(action for action, _ in res.chosen.values())
        for p in res.parsed:
            for k, r in enumerate(p.revs):
                if (p.para.number, k) in res.chosen and len(shown) < LISTED:
                    shown.append(
                        {
                            "id": r.id,
                            "type": r.kind,
                            "author": r.author,
                            "at": str(p.para.address),
                            "action": res.chosen[(p.para.number, k)][0],
                        }
                    )
    written: dict[str, bytes] = {}
    parts: dict[str, list[str]] = {}
    stories: list[StoryFootprint] = []
    changes: list[dict[str, Any]] = []
    for res in resolved:
        story = res.story
        if res.new_data is not None:
            part = story.part
            written[part] = (
                scan(written[part], story.container, part).splice(
                    res.replacements, delete=res.deletes
                )
                if part in written
                else res.new_data
            )
            if story.key:
                stories.append(
                    StoryFootprint(
                        part, story.container, replaced=frozenset(res.replacements) | res.deletes
                    )
                )
                prefix = f"{story.key}/"
                parts.setdefault(part, []).extend(
                    f"{prefix}p{res.index.paragraph(n).number}" for n in sorted(res.elems)
                )
        spans = res.index.spans
        changes += [
            {
                "part": story.part,
                **({"story": story.key} if story.key else {}),
                "kind": "replace",
                "index": span,
                "before": to_text(spans.paragraph_bytes(span)),
                "after": to_text(data),
                "text_before": next(p.text for p in res.index.paragraphs if p.span_index == span),
                "deleted": not has_live_text(data),
            }
            for span, data in sorted(res.replacements.items())
        ]
    resolves_body_sect = body_sect is not None and body_sect.action is not None
    if body_sect is not None and body_sect.action is not None:
        base = written.get(DOCUMENT_PART)
        if base is None and body is not None:
            base = body.index.spans.data
        assert base is not None
        written[DOCUMENT_PART] = _resolve_body_section(base, body_sect.action)
    address_map = {k: v for res in resolved for k, v in res.address_map.items()}
    if body is not None and plan:
        base = written.get(DOCUMENT_PART) or body.index.spans.data
        final = apply_rows(build_index(base), plan)
        written[DOCUMENT_PART] = final
        address_map = {
            k: v for res in resolved if res is not body for k, v in res.address_map.items()
        }
        address_map |= _address_map(body.index, build_index(final), body.gone, plan.dead)
    footprint = Footprint(
        replaced=frozenset(body.replacements) | frozenset(body.deletes) if body else frozenset(),
        stories=tuple(stories),
        body_section=resolves_body_sect,
        spans=plan.spans,
    )
    result = ResolutionResult(
        applied=len(ops),
        dry_run=dry_run,
        preview=preview,
        revisions=shown,
        accepted=counts["accept"],
        rejected=counts["reject"],
        merged_paragraphs=sum(len(res.gone) for res in resolved),
        address_map=address_map,
        footprint=[f"p{body.index.paragraph(n).number}" for n in sorted(body.elems)]
        if body
        else [],
        before_sha256=pkg.sha256,
        warnings=warnings,
        parts=parts,
    )
    joined = any(res.gone for res in resolved)
    new_data = body.new_data if body else None
    return PreparedResolution(pkg, new_data, footprint, result, changes, joined, written, plan)


def _choose(
    story: Story,
    index: DocumentIndex,
    parsed: list[_Parsed],
    ops: Sequence[ResolveRevisions],
    dead: frozenset[int] = frozenset(),
) -> _StoryResolution:
    """Which revisions of one story each operation selects, refusing contradictions."""
    chosen: dict[tuple[int, int], tuple[str, int]] = {}  # (paragraph, position) -> (action, op)
    picked_counts: list[int] = []
    where = {id(r): (p, k) for p in parsed for k, r in enumerate(p.revs)}
    for i, op in enumerate(ops):
        picked = _select(op, index, parsed, dead)
        for p, r in picked:
            key = (p.para.number, where[id(r)][1])
            old = chosen.get(key)
            if old is not None and old[0] != op.action:
                raise DocControlError(
                    ErrorCode.BATCH_CONFLICT,
                    f"edits {old[1]} and {i} accept and reject the same revision "
                    f"({r.kind} {r.id}) in {p.para.address}",
                    paragraph=str(p.para.address),
                    revision=r.id,
                    edits=sorted((old[1], i)),
                )
            chosen.setdefault(key, (op.action, i))
        picked_counts.append(len(picked))
    return _StoryResolution(story, index, parsed, chosen, picked_counts)


def _build(
    pkg: Package, res: _StoryResolution, own_breaks: frozenset[tuple[str, str, str]] = frozenset()
) -> None:
    """Resolve the chosen revisions of one story, joining paragraphs that lose their mark
    (a section closer only when ``own_breaks`` names its mark revision)."""
    index, parsed, chosen = res.index, res.parsed, res.chosen
    spans = index.spans
    # Decide, paragraph by paragraph.
    removed_marks: set[int] = set()  # paragraph numbers whose mark a decision removes
    own_marks: set[int] = set()  # of those, closers whose break the section writer tracked
    touched: dict[int, _Parsed] = {}
    for p in parsed:
        actions = {
            id(r.elem): chosen[(p.para.number, k)][0]
            for k, r in enumerate(p.revs)
            if (p.para.number, k) in chosen
        }
        if not actions:
            continue
        own = any(
            r.kind in _MARK_KINDS
            and (r.id, r.author, r.elem.get(DATE) or "") in own_breaks
            and (p.para.number, k) in chosen
            for k, r in enumerate(p.revs)
        )
        if xml.resolve_paragraph(p.elem, actions):
            removed_marks.add(p.para.number)
            if own:
                own_marks.add(p.para.number)
        touched[p.para.number] = p

    elems: dict[int, ET.Element] = {n: p.elem for n, p in touched.items()}

    # Move range markers whose side has no revision left mean nothing: drop them.
    left = {
        "from" if r.kind.endswith("move_from") else "to"
        for p in parsed
        for k, r in enumerate(p.revs)
        if r.kind.endswith(("move_from", "move_to")) and (p.para.number, k) not in chosen
    }
    cleared = {"from", "to"} - left
    if cleared and (chosen or not res.story.key):  # the body as before; a story when picked
        for para in index.paragraphs:
            raw = spans.paragraph_bytes(para.span_index)
            if _MOVE_RANGE.search(raw):
                elem = elems.get(para.number)
                if elem is None:
                    elem = spans.parse_paragraph(para.span_index)
                if xml.drop_move_markers(elem, cleared):
                    elems[para.number] = elem

    # Join paragraphs whose mark went away with the one after them.
    carry: dict[int, list[ET.Element]] = {}
    gone: set[int] = set()  # paragraph numbers merged into the next
    for number in sorted(removed_marks):
        para = index.paragraph(number)
        elem = elems[number]
        nxt = next_in_container(index, para)
        inherited = carry.pop(number, [])
        content = inherited + [c for c in elem if c.tag != PPR]
        closer = elem.find(f"{PPR}/{xml.SECT_PR}") is not None and number not in own_marks
        if nxt is None or closer:
            _with_content(elem, inherited)  # nothing to join with: keep the mark
            why = "a container" if nxt is None else "a section layout.apply did not break"
            res.warnings.append(f"{para.address} keeps its paragraph mark: it ends {why}")
            continue
        if nxt.number not in elems:
            elems[nxt.number] = spans.parse_paragraph(nxt.span_index)
        carry.setdefault(nxt.number, []).extend(content)
        gone.add(number)
    for number, content in carry.items():
        _with_content(elems[number], content)

    for number, elem in elems.items():
        span = index.paragraph(number).span_index
        if number in gone:
            res.deletes.add(span)
        else:
            res.replacements[span] = spans.serialize(elem, span)
    res.elems, res.gone = elems, gone
    if elems:
        res.new_data = spans.splice(res.replacements, delete=res.deletes)
        new_index = _self_check(pkg, index, res.new_data, parsed, chosen, gone, touched, res.story)
        res.address_map = _address_map(index, new_index, gone)


def _address_map(
    old: DocumentIndex, new: DocumentIndex, gone: set[int], dead: frozenset[int] = frozenset()
) -> dict[str, str]:
    """Old to new address of every paragraph whose number changed (a joined one: where it
    went; one in a removed table row has none)."""
    out: dict[str, str] = {}
    number = 0
    pending: list[Paragraph] = []
    for para in old.paragraphs:
        if para.number in dead:
            continue
        if para.number in gone:
            pending.append(para)
            continue
        number += 1
        target = str(new.paragraph(number).address)
        for p in [*pending, para]:
            if str(p.address) != target and (p.number != number or p in pending):
                out[str(p.address)] = target
        pending = []
    return out


def _self_check(
    pkg: Package,
    old: DocumentIndex,
    new_data: bytes,
    parsed: Sequence[_Parsed],
    chosen: Mapping[tuple[int, int], tuple[str, int]],
    gone: set[int],
    touched: Mapping[int, _Parsed],
    story: Story = BODY_STORY,
) -> DocumentIndex:
    new = build_index(new_data, story.container, story.part, story.key)
    if len(new) != len(old) - len(gone):
        raise DocControlError(
            ErrorCode.INTERNAL, "self-check: the resolved document has the wrong paragraph count"
        )
    before = Counter((r.id, r.kind, r.author) for p in parsed for r in p.revs)
    picked = Counter(
        (r.id, r.kind, r.author)
        for p in parsed
        for k, r in enumerate(p.revs)
        if (p.para.number, k) in chosen
    )
    after: Counter[tuple[str, str, str]] = Counter()
    for _, elem in _revision_paragraphs(new):
        after.update((r.id, r.kind, r.author) for r in xml.scan(elem))
    for key, n in after.items():
        if n > max(0, before[key] - picked[key]):
            raise DocControlError(
                ErrorCode.INTERNAL, "self-check: a selected revision is still in the document"
            )
    old_data = old.spans.data
    if _fields_balanced(old_data) and not _fields_balanced(new_data):
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            "resolving these revisions would leave a field open: a deletion cuts through one",
        )
    kinds = {r.kind for p in parsed for k, r in enumerate(p.revs) if (p.para.number, k) in chosen}
    actions = {a for a, _ in chosen.values()}
    if actions == {"accept"} and kinds <= _TEXT_NEUTRAL:
        expected = _joined(
            [p.text for p in old.paragraphs], [p.number in gone for p in old.paragraphs]
        )
        if expected != [p.text for p in new.paragraphs]:
            raise DocControlError(
                ErrorCode.INTERNAL, "self-check: accepting changed the current text"
            )
    return new


def _revision_paragraphs(index: DocumentIndex) -> list[tuple[Paragraph, ET.Element]]:
    out: list[tuple[Paragraph, ET.Element]] = []
    for para in index.paragraphs:
        if _REVISION_TAG.search(index.spans.paragraph_bytes(para.span_index)):
            out.append((para, index.spans.parse_paragraph(para.span_index)))
    return out


def _joined(texts: Sequence[str], joins: Sequence[bool]) -> list[str]:
    out: list[str] = []
    carry = ""
    for text, join in zip(texts, joins, strict=True):
        text = carry + text
        if join:
            carry = text
        else:
            out.append(text)
            carry = ""
    if carry:
        out.append(carry)
    return out


# -- applying -----------------------------------------------------------------------------------


def apply_resolutions(
    doc_path: Path,
    ops: Sequence[ResolveRevisions],
    *,
    expect_sha256: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    stage: Callable[[str], None] | None = None,
) -> ResolutionResult:
    """Resolve revisions in ``doc_path`` in one verified write.

    Raises the errors of :func:`doc_control.ops.tracked.apply_edits` before touching the
    file. When the selection matches nothing the file is not written.
    """
    if not ops:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "no operations given")
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    prepared = prepare_resolutions(
        pkg, ops, dry_run=dry_run, own_breaks=own_break_marks(doc_path, env)
    )
    result = prepared.result
    if dry_run or not prepared.written:
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
            "batch",
            before_sha256=written.before_sha256,
            after_sha256=written.after_sha256,
            backup=written.backup,
            changes=prepared.changes,
            request=[_as_request(op) for op in ops],
            footprint={DOCUMENT_PART: result.footprint, **result.parts},
            # Joined paragraphs are not logged, so `undo` refuses this entry (safety.history).
            extra=(
                {"undo_by_backup": True}
                if prepared.footprint.body_section or prepared.rows
                else {"parts": ["joined paragraphs"]}
                if prepared.joined
                else None
            ),
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


def own_break_marks(
    doc_path: Path, env: Mapping[str, str] | None = None
) -> frozenset[tuple[str, str, str]]:
    """The (id, author, date) of every section-break mark revision the section writer
    tracked in ``doc_path``, from its history log (``break_marks``). An undone write counts
    too: its bytes may be put back, and the three values together name the mark."""
    try:
        log = read_log(log_path_for(doc_path, env))
    except (DocControlError, OSError):
        return frozenset()
    return frozenset(
        (str(m.get("id")), str(m.get("author")), str(m.get("date")))
        for entry in log.entries
        if isinstance(entry.get("break_marks"), list)
        for m in entry["break_marks"]
        if isinstance(m, dict)
    )


def _as_request(op: ResolveRevisions) -> dict[str, Any]:
    out: dict[str, Any] = {"op": f"revision.{op.action}"}
    if op.ids is not None:
        out["ids"] = list(op.ids)
    for key in ("author", "at"):
        if getattr(op, key) is not None:
            out[key] = getattr(op, key)
    if op.all:
        out["all"] = True
    return out
