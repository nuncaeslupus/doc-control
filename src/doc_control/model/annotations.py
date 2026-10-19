"""Comments and tracked revisions of a document, read-only.

Comments come from ``word/comments.xml`` (text, author, date) joined with
``word/commentsExtended.xml`` (reply parent, resolved state) and located by the
``w:commentRangeStart`` in the body, or else in a note, header or footer; a comment's
``quote`` is the text between its range start and end as the document reads now. Revisions
are found in the paragraphs of one story (the body unless the document is a story view).
Both report the address of the paragraph they sit in.
"""

from __future__ import annotations

import bisect
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass

from doc_control.errors import DocControlError
from doc_control.model.document import Document
from doc_control.model.index import Paragraph
from doc_control.model.parts import MS_REL_2011, OFFICE_REL, W14, W15, W, parse_part, related_part

COMMENTS_PART = "word/comments.xml"
COMMENTS_EXTENDED_PART = "word/commentsExtended.xml"
_RANGE_START = re.compile(rb"<(?:[\w.-]+:)?commentRangeStart\b[^>]*>")
_RANGE_END = re.compile(rb"<(?:[\w.-]+:)?commentRangeEnd\b[^>]*>")
_REFERENCE = re.compile(rb"<(?:[\w.-]+:)?commentReference\b[^>]*>")
_ID_ATTR = re.compile(rb"[\s:]id\s*=\s*[\"']([^\"']*)[\"']")
_TABLE_REVISION_TAG = re.compile(
    rb"<(?:[\w.-]+:)?(?:cellIns|cellDel|cellMerge)[\s/>]"
    rb"|<(?:[\w.-]+:)?trPr[\s>](?:(?!</(?:[\w.-]+:)?trPr>).)*?<(?:[\w.-]+:)?(?:ins|del)[\s/>]",
    re.DOTALL,
)
_REVISION_TAG = re.compile(
    rb"<(?:[\w.-]+:)?(?:ins|del|moveFrom|moveTo|rPrChange|pPrChange|sectPrChange)[\s/>]"
)
_SECT_CHANGE_TAG = re.compile(rb"<(?:[\w.-]+:)?sectPrChange[\s/>]")

# Revision element -> reported type; the same tags inside a paragraph's own
# ``w:pPr/w:rPr`` mark the paragraph mark instead.
_RUN_REVISIONS = {
    W + "ins": "insert",
    W + "del": "delete",
    W + "moveFrom": "move_from",
    W + "moveTo": "move_to",
    W + "rPrChange": "format",
}
_MARK_REVISIONS = {
    W + "ins": "mark_insert",
    W + "del": "mark_delete",
    W + "moveFrom": "mark_move_from",
    W + "moveTo": "mark_move_to",
}
_ROW_REVISIONS = {W + "ins": "row_insert", W + "del": "row_delete"}
_CELL_REVISIONS = {
    W + "cellIns": "cell_insert",
    W + "cellDel": "cell_delete",
    W + "cellMerge": "cell_merge",
}
REVISION_TYPES = tuple(
    sorted(
        {
            *_RUN_REVISIONS.values(),
            *_MARK_REVISIONS.values(),
            *_ROW_REVISIONS.values(),
            *_CELL_REVISIONS.values(),
            "paragraph_format",
            "mark_format",
            "section_format",
        }
    )
)


@dataclass(frozen=True)
class Comment:
    id: str
    author: str
    date: str
    initials: str
    text: str
    resolved: bool
    reply_to: str | None
    at: str | None  # address of the paragraph where the comment range starts
    story: str | None = None  # "footnote", "endnote", "header", "footer"; None: the body
    quote: str = ""  # the text its range covers; "" for a comment on a point


@dataclass(frozen=True)
class Revision:
    id: str
    type: str
    author: str
    date: str
    at: str
    text: str
    #: Id of the insertion or deletion right before this one (an insertion or deletion
    #: too), with nothing visible between them; None otherwise.
    follows: str | None = None


def _text(elem: ET.Element, tags: tuple[str, ...]) -> str:
    return "".join(t.text or "" for t in elem.iter() if t.tag in tags)


