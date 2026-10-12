"""Comments and tracked revisions of a document, read-only.

Comments come from ``word/comments.xml`` (text, author, date) joined with
``word/commentsExtended.xml`` (reply parent, resolved state) and located by the
``w:commentRangeStart`` in the body. Revisions are found in the body paragraphs.
Both report the address of the paragraph they sit in.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from doc_control.model.document import Document
from doc_control.model.parts import MS_REL_2011, OFFICE_REL, W14, W15, W, parse_part, related_part

COMMENTS_PART = "word/comments.xml"
COMMENTS_EXTENDED_PART = "word/commentsExtended.xml"
_RANGE_START = re.compile(rb"commentRangeStart|commentReference")
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
REVISION_TYPES = tuple(
    sorted({*_RUN_REVISIONS.values(), *_MARK_REVISIONS.values(), "paragraph_format"})
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
    """Comment id -> address of the first paragraph that starts or references it."""
    out: dict[str, str] = {}
    for para in doc.index.paragraphs:
        if not _RANGE_START.search(doc.paragraph_bytes(para)):
            continue
        elem = doc.index.spans.parse_paragraph(para.span_index)
        for child in elem.iter():
            if child.tag in (W + "commentRangeStart", W + "commentReference"):
                cid = child.get(W + "id")
                if cid is not None:
                    out.setdefault(cid, str(para.address))
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
    """Tracked changes of the main story in document order."""
    out: list[Revision] = []
    for para in doc.index.paragraphs:
        if not _REVISION_TAG.search(doc.paragraph_bytes(para)):
            continue
        elem = doc.index.spans.parse_paragraph(para.span_index)
        _collect(elem, str(para.address), out)
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
            change = child.find(W + "pPrChange")
            if change is not None:
                out.append(_revision(change, "paragraph_format", at, ""))
            continue
        if tag in _RUN_REVISIONS:
            kind = _RUN_REVISIONS[tag]
            text = _text(child, (W + "t",) if kind in ("insert", "move_to") else (W + "delText",))
            out.append(_revision(child, kind, at, text))
            if kind in ("insert", "move_to", "delete", "move_from"):
                continue  # their content is the revision's text, not further revisions
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
