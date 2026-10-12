"""Comments and tracked revisions of a document, read-only.

Comments come from ``word/comments.xml`` (text, author, date) joined with
``word/commentsExtended.xml`` (reply parent, resolved state) and located by the
``w:commentRangeStart`` in the body. Revisions are found in the body paragraphs.
Both report the address of the paragraph they sit in.
"""

from __future__ import annotations

import bisect
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from doc_control.model.document import Document
from doc_control.model.parts import MS_REL_2011, OFFICE_REL, W14, W15, W, parse_part, related_part

COMMENTS_PART = "word/comments.xml"
COMMENTS_EXTENDED_PART = "word/commentsExtended.xml"
_RANGE_START = re.compile(rb"<(?:[\w.-]+:)?commentRangeStart\b[^>]*>")
_REFERENCE = re.compile(rb"<(?:[\w.-]+:)?commentReference\b[^>]*>")
_ID_ATTR = re.compile(rb"[\s:]id\s*=\s*[\"']([^\"']*)[\"']")
_TABLE_REVISION_TAG = re.compile(
    rb"<(?:[\w.-]+:)?(?:cellIns|cellDel|cellMerge)[\s/>]"
    rb"|<(?:[\w.-]+:)?trPr[\s>](?:(?!</(?:[\w.-]+:)?trPr>).)*?<(?:[\w.-]+:)?(?:ins|del)[\s/>]",
    re.DOTALL,
)
_REVISION_TAG = re.compile(rb"<(?:[\w.-]+:)?(?:ins|del|moveFrom|moveTo|rPrChange|pPrChange)[\s/>]")

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


@dataclass(frozen=True)
class Revision:
    id: str
    type: str
    author: str
    date: str
    at: str
    text: str


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
            elif i + 1 < len(starts):
                para = doc.index.paragraphs[i + 1]
            else:
                continue
            out.setdefault(cid.group(1).decode(), str(para.address))
    return out


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
    rows: list[tuple[str, str | None, Comment]] = []  # (own paraId, parent paraId, comment)
    para_to_id: dict[str, str] = {}
    for c in parse_part(doc.pkg, name).findall(W + "comment"):
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
    """Tracked changes of the main story in document order.

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
    if _TABLE_REVISION_TAG.search(data):
        keyed += _table_revisions(doc)
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


def _collect(elem: ET.Element, at: str, out: list[Revision]) -> None:
    for child in elem:
        tag = child.tag
        if not isinstance(tag, str) or tag == W + "txbxContent":
            continue
        if tag == W + "pPr":
            for mark in child.iterfind(f"{W}rPr/*"):
                if mark.tag in _MARK_REVISIONS:
                    out.append(_revision(mark, _MARK_REVISIONS[mark.tag], at, ""))
                elif mark.tag == W + "rPrChange":
                    out.append(_revision(mark, "mark_format", at, ""))
            change = child.find(W + "pPrChange")
            if change is not None:
                out.append(_revision(change, "paragraph_format", at, ""))
            continue
        if tag in _RUN_REVISIONS:
            kind = _RUN_REVISIONS[tag]
            if kind in ("insert", "move_to"):  # content later deleted by someone else still counts
                text = _text(child, (W + "t", W + "delText"))
            else:
                text = _text(child, (W + "delText",)) if kind != "format" else ""
            out.append(_revision(child, kind, at, text))
        _collect(child, at, out)


def _revision(elem: ET.Element, kind: str, at: str, text: str) -> Revision:
    return Revision(
        id=elem.get(W + "id") or "",
        type=kind,
        author=elem.get(W + "author") or "",
        date=elem.get(W + "date") or "",
        at=at,
        text=text,
    )