def _comment_locations(doc: Document) -> dict[str, str]:
    """Comment id -> address of the paragraph where its range starts.

    A range start at body level (between paragraphs) belongs to the next paragraph.
    Without a start, the comment reference's paragraph is used.
    """
    data = doc.index.spans.data
    spans = doc.index.spans.paragraphs
    starts = [spans[p.span_index].start for p in doc.index.paragraphs]
    out: dict[str, str] = {}
    for pattern in (_RANGE_START, _REFERENCE):
        for m in pattern.finditer(data):
            cid = _ID_ATTR.search(m.group(0))
            if cid is None:
                continue
            i = bisect.bisect_right(starts, m.start()) - 1
            if i >= 0 and m.start() < spans[doc.index.paragraphs[i].span_index].end:
                para = doc.index.paragraphs[i]
            elif doc.index.story:
                continue  # the part holds other notes too: only markers inside this story's
            elif i + 1 < len(starts):
                para = doc.index.paragraphs[i + 1]
            else:
                continue
            out.setdefault(cid.group(1).decode(), str(para.address))
    return out


def _comment_quotes(doc: Document) -> dict[str, str]:
    """Comment id -> the text between its ``commentRangeStart`` and ``commentRangeEnd``.

    The text is the visible text of the index (insertions shown, deletions not), one
    ``\\n`` between paragraphs. A marker between paragraphs counts from the start of the
    next one (a start) or to the end of the one before (an end). A comment without both
    markers, or with the end before the start, has no entry.
    """
    data = doc.index.spans.data
    if not _RANGE_START.search(data):
        return {}
    paras = doc.index.paragraphs
    spans = doc.index.spans.paragraphs
    starts = [spans[p.span_index].start for p in paras]
    places: tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]] = ({}, {})
    for is_end, (pattern, tag) in enumerate(
        ((_RANGE_START, W + "commentRangeStart"), (_RANGE_END, W + "commentRangeEnd"))
    ):
        for m in pattern.finditer(data):
            found = _ID_ATTR.search(m.group(0))
            if found is None:
                continue
            cid = found.group(1).decode()
            i = bisect.bisect_right(starts, m.start()) - 1
            if i >= 0 and m.start() < spans[paras[i].span_index].end:
                offset = _marker_offset(doc, paras[i], tag, cid)
                if offset is not None:
                    places[is_end].setdefault(cid, (i, offset))
            elif doc.index.story:
                continue  # the part holds other notes too: only markers inside this story's
            elif is_end and i >= 0:
                places[1].setdefault(cid, (i, len(paras[i].text)))
            elif not is_end and i + 1 < len(paras):
                places[0].setdefault(cid, (i + 1, 0))
    out: dict[str, str] = {}
    for cid, (first, start) in places[0].items():
        if cid not in places[1] or places[1][cid] < (first, start):
            continue
        last, end = places[1][cid]
        if first == last:
            out[cid] = paras[first].text[start:end]
        else:
            middle = [p.text for p in paras[first + 1 : last]]
            out[cid] = "\n".join([paras[first].text[start:], *middle, paras[last].text[:end]])
    return out


def _marker_offset(doc: Document, para: Paragraph, tag: str, cid: str) -> int | None:
    """Offset into the paragraph's visible text of the first ``tag`` marker of comment
    ``cid``; None if the paragraph's own text (a text box's is not) holds none."""

    def find(elem: ET.Element, path: tuple[int, ...]) -> tuple[int, ...] | None:
        for i, child in enumerate(elem):
            if not isinstance(child.tag, str) or child.tag == W + "txbxContent":
                continue
            if child.tag == tag and child.get(W + "id") == cid:
                return (*path, i)
            inner = find(child, (*path, i))
            if inner is not None:
                return inner
        return None

    path = find(doc.index.spans.parsed(para.span_index), ())
    if path is None:
        return None
    # Pieces are in document order, so those whose path sorts before the marker's precede it.
    before = bisect.bisect_left(para.pieces, path, key=lambda p: p.path)
    return para.pieces[before - 1].end if before else 0


