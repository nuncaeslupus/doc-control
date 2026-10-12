"""Comment operations of a batch: ``comment.add``, ``comment.reply``, ``comment.resolve``.

They are batch operation kinds, not standalone writes (see :mod:`doc_control.ops.batch`).

``comment.add`` anchors a new comment on a range (``at``: a paragraph, a range inside
one or across paragraphs, as for ``text.replace``) with ``commentRangeStart``,
``commentRangeEnd`` and a ``commentReference`` run. ``comment.reply`` takes the id
``comments.list`` reports; the reply is anchored on the same range as the thread's root
(a reply to a reply joins the thread). ``comment.resolve`` marks the whole thread
resolved (``resolved: false`` reopens it). All of them keep ``comments.xml``,
``commentsExtended.xml``, ``commentsIds.xml``, ``commentsExtensible.xml`` and
``people.xml`` consistent, creating the parts (with their content types and
relationships) when the document has none; see :mod:`doc_control.ooxml.commentparts`.

A comment is placed on a paragraph that no other operation of the same batch edits,
deletes or moves (``BATCH_CONFLICT``); apply such pairs as two batches. Comment ids
are above every ``w:id`` of the document, and above the ids the batch's revisions get.
"""

from __future__ import annotations

import bisect
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex, Paragraph
from doc_control.model.styles import load_styles
from doc_control.ooxml.commentparts import CommentParts
from doc_control.ooxml.revisions import (
    DEFAULT_AUTHOR,
    ParagraphEditor,
    RevisionContext,
    format_author,
    format_date,
    used_ids,
)
from doc_control.ooxml.zipio import Package
from doc_control.ops.tracked import (
    Annotator,
    TrackedResult,
    boundary_ok,
    check_name,
    check_text,
    prepare_edits,
)
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, write_verified
from doc_control.schema import validate

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
_PREVIEW = 120


@dataclass(frozen=True)
class AddComment:
    at: str
    text: str


@dataclass(frozen=True)
class ReplyComment:
    comment: str
    text: str


@dataclass(frozen=True)
class ResolveComment:
    comment: str
    resolved: bool = True


CommentEdit = AddComment | ReplyComment | ResolveComment

_STR: dict[str, Any] = {"type": "string", "minLength": 1}
COMMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "comment.add": {
        "type": "object",
        "properties": {
            "op": {"enum": ["comment.add"]},
            "at": {**_STR, "description": "Range to comment on: p3#fp, p3#fp:10-15 or across."},
            "text": {**_STR, "description": "Comment text; a newline starts a new paragraph."},
        },
        "required": ["op", "at", "text"],
        "additionalProperties": False,
    },
    "comment.reply": {
        "type": "object",
        "properties": {
            "op": {"enum": ["comment.reply"]},
            "comment": {**_STR, "description": "Id of the comment to reply to (comments.list)."},
            "text": {**_STR, "description": "Reply text."},
        },
        "required": ["op", "comment", "text"],
        "additionalProperties": False,
    },
    "comment.resolve": {
        "type": "object",
        "properties": {
            "op": {"enum": ["comment.resolve"]},
            "comment": {**_STR, "description": "Id of any comment of the thread."},
            "resolved": {"type": "boolean", "description": "false reopens the thread."},
        },
        "required": ["op", "comment"],
        "additionalProperties": False,
    },
}


def is_comment_edit(edit: object) -> bool:
    return isinstance(edit, AddComment | ReplyComment | ResolveComment)


def comment_from_dict(raw: Mapping[str, Any]) -> CommentEdit:
    op = raw.get("op")
    if not isinstance(op, str) or op not in COMMENT_SCHEMAS:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown comment operation: {op!r}")
    validate(dict(raw), COMMENT_SCHEMAS[op])
    if op == "comment.add":
        return AddComment(raw["at"], raw["text"])
    if op == "comment.reply":
        return ReplyComment(raw["comment"], raw["text"])
    return ResolveComment(raw["comment"], raw.get("resolved", True))


def _short(text: str) -> str:
    return text if len(text) <= _PREVIEW else text[: _PREVIEW - 1] + "…"


def _check_comment_text(text: str) -> None:
    check_text(text, "text")
    if not text.strip():
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "comment text must not be empty")


def _marker(tag: str, cid: str) -> ET.Element:
    return ET.Element(W + tag, {W + "id": cid})


