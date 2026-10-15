"""``caption.add``, ``xref.add``, ``index.mark`` and ``index.insert`` (LAYOUT T8).

All four write fields as tracked insertions and share :mod:`doc_control.ops.fieldwrite`'s
field helpers; the results of the fields they write are filled by ``field.update`` (the
field is marked dirty until then, and its cached result is a best guess: the target's own
text, or ``?``). The index is the exception: ``field.update`` never recomputes an INDEX,
so it keeps its placeholder until Word rebuilds it (F9, or on open, as the field is dirty).

* ``caption.add`` puts a new paragraph above or below a body paragraph: the label, a
  ``SEQ`` field and the text (``Table 1: Results``), in the ``Caption`` style when the
  document has it. A hidden bookmark ``_Ref<digits>`` surrounds the label and number, as
  Word does, so a cross-reference can point at it.
* ``xref.add`` puts a ``REF`` or ``PAGEREF`` field at an insertion point. The target is a
  caption (``number`` is its label and number, ``text`` the whole caption) or any other
  body paragraph (``number`` is its paragraph number, ``text`` its text). The target gets
  a ``_Ref`` bookmark unless it already has one that covers what the field shows; adding
  a bookmark is the only change to it and is not a tracked change, as in Word.
* ``index.mark`` puts an ``XE`` field (it has no result, so nothing shows) after a range;
  the entry is ``Term`` or ``Term:Sub``, up to three levels.
* ``index.insert`` puts an ``INDEX`` field, laid out like ``toc.insert``'s table of contents.

Each is one verified write whose log entry carries ``undo_by_backup``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import parse_range
from doc_control.model.document import Document
from doc_control.model.index import DocumentIndex, Paragraph, build_index
from doc_control.model.parts import W
from doc_control.model.styles import load_styles
from doc_control.ooxml.revisions import (
    BOOKMARK_END,
    BOOKMARK_START,
    DEFAULT_AUTHOR,
    ID,
    PPR,
    ParagraphEditor,
    RevisionContext,
    format_author,
    format_date,
    new_paragraph,
    text_children,
    used_ids,
)
from doc_control.ooxml.splice import DocumentSpans
from doc_control.ooxml.zipio import Package
from doc_control.ops.fieldwrite import (
    _EXPECT,
    _STR,
    _Body,
    _code_run,
    _commit,
    _el,
    _field_char,
    _flat_text,
    _ins,
    _Loc,
    _refuse_file,
    insert_field_block,
)
from doc_control.ops.tracked import check_name, check_text, log_changes, story_of
from doc_control.registry import operation
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, Footprint

LABELS = ("Table", "Figure", "Equation")
SHOW = ("number", "page", "text")
INDEX_PLACEHOLDER = "Update the field to build the index."
UNKNOWN = "?"
_MAX_LEVELS = 3
_REF_NAME = re.compile(r"^_Ref\d+$")
#: Children of a paragraph that carry no content of their own.
_MARKERS = frozenset({PPR, BOOKMARK_START, BOOKMARK_END, W + "proofErr"})


@dataclass
class _Open:
    """A document opened for one field write: everything the four operations start from."""

    path: Path
    pkg: Package
    index: DocumentIndex
    body: _Body
    ctx: RevisionContext
    author: str
    now: datetime

    @property
    def spans(self) -> DocumentSpans:
        return self.index.spans


def _open(
    doc_path: Path,
    addresses: Sequence[str],
    *,
    what: str,
    expect_sha256: str | None,
    author: str,
    tag: str | None,
    now: datetime | None,
    dry_run: bool,
) -> _Open:
    check_name(author, "author")
    if tag is not None:
        check_name(tag, "tag")
    for address in addresses:
        if story_of(address):
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"{what} works at body addresses: {address} is in a note, header or comment",
                address=address,
            )
    _refuse_file(doc_path)
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    now = now or datetime.now(UTC)
    index = DocumentIndex.from_package(pkg)
    full_author = format_author(author, tag)
    ctx = RevisionContext(used_ids(pkg), full_author, format_date(now))
    return _Open(doc_path, pkg, index, _Body(index.spans), ctx, full_author, now)


def _body_paragraph(o: _Open, address: str, what: str) -> Paragraph:
    para = o.index.resolve_address(address)
    if o.spans.paragraphs[para.span_index].nested:
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            f"{what} cannot use a paragraph inside a text box",
            address=address,
        )
    return para


def _one_line(text: str, what: str) -> None:
    check_text(text, what)
    if "\n" in text or "\t" in text:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"{what} must be one line without tabs")


def _bookmark_names(o: _Open) -> set[str]:
    root = ET.fromstring(o.pkg.read(DOCUMENT_PART))
    return {b.get(W + "name", "") for b in root.iter(BOOKMARK_START)}


def _fresh_name(taken: set[str]) -> str:
    n = 100_000_001
    while f"_Ref{n}" in taken:
        n += 1
    return f"_Ref{n}"


def _bookmark(ctx: RevisionContext, name: str) -> tuple[ET.Element, ET.Element]:
    bid = ctx.continuation_id()
    return _el("bookmarkStart", {ID: bid, W + "name": name}), _el("bookmarkEnd", {ID: bid})


def _inline_field(ctx: RevisionContext, code: str, result: str | None) -> list[ET.Element]:
    """A complex field as tracked insertions: its opening, the result (none for a field
    without one) and its closing. It is marked dirty, so Word or ``field.update`` fills it."""
    opening = [_field_char("begin", dirty=True), _code_run(code)]
    closing = _field_char("end")
    if result is not None:
        opening.append(_field_char("separate"))
    if result is None:
        return [_ins(ctx, opening), _ins(ctx, [closing])]
    holder = _el("r", None, *text_children(result))
    return [_ins(ctx, opening), _ins(ctx, [holder]), _ins(ctx, [closing])]


def _write(
    o: _Open,
    op: str,
    replacements: Mapping[int, ET.Element],
    insert_after: Mapping[int, ET.Element],
    insert_before: Mapping[int, ET.Element],
    request: dict[str, Any],
    tag: str | None,
    env: Mapping[str, str] | None,
    dry_run: bool,
    result: dict[str, Any],
) -> bytes:
    """Splice the edited and new paragraphs in, write them (unless ``dry_run``) and fill
    ``result``. Returns the new bytes of ``word/document.xml``."""
    spans = o.spans
    done = {i: spans.serialize(elem, i) for i, elem in replacements.items()}
    after = {i: [spans.serialize(elem, i)] for i, elem in insert_after.items()}
    before = {i: [spans.serialize(elem, i)] for i, elem in insert_before.items()}
    data = spans.splice(done, insert_after=after, insert_before=before)
    if dry_run:
        return data
    footprint = Footprint(
        replaced=frozenset(done),
        inserted_after=frozenset(after),
        inserted_before=frozenset(before),
    )
    changes = log_changes(spans, o.index, done, after, before)
    warnings: list[str] = []
    _commit(
        o.path,
        o.pkg,
        {DOCUMENT_PART: data},
        footprint,
        changes=changes,
        request={"op": op, **request},
        author=o.author,
        tag=tag,
        now=o.now,
        env=env,
        extra={},
        result=result,
        warnings=warnings,
    )
    if warnings:
        result["warnings"] = warnings
    return data


def _result(o: _Open, dry_run: bool, **fields: Any) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        **fields,
        "revision_ids": o.ctx.revision_ids,
        "author": o.author,
        "date": format_date(o.now),
    }


# --- caption.add --------------------------------------------------------------------------


def _next_number(o: _Open, label: str, before_span: int) -> int:
    """The number the caption gets: the ``SEQ <label>`` fields in front of it, plus one."""
    count = 0
    for loc in o.body.locate():
        words = loc.code.split()
        if (
            loc.type == "SEQ"
            and len(words) > 1
            and words[1] == label
            and loc.begin[0] < before_span
        ):
            count += 1
    return count + 1


def add_caption(
    doc_path: Path,
    at: str,
    *,
    label: str,
    text: str,
    position: str | None = None,
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Insert a tracked caption paragraph above or below the body paragraph ``at``."""
    if label not in LABELS:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"label must be one of {', '.join(LABELS)}")
    # A table's caption goes above it, a figure's or an equation's below (Word's habit).
    position = position or ("above" if label == "Table" else "below")
    if position not in ("above", "below"):
        raise DocControlError(ErrorCode.SCHEMA_INVALID, 'position must be "above" or "below"')
    _one_line(text, "text")
    o = _open(
        doc_path,
        [at],
        what="caption.add",
        expect_sha256=expect_sha256,
        author=author,
        tag=tag,
        now=now,
        dry_run=dry_run,
    )
    anchor = _body_paragraph(o, at, "a caption")
    ctx = o.ctx
    number = _next_number(o, label, anchor.span_index + (position == "below"))
    name = _fresh_name(_bookmark_names(o))
    start, end = _bookmark(ctx, name)

    para, _ = new_paragraph(None, ctx, inserted_mark=True)
    ppr = para.find(PPR)
    assert ppr is not None
    if "Caption" in {s.id for s in load_styles(o.pkg).styles}:
        ppr.insert(0, _el("pStyle", {W + "val": "Caption"}))
    lead = _el("r", None, *text_children(f"{label} "))
    para.append(start)
    para.append(_ins(ctx, [lead]))
    para.extend(_inline_field(ctx, f" SEQ {label} \\* ARABIC ", str(number)))
    para.append(end)
    if text:
        para.append(_ins(ctx, [_el("r", None, *text_children(f": {text}"))]))

    slot = {anchor.span_index: para}
    below = position == "below"
    result = _result(
        o,
        dry_run,
        label=label,
        position=position,
        number=number,
        bookmark=name,
        footprint={DOCUMENT_PART: [str(anchor.address)]},
    )
    data = _write(
        o,
        "caption.add",
        {},
        slot if below else {},
        {} if below else slot,
        {"at": at, "label": label, "text": text, "position": position},
        tag,
        env,
        dry_run,
        result,
    )
    result["at"] = str(build_index(data).paragraph(anchor.number + below).address)
    return result