def read_comments(doc: Document) -> list[Comment]:
    """All comments in thread order: each root comment followed by its replies."""
    name = related_part(doc.pkg, OFFICE_REL + "comments", COMMENTS_PART)
    if name is None:
        return []
    ext_name = related_part(doc.pkg, MS_REL_2011 + "commentsExtended", COMMENTS_EXTENDED_PART)
    parent_para: dict[str, str | None] = {}  # paraId -> parent paraId
    done: dict[str, bool] = {}
    if ext_name is not None:
        for ex in parse_part(doc.pkg, ext_name).iter(W15 + "commentEx"):
            para_id = ex.get(W15 + "paraId")
            if para_id is not None:
                parent_para[para_id] = ex.get(W15 + "paraIdParent")
                done[para_id] = (ex.get(W15 + "done") or "0").lower() in ("1", "true", "on")
    locations = _comment_locations(doc)
    quotes = _comment_quotes(doc)
    elements = parse_part(doc.pkg, name).findall(W + "comment")
    stories: dict[str, str] = {}  # comment id -> kind of the story it is anchored in
    if any(c.get(W + "id") not in locations for c in elements):
        from doc_control.model.stories import list_stories

        try:
            found = []
            for story in list_stories(doc.pkg, ("notes", "headers")):
                view = doc.story_view(story.key)
                found.append((story, _comment_locations(view), _comment_quotes(view)))
        except DocControlError:
            found = []  # ponytail: an unreadable note or header part leaves those unplaced
        for story, places, story_quotes in found:
            for ident, at in places.items():
                if ident not in locations:
                    locations[ident] = at
                    stories[ident] = story.kind
                    if ident in story_quotes:
                        quotes[ident] = story_quotes[ident]
    rows: list[tuple[str, str | None, Comment]] = []  # (own paraId, parent paraId, comment)
    para_to_id: dict[str, str] = {}
    for c in elements:
        cid = c.get(W + "id")
        if cid is None:
            continue
        paragraphs = c.findall(W + "p")
        last_para = paragraphs[-1].get(W14 + "paraId") if paragraphs else None
        text = "\n".join(_text(p, (W + "t",)) for p in paragraphs)
        own = last_para or ""
        if own:
            para_to_id[own] = cid
        rows.append(
            (
                own,
                parent_para.get(own),
                Comment(
                    id=cid,
                    author=c.get(W + "author") or "",
                    date=c.get(W + "date") or "",
                    initials=c.get(W + "initials") or "",
                    text=text,
                    resolved=done.get(own, False),
                    reply_to=None,
                    at=locations.get(cid),
                    story=stories.get(cid),
                    quote=quotes.get(cid, ""),
                ),
            )
        )
    roots: list[Comment] = []
    replies: dict[str, list[Comment]] = {}
    for _, parent, comment in rows:
        parent_id = para_to_id.get(parent) if parent else None
        if parent_id is None or parent_id == comment.id:
            roots.append(comment)
        else:
            reply = Comment(**{**comment.__dict__, "reply_to": parent_id})
            replies.setdefault(parent_id, []).append(reply)
    out: list[Comment] = []
    placed: set[str] = set()

    def place(comment: Comment) -> None:
        if comment.id in placed:
            return
        placed.add(comment.id)
        out.append(comment)
        for reply in replies.get(comment.id, []):
            place(reply)

    for root in roots:
        place(root)
    for reply_list in replies.values():  # replies whose chain never reached a root
        for reply in reply_list:
            place(reply)
    return out


def read_revisions(doc: Document) -> list[Revision]:
    """Tracked changes of the document's story (the body, or a story view's) in order.

    A table row or cell revision is reported at the first paragraph of the row or
    cell, before the revisions of that paragraph.
    """
    keyed: list[tuple[int, int, Revision]] = []  # (paragraph number, 0 row/cell | 1 text, rev)
    for para in doc.index.paragraphs:
        if not _REVISION_TAG.search(doc.paragraph_bytes(para)):
            continue
        found: list[Revision] = []
        _collect(doc.index.spans.parse_paragraph(para.span_index), str(para.address), found)
        keyed += [(para.number, 1, r) for r in found]
    data = doc.index.spans.data
    # ponytail: row and cell revisions of the body only (a notes part holds many stories)
    if not doc.index.story and _TABLE_REVISION_TAG.search(data):
        keyed += _table_revisions(doc)
    if not doc.index.story and len(doc.index) and doc.index.spans.body_sect is not None:
        start, end = doc.index.spans.body_sect
        if _SECT_CHANGE_TAG.search(data, start, end):
            change = doc.index.spans.parse_body_sect().find(W + "sectPrChange")
            if change is not None:  # the last section's setup: reported at the last paragraph
                last = doc.index.paragraph(len(doc.index))
                keyed.append(
                    (last.number, 2, _revision(change, "section_format", str(last.address), ""))
                )
    keyed.sort(key=lambda k: (k[0], k[1]))
    return [r for _, _, r in keyed]