def _reference(cid: str, styled: bool) -> ET.Element:
    run = ET.Element(W + "r")
    if styled:
        rpr = ET.SubElement(run, W + "rPr")
        ET.SubElement(rpr, W + "rStyle", {W + "val": "CommentReference"})
    ET.SubElement(run, W + "commentReference", {W + "id": cid})
    return run


_MARKER = re.compile(
    rb"<(?:[\w.-]+:)?(commentRangeStart|commentReference)\b((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>"
)
_ID_ATTR = re.compile(rb"""[\s:]id\s*=\s*["']([^"']*)["']""")


def _locate_markers(index: DocumentIndex, cid: str) -> dict[str, Paragraph]:
    """Paragraph holding each of ``commentRangeStart`` / ``commentReference`` of ``cid``."""
    data = index.spans.data
    spans = index.spans.paragraphs
    starts = [spans[p.span_index].start for p in index.paragraphs]
    found: dict[str, Paragraph] = {}
    for m in _MARKER.finditer(data):
        ident = _ID_ATTR.search(m.group(2))
        if ident is None or ident.group(1).decode() != cid:
            continue
        i = bisect.bisect_right(starts, m.start()) - 1
        if i < 0 or m.start() >= spans[index.paragraphs[i].span_index].end:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"comment {cid} is anchored outside a paragraph of the main story",
                comment=cid,
            )
        found.setdefault(m.group(1).decode(), index.paragraphs[i])
    return found


def _insert_after_marker(
    elem: ET.Element, tag: str, cid: str, nodes: Sequence[ET.Element], para: Paragraph
) -> None:
    parents = {child: parent for parent in elem.iter() for child in parent}
    for node in elem.iter(W + tag):
        if node.get(W + "id") == cid:
            parent = parents.get(node)
            if parent is None:
                break
            at = list(parent).index(node) + 1
            for j, item in enumerate(nodes):
                parent.insert(at + j, item)
            return
    raise DocControlError(
        ErrorCode.UNSUPPORTED_CONTENT,
        f"comment {cid} is not anchored in {para.address}",
        comment=cid,
    )


@dataclass
class _Plan:
    """Marker placements per paragraph number."""

    at_offset: defaultdict[int, list[tuple[int, list[ET.Element]]]]
    after_marker: defaultdict[int, list[tuple[str, str, list[ET.Element]]]]

    def annotators(self) -> dict[int, Annotator]:
        return {n: self._make(n) for n in sorted(set(self.at_offset) | set(self.after_marker))}

    def _make(self, number: int) -> Annotator:
        offsets = self.at_offset.get(number, [])
        relative = self.after_marker.get(number, [])

        def annotate(elem: ET.Element, para: Paragraph, ctx: RevisionContext) -> None:
            editor = ParagraphEditor(elem, para, ctx)
            for offset, nodes in sorted(offsets, key=lambda item: -item[0]):
                editor.place(offset, nodes)
            for tag, cid, nodes in relative:
                _insert_after_marker(elem, tag, cid, nodes, para)

        return annotate


def _require_comment(parts: CommentParts, cid: str) -> None:
    if cid not in parts.existing:
        raise DocControlError(
            ErrorCode.COMMENT_NOT_FOUND, f"no comment with id {cid!r}", comment=cid
        )