# --- xref.add -----------------------------------------------------------------------------


def _content(elem: ET.Element, lo: int, hi: int) -> bool:
    return any(child.tag not in _MARKERS for child in list(elem)[lo:hi])


def _content_span(elem: ET.Element) -> tuple[int, int]:
    """Child indexes ``[first, stop)`` from the first to the last child with content."""
    kids = [i for i, child in enumerate(elem) if child.tag not in _MARKERS]
    return (kids[0], kids[-1] + 1) if kids else (0, 0)


def _covering(elem: ET.Element, lo: int, hi: int) -> str | None:
    """The name of a ``_Ref`` bookmark of ``elem`` that covers the children ``[lo, hi)`` and
    nothing else with content, if the paragraph has one."""
    kids = list(elem)
    for s, child in enumerate(kids):
        name = child.get(W + "name", "")
        if child.tag != BOOKMARK_START or not _REF_NAME.match(name):
            continue
        ends = [
            e for e, c in enumerate(kids) if c.tag == BOOKMARK_END and c.get(ID) == child.get(ID)
        ]
        if not ends:
            continue
        e = ends[0]
        if s <= lo and e >= hi and not _content(elem, s, lo) and not _content(elem, hi, e):
            return name
    return None


def _numbered(o: _Open, elem: ET.Element) -> bool:
    """Whether the paragraph has a number: ``w:numPr`` of its own or of its style chain."""
    num = elem.find(f"{PPR}/{W}numPr/{W}numId")
    if num is not None:
        return num.get(W + "val") not in (None, "0")
    style = elem.find(f"{PPR}/{W}pStyle")
    if style is None or "word/styles.xml" not in o.pkg:
        return False
    root = ET.fromstring(o.pkg.read("word/styles.xml"))
    by_id = {s.get(W + "styleId"): s for s in root.findall(W + "style")}
    current = style.get(W + "val")
    for _ in range(64):
        found = by_id.get(current)
        if found is None:
            return False
        num = found.find(f"{PPR}/{W}numPr/{W}numId")
        if num is not None:
            return num.get(W + "val") not in (None, "0")
        based = found.find(W + "basedOn")
        current = based.get(W + "val") if based is not None else None
    return False


