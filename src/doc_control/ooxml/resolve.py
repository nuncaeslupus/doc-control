"""Accepting and rejecting tracked changes in one paragraph element.

:func:`scan` lists the revisions of a paragraph (as ``revisions.list`` reports
them) and :func:`resolve_paragraph` applies a decision, ``accept`` or ``reject``,
to chosen ones. Both work on the element from
:meth:`doc_control.ooxml.splice.DocumentSpans.parse_paragraph`; the element is
edited in place and the caller serialises and splices it.

What each revision does
    ``w:ins``, ``w:moveTo`` (content): accepting unwraps the element and keeps its
    content, rejecting removes it with everything inside.
    ``w:del``, ``w:moveFrom``: accepting removes the element with its content,
    rejecting unwraps it and turns ``w:delText``/``w:delInstrText`` back into
    ``w:t``/``w:instrText``.
    ``w:rPrChange``: accepting drops the record, rejecting puts the recorded
    run properties back. ``w:pPrChange``: the same for paragraph properties
    (the paragraph's mark properties and section break stay).
    Paragraph marks (``w:pPr/w:rPr`` holding ``w:ins``, ``w:del``, ``w:moveFrom``
    or ``w:moveTo``): the marker is dropped; when the decision removes the mark
    (accepting a deleted or moved-away mark, rejecting an inserted or moved-in
    one) :func:`resolve_paragraph` reports it, and the caller joins the paragraph
    with the next one.

Nesting
    Decisions apply top down: rejecting an insertion removes a deletion nested in
    it, whatever its own decision; accepting it unwraps it and leaves the nested
    deletion pending (or resolved by its own decision).

What survives a removal
    Bookmark, comment-range and permission markers inside removed content are kept
    where they were, and so is a run holding a comment reference, so removing text
    never orphans a comment or splits a bookmark.

Not covered: revisions in text boxes (``w:txbxContent``) are left as they are, as
``revisions.list`` does not report them, and table row and cell revisions live
outside paragraphs.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from doc_control.ooxml.revisions import AUTHOR, DEL, DELTEXT, ID, INS, PPR, RPR, R, T, W

MOVE_FROM, MOVE_TO = W + "moveFrom", W + "moveTo"
RPR_CHANGE, PPR_CHANGE = W + "rPrChange", W + "pPrChange"
SECT_PR = W + "sectPr"
_TEXT_BOX = W + "txbxContent"
_DEL_INSTR, _INSTR = W + "delInstrText", W + "instrText"
_COMMENT_REFERENCE = W + "commentReference"
_KEPT_MARKERS = frozenset(
    W + name
    for name in (
        "bookmarkStart",
        "bookmarkEnd",
        "commentRangeStart",
        "commentRangeEnd",
        "permStart",
        "permEnd",
    )
)

_CONTENT_KIND = {INS: "insert", DEL: "delete", MOVE_FROM: "move_from", MOVE_TO: "move_to"}
_MARK_KIND = {
    INS: "mark_insert",
    DEL: "mark_delete",
    MOVE_FROM: "mark_move_from",
    MOVE_TO: "mark_move_to",
}
_MARK_REMOVED = frozenset(
    {
        ("mark_insert", "reject"),
        ("mark_move_to", "reject"),
        ("mark_delete", "accept"),
        ("mark_move_from", "accept"),
    }
)
_KEEPS_CONTENT_WHEN_ACCEPTED = frozenset({INS, MOVE_TO})

ACCEPT, REJECT = "accept", "reject"


@dataclass(eq=False)
class Revision:
    """One revision found in a paragraph; ``kind`` is the ``revisions.list`` type."""

    id: str
    kind: str
    author: str
    elem: ET.Element


def scan(p: ET.Element) -> list[Revision]:
    """The revisions of paragraph ``p`` in document order (text boxes excluded)."""
    out: list[Revision] = []
    _scan(p, out)
    return out


def _scan(elem: ET.Element, out: list[Revision]) -> None:
    for child in elem:
        tag = child.tag
        if not isinstance(tag, str) or tag == _TEXT_BOX:
            continue
        if tag == PPR:
            for mark in child.iterfind(f"{RPR}/*"):
                if mark.tag in _MARK_KIND:
                    out.append(_revision(mark, _MARK_KIND[mark.tag]))
                elif mark.tag == RPR_CHANGE:
                    out.append(_revision(mark, "mark_format"))
            change = child.find(PPR_CHANGE)
            if change is not None:
                out.append(_revision(change, "paragraph_format"))
            continue
        if tag in _CONTENT_KIND:
            out.append(_revision(child, _CONTENT_KIND[tag]))
        elif tag == RPR_CHANGE:
            out.append(_revision(child, "format"))
        _scan(child, out)


def _revision(elem: ET.Element, kind: str) -> Revision:
    return Revision(elem.get(ID) or "", kind, elem.get(AUTHOR) or "", elem)


def resolve_paragraph(p: ET.Element, actions: dict[int, str]) -> bool:
    """Apply ``actions`` (``id(revision.elem)`` -> ``"accept"``/``"reject"``) to ``p``.

    Returns True when a decision removes the paragraph's mark: the caller joins ``p``
    with the paragraph after it (or keeps ``p`` if there is none).
    """
    ppr = p.find(PPR)
    removed = _resolve_ppr(ppr, actions) if ppr is not None else False
    _walk(p, actions)
    return removed


def _resolve_ppr(ppr: ET.Element, actions: dict[int, str]) -> bool:
    removed = False
    rpr = ppr.find(RPR)
    if rpr is not None:
        emptied = False
        for mark in list(rpr):
            act = actions.get(id(mark))
            if act is None or mark.tag not in _MARK_KIND:
                continue
            rpr.remove(mark)
            emptied = True
            if (_MARK_KIND[mark.tag], act) in _MARK_REMOVED:
                removed = True
        _resolve_rpr(rpr, actions)
        if emptied and len(rpr) == 0:
            ppr.remove(rpr)
    change = ppr.find(PPR_CHANGE)
    act = actions.get(id(change)) if change is not None else None
    if change is not None and act is not None:
        if act == REJECT:
            old = change.find(PPR)
            kept = [c for c in ppr if c.tag in (RPR, SECT_PR)]
            ppr[:] = ([copy.deepcopy(c) for c in old] if old is not None else []) + kept
        else:
            ppr.remove(change)
    return removed


def _resolve_rpr(rpr: ET.Element, actions: dict[int, str]) -> None:
    change = rpr.find(RPR_CHANGE)
    act = actions.get(id(change)) if change is not None else None
    if change is None or act is None:
        return
    if act == REJECT:
        old = change.find(RPR)
        keep = [c for c in rpr if c.tag in _MARK_KIND]  # revision marks of a paragraph mark
        rpr[:] = keep + ([copy.deepcopy(c) for c in old] if old is not None else [])
    else:
        rpr.remove(change)


def _walk(node: ET.Element, actions: dict[int, str]) -> None:
    i = 0
    while i < len(node):
        child = node[i]
        tag = child.tag
        if not isinstance(tag, str) or tag in (_TEXT_BOX, PPR):
            i += 1
            continue
        if tag == RPR:
            _resolve_rpr(child, actions)
            i += 1
            continue
        act = actions.get(id(child)) if tag in _CONTENT_KIND else None
        if act is None:
            _walk(child, actions)
            i += 1
            continue
        if (tag in _KEEPS_CONTENT_WHEN_ACCEPTED) == (act == ACCEPT):
            _walk(child, actions)
            content = list(child)
            if tag in (DEL, MOVE_FROM):
                for c in content:
                    _undelete(c)
            node[i : i + 1] = content
            i += len(content)
        else:
            left = _survivors(child)
            node[i : i + 1] = left
            i += len(left)


def _undelete(elem: ET.Element) -> None:
    """Turn deleted text back into text, except inside a nested deletion."""
    if elem.tag == DELTEXT:
        elem.tag = T
    elif elem.tag == _DEL_INSTR:
        elem.tag = _INSTR
    for c in elem:
        if c.tag not in (DEL, MOVE_FROM):
            _undelete(c)


def _survivors(elem: ET.Element) -> list[ET.Element]:
    """What stays where removed content was: markers and comment-reference runs."""
    out: list[ET.Element] = []
    for c in elem:
        tag = c.tag
        if not isinstance(tag, str):
            continue
        if tag in _KEPT_MARKERS:
            out.append(c)
        elif tag == R:
            if any(x.tag == _COMMENT_REFERENCE for x in c):
                c[:] = [x for x in c if x.tag in (RPR, _COMMENT_REFERENCE)]
                out.append(c)
        elif tag.startswith(W) and tag != RPR:
            out.extend(_survivors(c))
    return out
