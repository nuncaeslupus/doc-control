"""Read operations: `info`, `outline`, `stats`, `para.get`, `styles.list`, `style.get`,
`comments.list`, `revisions.list`.

None of them writes: the document is opened, read and closed. Lists are capped at
about 8,000 bytes and continue with `next` (see :mod:`doc_control.ops.paging`).

Extension points: every fact function in :data:`PARA_FACTS` is merged into the
`para.get` result and every one in :data:`OUTLINE_FACTS` into each `outline`
entry, so layout facts can be added without touching the handlers.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply, success
from doc_control.errors import DocControlError, ErrorCode
from doc_control.inprocess import budget
from doc_control.model.address import parse_range
from doc_control.model.annotations import (
    REVISION_TYPES,
    read_comments,
    read_revisions,
)
from doc_control.model.document import Document
from doc_control.model.format import para_format, run_format
from doc_control.model.index import Paragraph
from doc_control.model.parts import OFFICE_REL, W, parse_part, related_part
from doc_control.model.stories import list_stories
from doc_control.model.storyorder import STORIES_PROPERTY, fields_for, kinds_of, rows
from doc_control.model.styles import Style
from doc_control.ops.paging import (
    PAGING_PROPERTIES,
    RESPONSE_BUDGET,
    make_cursor,
    paged,
    read_cursor,
    size,
)
from doc_control.registry import operation
from doc_control.safety.lock import find_lock_file

_DOC: dict[str, Any] = {"type": "string", "minLength": 1, "description": "Path to the .docx."}
_OUTLINE_TEXT = 160
_COMMENT_TEXT = 1000
_REVISION_TEXT = 200
_FONT_LIMIT = 20
_STYLE_TYPES = ["paragraph", "character", "table", "numbering"]


@dataclass(frozen=True)
class ParagraphContext:
    """What a fact function gets to look at."""

    doc: Document
    paragraph: Paragraph


Fact = Callable[[ParagraphContext], dict[str, Any]]
PARA_FACTS: list[Fact] = []
OUTLINE_FACTS: list[Fact] = []


def _list_schema(**extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"doc": _DOC, **extra, **PAGING_PROPERTIES},
        "required": ["doc"],
        "additionalProperties": False,
    }


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


# -- info ------------------------------------------------------------------

_CORE_FIELDS = {
    "title": "title",
    "subject": "subject",
    "creator": "creator",
    "lastModifiedBy": "last_modified_by",
    "created": "created",
    "modified": "modified",
    "revision": "revision",
}
_APP_FIELDS = {"Application": "application", "Pages": "pages", "Words": "words"}


def _doc_props(doc: Document, part: str, fields: dict[str, str]) -> dict[str, Any]:
    if part not in doc.pkg:
        return {}
    out: dict[str, Any] = {}
    for child in parse_part(doc.pkg, part):
        name = child.tag.rsplit("}", 1)[-1] if isinstance(child.tag, str) else ""
        text = (child.text or "").strip()
        if name in fields and text:
            out[fields[name]] = int(text) if text.isdigit() else text
    return out


@operation(
    "info",
    "read",
    "File facts: size, hash, paragraph count, properties and which parts exist.",
    {
        "type": "object",
        "properties": {"doc": _DOC},
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def info(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    names = doc.pkg.names
    settings = parse_part(doc.pkg, "word/settings.xml") if "word/settings.xml" in doc.pkg else None
    tracking = settings is not None and settings.find(W + "trackRevisions") is not None
    result: dict[str, Any] = {
        "size_bytes": Path(doc.path).stat().st_size,
        "parts": len(names),
        "properties": _doc_props(doc, "docProps/core.xml", _CORE_FIELDS),
        "application": _doc_props(doc, "docProps/app.xml", _APP_FIELDS),
        "tracked_changes_on": tracking,
        "word_lock": find_lock_file(Path(doc.path)) is not None,
        "has": {
            "styles": any(n == "word/styles.xml" for n in names),
            "comments": any(n == "word/comments.xml" for n in names),
            "footnotes": any(n == "word/footnotes.xml" for n in names),
            "endnotes": any(n == "word/endnotes.xml" for n in names),
            "headers": sum(1 for n in names if re.fullmatch(r"word/header\d*\.xml", n)),
            "footers": sum(1 for n in names if re.fullmatch(r"word/footer\d*\.xml", n)),
        },
    }
    return Reply(result, doc=doc.envelope_doc())


# -- outline ---------------------------------------------------------------


@operation(
    "outline",
    "read",
    "The headings of the document with level, style and address, in order.",
    _list_schema(
        max_level={
            "type": "integer",
            "minimum": 1,
            "maximum": 9,
            "description": "Only headings up to this level.",
        },
        stories=STORIES_PROPERTY,
    ),
)
def outline(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    max_level = request.get("max_level", 9)
    entries: list[dict[str, Any]] = []
    for story_row in rows(doc, kinds_of(request)):
        view, para = story_row.view, story_row.para
        level = view.heading_level(para)
        if level is None or level > max_level or not para.text.strip():
            continue
        text, clipped = _clip(" ".join(para.text.split()), _OUTLINE_TEXT)
        entry: dict[str, Any] = {
            "at": str(para.address),
            "level": level,
            "style": view.style_id(para),
            "text": text,
        }
        if clipped:
            entry["truncated"] = True
        for fact in OUTLINE_FACTS:
            entry.update(fact(ParagraphContext(view, para)))
        entry.update(story_row.fields)
        entries.append(entry)
    return paged("outline", doc, request, "headings", entries)


# -- stats -----------------------------------------------------------------

_FONT_REF = re.compile(rb'<(?:[\w.-]+:)?rFonts\s[^>]*?\b(?:ascii|hAnsi)="([^"]*)"')
_RSTYLE_TAG = re.compile(rb"<(?:[\w.-]+:)?rStyle[\s/>]")
_TBLSTYLE_REF = re.compile(rb'<(?:[\w.-]+:)?tblStyle\s[^>]*?\bval="([^"]*)"')
_TBL_CHANGE = re.compile(
    rb"<(?:[\w.-]+:)?tblPrChange[\s>].*?</(?:[\w.-]+:)?tblPrChange>", re.DOTALL
)
# Where a character style is not in use: deleted text and the "before" properties of a change.
_NOT_IN_USE = frozenset(W + name for name in ("del", "moveFrom", "rPrChange", "pPrChange"))


def _run_styles(elem: Any, out: Counter[str]) -> None:
    for child in elem:
        tag = child.tag
        if not isinstance(tag, str) or tag in _NOT_IN_USE:
            continue
        if tag == W + "rStyle":
            if (value := child.get(W + "val")) is not None:
                out[value] += 1
        else:
            _run_styles(child, out)


def _style_usage(doc: Document) -> Counter[tuple[str, str]]:
    """Uses of each style as ``(type, id)``.

    Paragraphs are counted by style; character and table styles by reference, ignoring
    references inside deleted runs and the old properties kept by a tracked format change.
    """
    usage: Counter[tuple[str, str]] = Counter()
    for para in doc.index.paragraphs:
        style = doc.style_id(para)
        if style is not None:
            usage["paragraph", style] += 1
        if _RSTYLE_TAG.search(doc.paragraph_bytes(para)):
            runs: Counter[str] = Counter()
            _run_styles(doc.index.spans.parse_paragraph(para.span_index), runs)
            for key, n in runs.items():
                usage["character", key] += n
    data = doc.index.spans.data
    tables = Counter(m.decode("utf-8", "replace") for m in _TBLSTYLE_REF.findall(data))
    for change in _TBL_CHANGE.finditer(data):
        tables.subtract(
            m.decode("utf-8", "replace") for m in _TBLSTYLE_REF.findall(change.group(0))
        )
    for key, n in tables.items():
        if n > 0:
            usage["table", key] += n
    return usage


def _fonts(doc: Document) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    parts = [doc.index.spans.data]
    styles = related_part(doc.pkg, OFFICE_REL + "styles", "word/styles.xml")
    if styles is not None:
        parts.append(doc.pkg.read(styles))
    for data in parts:
        counts.update(raw.decode("utf-8", "replace") for raw in _FONT_REF.findall(data))
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"font": name, "uses": n} for name, n in ranked[:_FONT_LIMIT]]


def _footnote_count(doc: Document) -> int:
    name = related_part(doc.pkg, OFFICE_REL + "footnotes", "word/footnotes.xml")
    if name is None:
        return 0
    notes = parse_part(doc.pkg, name).findall(W + "footnote")
    return sum(1 for n in notes if n.get(W + "type", "normal") == "normal")


@operation(
    "stats",
    "read",
    "Counts: paragraphs, words, headings, tables, comments, revisions, fonts and styles in use.",
    {
        "type": "object",
        "properties": {"doc": _DOC, "stories": STORIES_PROPERTY},
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def stats(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    paragraphs = doc.index.paragraphs
    revisions = Counter(r.type for r in read_revisions(doc))
    usage = _style_usage(doc)
    result: dict[str, Any] = {
        "paragraphs": len(paragraphs),
        "empty_paragraphs": sum(1 for p in paragraphs if not p.text.strip()),
        "words": sum(len(p.text.split()) for p in paragraphs),
        "characters": sum(len(p.text) for p in paragraphs),
        "headings": sum(1 for p in paragraphs if doc.heading_level(p) is not None),
        "tables": doc.count_elements("tbl"),
        "comments": len(read_comments(doc)),
        "revisions": {"total": sum(revisions.values()), "by_type": dict(sorted(revisions.items()))},
        "footnotes": _footnote_count(doc),
        "styles_in_use": len(usage),
        "fonts": _fonts(doc),
    }
    others = [k for k in kinds_of(request) if k != "body"]
    if others:
        # The body's counts above stay the body's; each other story kind is counted apart.
        result["stories"] = {}
        for kind in others:
            texts = [r.para.text for r in rows(doc, (kind,))]
            result["stories"][kind] = {
                "stories": len({key for r in rows(doc, (kind,)) if (key := r.para.story)}),
                "paragraphs": len(texts),
                "words": sum(len(t.split()) for t in texts),
                "characters": sum(len(t) for t in texts),
            }
    return Reply(result, doc=doc.envelope_doc())


# -- para.get --------------------------------------------------------------


def _run_spans(doc: Document, para: Paragraph) -> list[dict[str, Any]]:
    """Spans of the visible text that carry direct character formatting."""
    elem = doc.index.spans.parse_paragraph(para.span_index)
    spans: list[dict[str, Any]] = []
    for piece in para.pieces:
        node = elem
        for i in piece.path[:-1]:
            node = node[i]
        fmt = run_format(node.find(W + "rPr")) if node.tag == W + "r" else {}
        if not fmt:
            continue
        last = spans[-1] if spans else None
        if last is not None and last["end"] == piece.start and _same(last, fmt):
            last["end"] = piece.end
        else:
            spans.append({"start": piece.start, "end": piece.end, **fmt})
    return spans


def _same(span: dict[str, Any], fmt: dict[str, Any]) -> bool:
    return {k: v for k, v in span.items() if k not in ("start", "end")} == fmt


def _clip_spans(spans: list[dict[str, Any]], start: int, end: int) -> list[dict[str, Any]]:
    return [
        {**s, "start": max(s["start"], start), "end": min(s["end"], end)}
        for s in spans
        if s["start"] < end and s["end"] > start
    ]


@operation(
    "para.get",
    "read",
    "One paragraph by address: text, style, direct formatting and formatted spans, and layout "
    "facts (heading_level is null for a body paragraph, 1-9 for a heading).",
    {
        "type": "object",
        "properties": {
            "doc": _DOC,
            "at": {
                "type": "string",
                "minLength": 1,
                "description": "Paragraph address from a read result, e.g. p142#a7b2a7b2.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1_000_000,
                "description": "Most characters of text to return.",
            },
            "cursor": PAGING_PROPERTIES["cursor"],
        },
        "required": ["doc", "at"],
        "additionalProperties": False,
    },
)
def para_get(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    view = doc.story_view(parse_range(request["at"]).start.story)
    para = view.index.resolve_address(request["at"])
    start = read_cursor("para.get", doc, request)
    total = len(para.text)
    if start > total:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, "cursor is beyond the end of the paragraph", path="$.cursor"
        )
    style = view.style_id(para)
    style_def = doc.styles.get(style) if style is not None else None
    elem = view.index.spans.parse_paragraph(para.span_index)
    head: dict[str, Any] = {
        "at": str(para.address),
        "number": para.number,
        "length": total,
        "style": style,
        "style_name": style_def.name if style_def is not None else None,
        "format": para_format(elem.find(W + "pPr")),
    }
    for fact in PARA_FACTS:
        head.update(fact(ParagraphContext(view, para)))
    head.update(fields_for(doc, para.story))
    spans = _run_spans(view, para)
    ceiling = min(total - start, request.get("limit", total))
    count = ceiling
    while True:
        end = start + count
        result = {**head, "offset": start, "text": para.text[start:end]}
        result["runs"] = _clip_spans(spans, start, end)
        cursor = make_cursor("para.get", doc, request, end) if end < total else None
        reply = Reply(result, doc=doc.envelope_doc(), next_cursor=cursor)
        envelope = success("para.get", result, doc=reply.doc, next_cursor=cursor)
        if count <= 1 or size(envelope) <= budget(RESPONSE_BUDGET):
            return reply
        count = max(1, int(count * 0.8))


# -- styles ----------------------------------------------------------------


def _style_row(style: Style, used: int) -> dict[str, Any]:
    row: dict[str, Any] = {"id": style.id, "name": style.name, "type": style.type}
    if style.based_on:
        row["based_on"] = style.based_on
    for key in ("default", "custom"):
        if getattr(style, key):
            row[key] = True
    row["used"] = used
    return row


@operation(
    "styles.list",
    "read",
    "Styles defined in the document with how many times each is used.",
    _list_schema(
        type={"type": "string", "enum": _STYLE_TYPES, "description": "Only this kind of style."},
        used_only={"type": "boolean", "description": "Only styles used at least once."},
    ),
)
def styles_list(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    usage = _style_usage(doc)
    rows = []
    for style in doc.styles.styles:
        if "type" in request and style.type != request["type"]:
            continue
        used = usage[style.type, style.id]
        if request.get("used_only") and not used:
            continue
        rows.append(_style_row(style, used))
    return paged("styles.list", doc, request, "styles", rows)


@operation(
    "style.get",
    "read",
    "One style by id or name: its own formatting, inherited formatting and heading level.",
    {
        "type": "object",
        "properties": {
            "doc": _DOC,
            "style": {"type": "string", "minLength": 1, "description": "Style id or name."},
        },
        "required": ["doc", "style"],
        "additionalProperties": False,
    },
)
def style_get(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    style = doc.styles.find(request["style"])
    if style is None:
        raise DocControlError(
            ErrorCode.STYLE_NOT_FOUND, f"no style {request['style']!r}", style=request["style"]
        )
    ppr, rpr = doc.styles.effective(style.id)
    result = _style_row(style, _style_usage(doc)[style.type, style.id])
    for key in ("next", "link"):
        if getattr(style, key):
            result[key] = getattr(style, key)
    result["quick"] = style.quick
    result["heading_level"] = doc.styles.heading_level(style.id)
    result["chain"] = [s.id for s in doc.styles.chain(style.id)]
    result["own"] = {"paragraph": style.ppr, "run": style.rpr}
    result["effective"] = {"paragraph": ppr, "run": rpr}
    return Reply(result, doc=doc.envelope_doc())


# -- comments and revisions ------------------------------------------------


@operation(
    "comments.list",
    "read",
    "Comments in thread order (each root then its replies) with resolved state and location.",
    _list_schema(
        resolved={"type": "boolean", "description": "Only resolved (true) or open (false)."},
        author={"type": "string", "minLength": 1, "description": "Only this author."},
    ),
)
def comments_list(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    rows: list[dict[str, Any]] = []
    for c in read_comments(doc):
        if "resolved" in request and c.resolved != request["resolved"]:
            continue
        if "author" in request and c.author != request["author"]:
            continue
        text, clipped = _clip(c.text, _COMMENT_TEXT)
        row: dict[str, Any] = {
            "id": c.id,
            "author": c.author,
            "date": c.date,
            "text": text,
            "resolved": c.resolved,
        }
        if clipped:
            row["truncated"] = True
        if c.reply_to is not None:
            row["reply_to"] = c.reply_to
        if c.at is not None:
            row["at"] = c.at
        if c.story is not None:
            row["story"] = c.story
        rows.append(row)
    return paged("comments.list", doc, request, "comments", rows)


@operation(
    "revisions.list",
    "read",
    "Tracked changes in document order with author, date, type, text and paragraph address.",
    _list_schema(
        author={"type": "string", "minLength": 1, "description": "Only this author."},
        type={"type": "string", "enum": list(REVISION_TYPES), "description": "Only this type."},
        stories={
            **STORIES_PROPERTY,
            "description": 'Stories to list (default ["body"]): "notes" adds footnotes and '
            'endnotes, "headers" headers and footers, after the body\'s revisions; their '
            "addresses carry a prefix (fn3/p1#…, hdr1.default/p1#…).",
        },
    ),
)
def revisions_list(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    kinds = kinds_of(request)
    found = [(r, "") for r in read_revisions(doc)] if "body" in kinds else []
    others = tuple(k for k in kinds if k != "body")
    for story in list_stories(doc.pkg, others) if others else []:
        found += [(r, story.kind) for r in read_revisions(doc.story_view(story.key))]
    rows: list[dict[str, Any]] = []
    for r, story_kind in found:
        if "author" in request and r.author != request["author"]:
            continue
        if "type" in request and r.type != request["type"]:
            continue
        text, clipped = _clip(r.text, _REVISION_TEXT)
        row: dict[str, Any] = {
            "id": r.id,
            "type": r.type,
            "author": r.author,
            "date": r.date,
            "at": r.at,
        }
        if text:
            row["text"] = text
        if clipped:
            row["truncated"] = True
        if story_kind:
            row["story"] = story_kind
        rows.append(row)
    return paged("revisions.list", doc, request, "revisions", rows)


# Layout facts register themselves on the hooks above; operations loaded one at a time
# (doc_control.registry.get_operation) must still see them.
from doc_control.ops import layout as _layout  # noqa: E402, F401