def _seq_in(o: _Open, span: int) -> _Loc | None:
    """The first ``SEQ`` field that sits in one paragraph, if the paragraph has any."""
    for loc in o.body.locate():
        if loc.type == "SEQ" and loc.begin[0] == span and (loc.end or loc.begin)[0] == span:
            return loc
    return None


def add_xref(
    doc_path: Path,
    at: str,
    to: str,
    *,
    show: str = "number",
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Insert a tracked ``REF`` or ``PAGEREF`` field at ``at`` that points at ``to``."""
    if show not in SHOW:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"show must be one of {', '.join(SHOW)}")
    where = parse_range(at)
    whole = where.start_offset is None and where.sentence is None
    if where.start != where.end or not (
        whole or (where.sentence is None and where.start_offset == where.end_offset)
    ):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            "at must be an insertion point (p<n>#<fp>:<k>-<k>) or a paragraph address",
            address=at,
        )
    o = _open(
        doc_path,
        [at, to],
        what="xref.add",
        expect_sha256=expect_sha256,
        author=author,
        tag=tag,
        now=now,
        dry_run=dry_run,
    )
    place = _body_paragraph(o, str(where.start), "a cross-reference")
    offset = len(place.text) if whole else where.start_offset or 0
    target = _body_paragraph(o, to, "a cross-reference")
    if target.span_index == place.span_index:
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            "a cross-reference cannot go in the paragraph it points at",
            address=at,
        )

    telem = o.body.elem(target.span_index)
    seq = _seq_in(o, target.span_index)
    first, stop = _content_span(telem)
    if first == stop:
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT, "the target paragraph has no content", address=to
        )
    # What the bookmark has to cover: a caption's label and number, or the whole paragraph.
    if seq is not None and show != "text":
        if seq.end is None:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                "the caption's number is a simple field (w:fldSimple), which a "
                "cross-reference cannot be bookmarked around; use show=text",
                address=to,
            )
        lo, hi = first, seq.end[1] + 1
    else:
        lo, hi = first, stop
    if seq is None and show == "number" and not _numbered(o, telem):
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            "the target is neither a caption nor a numbered paragraph, so it has no number "
            "to show; use show=text or show=page",
            address=to,
        )
    name = _covering(telem, lo, hi)
    created = name is None
    if name is None:
        name = _fresh_name(_bookmark_names(o))
        start, end = _bookmark(o.ctx, name)
        telem.insert(hi, end)
        telem.insert(lo, start)

    if show == "page":
        code, cached = f" PAGEREF {name} \\h ", UNKNOWN
    else:
        switch = " \\r" if seq is None and show == "number" else ""
        code = f" REF {name}{switch} \\h "
        cached = _flat_text(list(telem)[lo:hi]).strip() if switch == "" else UNKNOWN
    nodes = _inline_field(o.ctx, code, cached or UNKNOWN)
    pelem = o.body.elem(place.span_index)
    ParagraphEditor(pelem, place, o.ctx).place(offset, nodes)

    touched = {place.span_index: pelem}
    footprint = [str(place.address)]
    if created:
        touched[target.span_index] = telem
        footprint.append(str(target.address))
    result = _result(
        o,
        dry_run,
        at=str(place.address),
        to=str(target.address),
        show=show,
        code=code.strip(),
        bookmark=name,
        footprint={DOCUMENT_PART: footprint},
    )
    _write(
        o,
        "xref.add",
        touched,
        {},
        {},
        {"at": at, "to": to, "show": show},
        tag,
        env,
        dry_run,
        result,
    )
    return result


# --- index.mark and index.insert ----------------------------------------------------------


def _entry_parts(entry: str) -> list[str]:
    _one_line(entry, "entry")
    parts = [part.strip() for part in entry.split(":")]
    if len(parts) > _MAX_LEVELS or not all(parts):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f'entry must be like "Term" or "Term:Sub" (at most {_MAX_LEVELS} levels, none empty)',
        )
    return parts


def mark_index(
    doc_path: Path,
    at: str,
    entry: str,
    *,
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Insert a tracked ``XE`` field right after the range ``at``."""
    parts = _entry_parts(entry)
    o = _open(
        doc_path,
        [at],
        what="index.mark",
        expect_sha256=expect_sha256,
        author=author,
        tag=tag,
        now=now,
        dry_run=dry_run,
    )
    resolved = o.index.resolve_range(at)
    if resolved.first is not resolved.last:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            "at must lie inside one paragraph: the index entry sits at the end of the range",
            address=at,
        )
    para = _body_paragraph(o, str(resolved.first.address), "an index entry")
    offset = len(para.text) if resolved.end_offset is None else resolved.end_offset
    escaped = ":".join(part.replace("\\", "\\\\").replace('"', '\\"') for part in parts)
    code = f' XE "{escaped}" '
    elem = o.body.elem(para.span_index)
    ParagraphEditor(elem, para, o.ctx).place(offset, _inline_field(o.ctx, code, None))
    result = _result(
        o,
        dry_run,
        at=str(para.address),
        entry=":".join(parts),
        code=code.strip(),
        footprint={DOCUMENT_PART: [str(para.address)]},
    )
    _write(
        o,
        "index.mark",
        {para.span_index: elem},
        {},
        {},
        {"at": at, "entry": entry},
        tag,
        env,
        dry_run,
        result,
    )
    return result