def apply_batch(
    doc_path: Path,
    edits: Sequence[Any],
    *,
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> TrackedResult:
    """Apply tracked text edits and comment operations together in one verified write."""
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
    now = now or datetime.now(UTC)
    index = DocumentIndex.from_package(pkg)
    full_author, date = format_author(author, tag), format_date(now)

    styles = frozenset(s.id for s in load_styles(pkg).styles)
    parts = CommentParts(pkg, styles=styles)
    taken = used_ids(pkg) | {int(c) for c in parts.comment_ids() if c.strip().isdigit()}
    next_id = max(taken, default=-1) + 1
    reserved: list[int] = []
    plan = _Plan(defaultdict(list), defaultdict(list))
    previews: list[dict[str, Any]] = []
    touched: list[str] = []
    ref_styled = "CommentReference" in styles

    for i, edit in enumerate(edits):
        if isinstance(edit, AddComment):
            _check_comment_text(edit.text)
            rng = index.resolve_range(edit.at)
            assert rng.start_offset is not None and rng.end_offset is not None
            first, last, s, e = rng.first, rng.last, rng.start_offset, rng.end_offset
            if not boundary_ok(first.text, s) or not boundary_ok(last.text, e):
                raise DocControlError(
                    ErrorCode.ADDRESS_INVALID,
                    f"{edit.at!r} splits a character from its combining marks",
                    address=edit.at,
                )
            if first is last and s == e and first.text:
                raise DocControlError(
                    ErrorCode.ADDRESS_INVALID,
                    f"{edit.at!r} is an empty range: a comment needs text to anchor on",
                    address=edit.at,
                )
            cid = str(next_id)
            reserved.append(next_id)
            next_id += 1
            parts.add(cid, edit.text, full_author, date)
            start, end = _marker("commentRangeStart", cid), _marker("commentRangeEnd", cid)
            ref = _reference(cid, ref_styled)
            if first is last and s == e:  # an empty paragraph: a collapsed anchor
                plan.at_offset[first.number].append((0, [start, end, ref]))
            else:
                plan.at_offset[last.number].append((e, [end, ref]))
                plan.at_offset[first.number].append((s, [start]))
            quoted = first.text[s:e] if first is last else first.text[s:] + "…"
            previews.append(
                {
                    "op": i,
                    "at": edit.at,
                    "before": _short(quoted),
                    "after": f"(comment c{cid} by {full_author}: {_short(edit.text)})",
                }
            )
            touched.append(cid)
        elif isinstance(edit, ReplyComment):
            _check_comment_text(edit.text)
            _require_comment(parts, edit.comment)
            root = parts.root_of(edit.comment)
            places = _locate_markers(index, root)
            ref_para = places.get("commentReference")
            if ref_para is None:
                raise DocControlError(
                    ErrorCode.UNSUPPORTED_CONTENT,
                    f"comment {root} has no anchor in the main story to reply on",
                    comment=root,
                )
            cid = str(next_id)
            reserved.append(next_id)
            next_id += 1
            parts.add(cid, edit.text, full_author, date, parent=root)
            plan.after_marker[ref_para.number].append(
                (
                    "commentReference",
                    root,
                    [_marker("commentRangeEnd", cid), _reference(cid, ref_styled)],
                )
            )
            start_para = places.get("commentRangeStart")
            if start_para is None:
                start_para = ref_para
                plan.after_marker[ref_para.number].append(
                    ("commentReference", root, [_marker("commentRangeStart", cid)])
                )
            else:
                plan.after_marker[start_para.number].append(
                    ("commentRangeStart", root, [_marker("commentRangeStart", cid)])
                )
            previews.append(
                {
                    "op": i,
                    "at": str(start_para.address),
                    "before": f"(thread of comment {root})",
                    "after": f"(reply c{cid} by {full_author}: {_short(edit.text)})",
                }
            )
            touched.append(cid)
        elif isinstance(edit, ResolveComment):
            _require_comment(parts, edit.comment)
            root = parts.root_of(edit.comment)
            members = parts.thread(root)
            changed = [m for m in members if parts.set_done(m, edit.resolved)]
            previews.append(
                {
                    "op": i,
                    "at": f"comment {root}",
                    "before": "(open)" if edit.resolved else "(resolved)",
                    "after": ("(resolved)" if edit.resolved else "(open)")
                    + ("" if changed else ", already so"),
                }
            )
            touched += members

    prepared = prepare_edits(
        pkg,
        edits,
        author=author,
        tag=tag,
        now=now,
        dry_run=dry_run,
        index=index,
        annotations=plan.annotators(),
        reserved_ids=reserved,
    )
    part_bytes = parts.build()
    replacements = dict(part_bytes)
    if prepared.new_data != pkg.read(DOCUMENT_PART):
        replacements[DOCUMENT_PART] = prepared.new_data
    footprint = replace(prepared.footprint, parts=frozenset(part_bytes))
    ids = sorted(set(touched), key=int)
    result = replace(
        prepared.result,
        applied=len(edits),
        preview=sorted(prepared.result.preview + previews, key=lambda p: p["op"]),
        parts={name: [f"c{c}" for c in ids] for name in part_bytes},
        comment_ids=ids,
    )
    if dry_run:
        return result
    if not replacements:
        return replace(result, after_sha256=pkg.sha256, backup=None)
    written = write_verified(doc_path, replacements, footprint, pkg.sha256, now=now, env=env)
    return replace(
        result,
        before_sha256=written.before_sha256,
        after_sha256=written.after_sha256,
        backup=written.backup,
    )