def _table_revisions(doc: Document) -> list[tuple[int, int, Revision]]:
    """Row and cell revisions, found by walking the whole body counting paragraphs."""
    root = ET.fromstring(doc.index.spans.data)
    count = 0
    out: list[tuple[int, int, Revision]] = []

    def at(number: int) -> str | None:
        return str(doc.index.paragraph(number).address) if number <= len(doc.index) else None

    def walk(elem: ET.Element) -> None:
        nonlocal count
        for child in elem:
            if child.tag == W + "p":
                count += 1  # numbered like the index: nested paragraphs are not entered
                continue
            if child.tag == W + "tr":
                where = at(count + 1)
                for mark in child.iterfind(f"{W}trPr/*"):
                    if mark.tag in _ROW_REVISIONS and where is not None:
                        out.append(
                            (count + 1, 0, _revision(mark, _ROW_REVISIONS[mark.tag], where, ""))
                        )
            elif child.tag == W + "tc":
                where = at(count + 1)
                for mark in child.iterfind(f"{W}tcPr/*"):
                    if mark.tag in _CELL_REVISIONS and where is not None:
                        out.append(
                            (count + 1, 0, _revision(mark, _CELL_REVISIONS[mark.tag], where, ""))
                        )
            if isinstance(child.tag, str):
                walk(child)

    walk(root)
    if count != len(doc.index):  # numbering disagrees with the index: do not guess
        return []
    return out


#: What shows in the text; a sibling holding none of it (a bookmark, a comment marker, a
#: run with only a comment reference) leaves two changes on either side of it adjacent.
_SHOWN = frozenset(
    W + name
    for name in (
        "t",
        "tab",
        "br",
        "cr",
        "sym",
        "noBreakHyphen",
        "softHyphen",
        "ptab",
        "drawing",
        "pict",
        "object",
        "footnoteReference",
        "endnoteReference",
    )
)
_TEXT_CHANGES = frozenset({W + "ins", W + "del"})


def _collect(elem: ET.Element, at: str, out: list[Revision]) -> None:
    last_change: str | None = None  # id of the w:ins/w:del just before, nothing shown since
    for child in elem:
        tag = child.tag
        if not isinstance(tag, str) or tag == W + "txbxContent":
            continue
        follows = last_change if tag in _TEXT_CHANGES else None
        if tag in _TEXT_CHANGES:
            last_change = child.get(W + "id")
        elif tag != W + "pPr" and any(e.tag in _SHOWN for e in child.iter()):
            last_change = None
        if tag == W + "pPr":
            for mark in child.iterfind(f"{W}rPr/*"):
                if mark.tag in _MARK_REVISIONS:
                    out.append(_revision(mark, _MARK_REVISIONS[mark.tag], at, ""))
                elif mark.tag == W + "rPrChange":
                    out.append(_revision(mark, "mark_format", at, ""))
            change = child.find(W + "pPrChange")
            if change is not None:
                out.append(_revision(change, "paragraph_format", at, ""))
            sect_change = child.find(f"{W}sectPr/{W}sectPrChange")
            if sect_change is not None:
                out.append(_revision(sect_change, "section_format", at, ""))
            continue
        if tag in _RUN_REVISIONS:
            kind = _RUN_REVISIONS[tag]
            if kind in ("insert", "move_to"):  # content later deleted by someone else still counts
                text = _text(child, (W + "t", W + "delText"))
            else:
                text = _text(child, (W + "delText",)) if kind != "format" else ""
            out.append(_revision(child, kind, at, text, follows))
        _collect(child, at, out)


def _revision(
    elem: ET.Element, kind: str, at: str, text: str, follows: str | None = None
) -> Revision:
    return Revision(
        id=elem.get(W + "id") or "",
        type=kind,
        author=elem.get(W + "author") or "",
        date=elem.get(W + "date") or "",
        at=at,
        text=text,
        follows=follows,
    )


def replacements(revisions: Sequence[Revision]) -> dict[tuple[str, str], str]:
    """``(paragraph address, revision id)`` -> id of its other half, for each deletion and
    insertion that make one replacement: same author, same paragraph, one right after the
    other (either order) with nothing visible between them. Each revision is in at most
    one pair."""
    out: dict[tuple[str, str], str] = {}
    seen: dict[tuple[str, str], Revision] = {}
    for rev in revisions:
        prev = seen.get((rev.at, rev.follows)) if rev.follows else None
        seen[rev.at, rev.id] = rev
        if (
            prev is None
            or {prev.type, rev.type} != {"insert", "delete"}
            or prev.author != rev.author
            or (prev.at, prev.id) in out
            or (rev.at, rev.id) in out
        ):
            continue
        out[prev.at, prev.id] = rev.id
        out[rev.at, rev.id] = prev.id
    return out