def insert_index(
    doc_path: Path,
    after: str,
    *,
    title: str = "Index",
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Insert a tracked index field after the body paragraph ``after``."""
    return insert_field_block(
        doc_path,
        after,
        op="index.insert",
        code=" INDEX ",
        title=title,
        title_style="IndexHeading",
        entry_style="Index1",
        placeholder=INDEX_PLACEHOLDER,
        request={"title": title},
        expect_sha256=expect_sha256,
        author=author,
        tag=tag,
        now=now,
        dry_run=dry_run,
        env=env,
    )


# --- registered operations ----------------------------------------------------------------

_COMMON: dict[str, Any] = {
    "doc": {**_STR, "description": "Path to the .docx."},
    "expect": _EXPECT,
    "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
    "author": {**_STR, "description": "Revision author."},
    "tag": {**_STR, "description": "Appended to the author."},
}


def _schema(required: list[str], **properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**_COMMON, **properties},
        "required": ["doc", *required],
        "additionalProperties": False,
    }


def _common(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "expect_sha256": request.get("expect", {}).get("sha256"),
        "author": request.get("author", DEFAULT_AUTHOR),
        "tag": request.get("tag"),
        "dry_run": request.get("dry_run", False),
    }


def _reply(path: Path, result: dict[str, Any]) -> Reply:
    warnings = result.pop("warnings", [])
    return Reply(result, doc=Document(str(path)).envelope_doc(), warnings=warnings)


@operation(
    "caption.add",
    "write",
    "Add a caption paragraph above or below a body paragraph, as tracked insertions: the "
    "label (Table, Figure or Equation), a SEQ field that numbers it, and the text, with a "
    "hidden bookmark around the label and number for cross-references. The number is "
    "refreshed by field.update. Returns the address of the caption.",
    _schema(
        ["at", "label", "text"],
        at={**_STR, "description": "Address of the body paragraph the caption goes next to."},
        label={"enum": list(LABELS), "description": "The caption label."},
        text={"type": "string", "description": "Caption text after the number; may be empty."},
        position={
            "enum": ["above", "below"],
            "description": "Default: above for a Table, below for a Figure or Equation.",
        },
    ),
)
def caption_add(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    result = add_caption(
        path,
        request["at"],
        label=request["label"],
        text=request["text"],
        position=request.get("position"),
        **_common(request),
    )
    return _reply(path, result)


@operation(
    "xref.add",
    "write",
    "Insert a cross-reference field (REF, or PAGEREF for the page) at an insertion point, "
    "as a tracked insertion, pointing at a caption or another body paragraph. The target "
    "gets a hidden bookmark when it has none that fits. field.update fills in the shown "
    "number, text or page.",
    _schema(
        ["at", "to"],
        at={
            **_STR,
            "description": "Insertion point: p<n>#<fp>:<k>-<k>, or a paragraph address for "
            "its end.",
        },
        to={**_STR, "description": "Address of the caption or heading it points at."},
        show={
            "enum": list(SHOW),
            "description": "number (default): a caption's label and number, or a paragraph's "
            "number; page: the page it is on; text: the caption or paragraph text.",
        },
    ),
)
def xref_add(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    result = add_xref(
        path, request["at"], request["to"], show=request.get("show", "number"), **_common(request)
    )
    return _reply(path, result)


@operation(
    "index.mark",
    "write",
    "Mark an index entry: an XE field, as a tracked insertion, right after a range. "
    'The entry is "Term" or "Term:Sub" (up to three levels). index.insert puts the index, '
    "which Word builds from the marks.",
    _schema(
        ["at", "entry"],
        at={**_STR, "description": "A range inside one paragraph; the mark goes after it."},
        entry={**_STR, "description": 'The index entry, e.g. "Term" or "Term:Sub".'},
    ),
)
def index_mark(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    result = mark_index(path, request["at"], request["entry"], **_common(request))
    return _reply(path, result)


@operation(
    "index.insert",
    "write",
    "Insert an index (an INDEX field) after a body paragraph, as tracked insertions: an "
    "optional title, the field with a placeholder result and the paragraph that closes it. "
    "The field is marked dirty: Word builds the entries and page numbers "
    "from the index.mark marks (F9); field.update leaves it alone. "
    "Returns the address of the field's paragraph.",
    _schema(
        ["after"],
        after={**_STR, "description": "Address of the body paragraph it goes after."},
        title={
            "type": "string",
            "description": 'Title paragraph above it (default "Index"); "" for none.',
        },
    ),
)
def index_insert(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    result = insert_index(
        path, request["after"], title=request.get("title", "Index"), **_common(request)
    )
    return _reply(path, result)
