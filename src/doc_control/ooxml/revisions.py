"""Tracked-change markup for one paragraph: ``w:ins``, ``w:del`` and paragraph marks.

:class:`ParagraphEditor` edits a paragraph element from
:meth:`doc_control.ooxml.splice.DocumentSpans.parse_paragraph` and writes every
change as a revision, so rejecting the revisions restores the original text and
accepting them gives the new text.

Markup written
    Deleted text keeps its run (and the run's ``w:rPr``) and the run moves into a
    ``w:del`` with ``w:t`` renamed ``w:delText``. Inserted text is one run inside
    a ``w:ins``; its ``w:rPr`` is a copy of the run at the start of the range.
    A deleted paragraph has ``w:pPr/w:rPr/w:del`` on its mark, an inserted one
    ``w:pPr/w:rPr/w:ins``. A run is split where a range starts or ends; the new
    half copies the run's attributes and ``w:rPr``. Text inside someone else's
    ``w:ins`` is deleted with a ``w:del`` nested in it (what Word writes); new
    text is never nested there: the foreign ``w:ins`` is split in two, the
    continuation getting a fresh id, author and date copied.

What a range may cover
    Only runs made of text (``w:t``, ``w:tab``, ``w:br``, ``w:cr``, hyphens,
    ``w:sym``, ``w:ptab``) directly in the paragraph or in a ``w:ins``, outside
    fields. Text in a hyperlink, field, smart tag, content control, ruby or
    alternate content, a run holding a drawing, a footnote reference or a field
    character, or such a run between the two ends of the range, raises
    ``UNSUPPORTED_CONTENT`` (spec §Risks). Bookmark and comment-range markers,
    proof marks and existing deletions between the ends stay where they are.

Ids
    :class:`RevisionContext` hands out ``w:id`` values above every ``w:id`` in
    the document, comments, footnotes, endnotes, headers and footers, so a new
    revision never shares an id with a revision, comment or bookmark.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import Paragraph, Piece
from doc_control.ooxml.splice import W_NS, XML_NS
from doc_control.ooxml.zipio import Package

W = f"{{{W_NS}}}"
XML_SPACE = f"{{{XML_NS}}}space"

P, PPR, RPR, R = W + "p", W + "pPr", W + "rPr", W + "r"
T, DELTEXT, INS, DEL = W + "t", W + "delText", W + "ins", W + "del"
BR = W + "br"
ID, AUTHOR, DATE = W + "id", W + "author", W + "date"

DEFAULT_AUTHOR = "Claude"
MAX_ID = 2**31 - 1
STORY_PART = re.compile(r"^word/(document|comments|footnotes|endnotes|header\d*|footer\d*)\.xml$")

_TEXT_TOKENS = frozenset(
    W + name for name in ("t", "tab", "cr", "noBreakHyphen", "softHyphen", "sym", "ptab", "br")
)
_QUIET_TOKENS = frozenset({RPR, W + "lastRenderedPageBreak"})
_INERT_TOKENS = _QUIET_TOKENS | {W + "commentReference", W + "annotationRef"}
_INERT_ELEMENTS = frozenset(
    W + name
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
_MARK_TAGS = frozenset({INS, DEL, W + "moveFrom", W + "moveTo", W + "rPrChange"})
_PAGE_BREAKS = frozenset({"page", "column"})
_VANISH_OFF = frozenset({"0", "false", "off"})
_PAGE_BREAK_TOKEN = "page-break"


def format_date(now: datetime | None = None) -> str:
    """``now`` (default: the current time) as ISO 8601 UTC with ``Z``, to the second."""
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        raise ValueError("revision date must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_author(author: str, tag: str | None) -> str:
    """The ``w:author`` value: the author, with the per-call tag in parentheses if given."""
    return f"{author} ({tag})" if tag else author


def used_ids(pkg: Package) -> set[int]:
    """Every numeric ``w:id`` in the package's story parts (revisions, comments, bookmarks…)."""
    ids: set[int] = set()
    for name in pkg.names:
        if not STORY_PART.match(name):
            continue
        try:
            root = ET.fromstring(pkg.read(name))
        except ET.ParseError as exc:
            raise DocControlError(
                ErrorCode.NOT_A_DOCX, f"{name} does not parse: {exc}", part=name
            ) from exc
        for elem in root.iter():
            raw = elem.get(ID)
            if raw is None:
                continue
            try:
                ids.add(int(raw.strip()))
            except ValueError:
                continue
    return ids


@dataclass
class RevisionContext:
    """Author, date and the id counter shared by every revision one call writes.

    ``revision_ids`` lists the ids of the revisions the call authored (what
    "reject every revision the tool wrote" means); ``continuation_ids`` the ids
    given to the second half of someone else's revision that had to be split.
    """

    used: set[int]
    author: str
    date: str
    revision_ids: list[str] = field(default_factory=list)
    continuation_ids: list[str] = field(default_factory=list)
    _next: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        self._next = max(max(self.used, default=0), 0) + 1

    def _allocate(self) -> str:
        value = self._next
        if value > MAX_ID:
            raise DocControlError(ErrorCode.INTERNAL, "no revision ids left in this document")
        self._next += 1
        return str(value)

    def revision(self, kind: str) -> ET.Element:
        """A new ``w:ins`` or ``w:del`` carrying id, author and date."""
        rid = self._allocate()
        self.revision_ids.append(rid)
        return ET.Element(W + kind, {ID: rid, AUTHOR: self.author, DATE: self.date})

    def continuation_id(self) -> str:
        rid = self._allocate()
        self.continuation_ids.append(rid)
        return rid


def unsupported(message: str, **details: object) -> DocControlError:
    return DocControlError(ErrorCode.UNSUPPORTED_CONTENT, message, **details)


# -- small element helpers ------------------------------------------------


def _elements(node: ET.Element) -> list[ET.Element]:
    """Child elements, leaving out comments and processing instructions."""
    return [c for c in node if isinstance(c.tag, str)]


def _set_space(t: ET.Element) -> None:
    text = t.text or ""
    if text != text.strip():
        t.set(XML_SPACE, "preserve")


def _text_element(tag: str, text: str) -> ET.Element:
    elem = ET.Element(tag)
    elem.text = text
    _set_space(elem)
    return elem


def _tokens(run: ET.Element) -> set[str]:
    out: set[str] = set()
    for child in run:
        if not isinstance(child.tag, str):
            continue
        if child.tag == BR and child.get(W + "type") in _PAGE_BREAKS:
            out.add(_PAGE_BREAK_TOKEN)
        else:
            out.add(child.tag)
    return out


def _hidden(run: ET.Element) -> bool:
    rpr = run.find(RPR)
    vanish = rpr.find(W + "vanish") if rpr is not None else None
    return vanish is not None and vanish.get(W + "val", "1").lower() not in _VANISH_OFF


def _run_kind(run: ET.Element) -> str:
    """``text``, ``inert`` (nothing to read or delete), ``hidden`` or ``other`` (structure)."""
    tokens = _tokens(run)
    plain = tokens <= (_TEXT_TOKENS | _QUIET_TOKENS)
    if _hidden(run):
        return "hidden" if tokens <= (_TEXT_TOKENS | _INERT_TOKENS) else "other"
    if plain and tokens & _TEXT_TOKENS:
        return "text"
    if tokens <= _INERT_TOKENS:
        return "inert"
    return "other"


def _item_kind(node: ET.Element) -> str:
    if node.tag == R:
        return _run_kind(node)
    if node.tag in _INERT_ELEMENTS or node.tag == DEL:
        return "inert"
    return "other"


def _flat(p: ET.Element) -> list[tuple[ET.Element, ET.Element]]:
    """``(parent, node)`` for the paragraph's content in order, entering ``w:ins`` wrappers."""
    items: list[tuple[ET.Element, ET.Element]] = []
    for child in _elements(p):
        if child.tag == PPR:
            continue
        if child.tag == INS:
            items.extend((child, sub) for sub in _elements(child))
        else:
            items.append((p, child))
    return items


def _clone_rpr(rpr: ET.Element, ctx: RevisionContext, *, keep_changes: bool) -> ET.Element:
    """A copy of a run's ``w:rPr``; a tracked format change either gets a new id or is dropped."""
    clone = copy.deepcopy(rpr)
    for parent in list(clone.iter()):
        for child in list(parent):
            if child.tag == W + "rPrChange" and not keep_changes:
                parent.remove(child)
    for elem in clone.iter():
        if elem.get(ID) is not None:
            elem.set(ID, ctx.continuation_id())
    return clone


def clone_run(run: ET.Element, ctx: RevisionContext) -> ET.Element:
    """A copy of ``run`` for new content: formatting kept, tracked format changes dropped."""
    new = ET.Element(run.tag, dict(run.attrib))
    for child in run:
        if child.tag == RPR:
            new.append(_clone_rpr(child, ctx, keep_changes=False))
        else:
            new.append(copy.deepcopy(child))
    return new


def text_children(text: str) -> list[ET.Element]:
    """``w:t``/``w:tab``/``w:br`` children giving exactly ``text`` as visible text."""
    out: list[ET.Element] = []
    for part in re.split(r"([\t\n])", text):
        if part == "\t":
            out.append(ET.Element(W + "tab"))
        elif part == "\n":
            out.append(ET.Element(BR))
        elif part:
            out.append(_text_element(T, part))
    return out


# -- paragraph marks --------------------------------------------------------


def mark_state(p: ET.Element) -> set[tuple[str, str]]:
    """``(kind, id)`` of the revisions on the paragraph mark (``ins``, ``del``…)."""
    rpr = _mark_rpr(p)
    if rpr is None:
        return set()
    return {(c.tag.removeprefix(W), c.get(ID, "")) for c in _elements(rpr) if c.tag in _MARK_TAGS}


def _mark_rpr(p: ET.Element) -> ET.Element | None:
    ppr = p.find(PPR)
    return ppr.find(RPR) if ppr is not None else None


def has_section_break(p: ET.Element) -> bool:
    ppr = p.find(PPR)
    return ppr is not None and ppr.find(W + "sectPr") is not None


def set_mark(p: ET.Element, kind: str, ctx: RevisionContext) -> None:
    """Track the paragraph mark as inserted (``ins``) or deleted (``del``)."""
    if has_section_break(p):
        raise unsupported("the paragraph ends a section: its mark cannot be tracked")
    ppr = p.find(PPR)
    if ppr is None:
        ppr = ET.Element(PPR)
        p.insert(0, ppr)
    rpr = ppr.find(RPR)
    if rpr is None:
        rpr = ET.Element(RPR)
        later = [i for i, c in enumerate(ppr) if c.tag in (W + "sectPr", W + "pPrChange")]
        ppr.insert(later[0] if later else len(ppr), rpr)
    if any(c.tag == W + kind for c in rpr):
        return  # already tracked by someone else
    where = 0
    if kind == "del" and len(rpr) and rpr[0].tag == INS:
        where = 1  # CT_ParaRPr orders ins before del
    rpr.insert(where, ctx.revision(kind))


def new_paragraph(
    reference: ET.Element | None, ctx: RevisionContext, *, inserted_mark: bool
) -> tuple[ET.Element, ET.Element | None]:
    """An empty paragraph with the reference paragraph's properties.

    Returns the ``w:p`` and the run properties its text should use (the
    reference paragraph's mark properties). Section breaks, tracked property
    changes and the reference's revision marks are not copied.
    """
    p = ET.Element(P)
    ppr = ET.Element(PPR)
    src = reference.find(PPR) if reference is not None else None
    if src is not None:
        ppr = copy.deepcopy(src)
        for child in list(ppr):
            if child.tag in (W + "sectPr", W + "pPrChange"):
                ppr.remove(child)
    rpr = ppr.find(RPR)
    if rpr is None:
        rpr = ET.Element(RPR)
        ppr.append(rpr)
    for child in list(rpr):
        if child.tag in _MARK_TAGS:
            rpr.remove(child)
    run_props: ET.Element | None = None
    if len(rpr):
        run_props = _clone_rpr(rpr, ctx, keep_changes=False)
    if inserted_mark:
        rpr.insert(0, ctx.revision("ins"))
    elif not len(rpr):
        ppr.remove(rpr)
    if len(ppr):
        p.append(ppr)
    return p, run_props


def inserted_content(
    ctx: RevisionContext,
    *,
    text: str | None = None,
    run_props: ET.Element | None = None,
    runs: list[ET.Element] | None = None,
) -> ET.Element | None:
    """A ``w:ins`` holding a new run for ``text`` or the given ``runs``; None if both are empty."""
    if runs is None:
        if not text:
            return None
        run = ET.Element(R)
        if run_props is not None:
            run.append(copy.deepcopy(run_props))
        run.extend(text_children(text))
        runs = [run]
    if not runs:
        return None
    ins = ctx.revision("ins")
    ins.extend(runs)
    return ins


# -- the paragraph editor -----------------------------------------------------


class ParagraphEditor:
    """Edits one paragraph element in place as tracked changes."""

    def __init__(self, p: ET.Element, para: Paragraph, ctx: RevisionContext) -> None:
        self.p = p
        self.para = para
        self.ctx = ctx

    # -- locating ----------------------------------------------------------

    def _where(self, what: str) -> dict[str, object]:
        return {"paragraph": str(self.para.address), "where": what}

    def _site(self, piece: Piece) -> tuple[ET.Element, ET.Element, int]:
        """``(parent, run, child index)`` of a piece in an editable place, else a refusal."""
        if piece.in_field:
            raise unsupported("the range covers text inside a field", **self._where("field"))
        nodes = [self.p]
        try:
            for i in piece.path:
                nodes.append(nodes[-1][i])
        except IndexError as exc:
            raise DocControlError(
                ErrorCode.INTERNAL, f"{self.para.address}: index does not match the paragraph"
            ) from exc
        inner, run, leaf = nodes[1:-2], nodes[-2], nodes[-1]
        if run.tag != R:
            raise unsupported("the range covers text outside a run", **self._where("structure"))
        if len(inner) > 1 or (inner and inner[0].tag != INS):
            kind = inner[0].tag.removeprefix(W) if inner and isinstance(inner[0].tag, str) else "?"
            raise unsupported(
                f"the range covers text inside a {kind} element, which v1 does not edit",
                **self._where(kind),
            )
        kind = _run_kind(run)
        if kind != "text":
            raise unsupported(
                "the range covers a run with a drawing, reference or other structure",
                **self._where("run"),
            )
        parent = inner[0] if inner else self.p
        # An edit to the right of this one may already have cut the text short.
        seen = "".join(leaf.itertext()) if leaf.tag == T else None
        if seen is not None and (
            len(leaf) or not self.para.text[piece.start : piece.end].startswith(seen)
        ):
            raise DocControlError(
                ErrorCode.INTERNAL, f"{self.para.address}: index does not match the text"
            )
        return parent, run, piece.path[-1]

    def _require_plain(self) -> None:
        """Refuse a paragraph holding anything but plain runs, markers and revisions."""
        for _, node in _flat(self.p):
            if _item_kind(node) == "other":
                raise unsupported(
                    "the paragraph holds a field, link, drawing, reference or other structure",
                    **self._where(node.tag.removeprefix(W) if isinstance(node.tag, str) else "?"),
                )

    # -- cutting runs ----------------------------------------------------------

    def _cut(
        self, parent: ET.Element, run: ET.Element, k: int, offset: int
    ) -> tuple[ET.Element | None, ET.Element | None]:
        """Cut ``run`` before character ``offset`` of its child ``k``.

        Returns ``(left, right)``: the run ending at the cut and the run
        starting at it. One is ``None`` when the cut is at the run's edge, and
        then the other is ``run`` itself. The left half keeps the original
        element, so paths that point at or before the cut stay valid.
        """
        child = run[k]
        b = k
        if child.tag == T:
            text = child.text or ""
            if offset >= len(text):
                b = k + 1
            elif offset > 0:
                tail = ET.Element(T, dict(child.attrib))
                tail.text = text[offset:]
                child.text = text[:offset]
                _set_space(child)
                _set_space(tail)
                run.insert(k + 1, tail)
                b = k + 1
        elif offset > 0:
            b = k + 1
        kids = list(run)
        before = [c for c in kids[:b] if isinstance(c.tag, str) and c.tag != RPR]
        after = [c for c in kids[b:] if isinstance(c.tag, str)]
        if not before:
            return None, run
        if not after:
            return run, None
        right = ET.Element(run.tag, dict(run.attrib))
        rpr = run.find(RPR)
        if rpr is not None:
            right.append(_clone_rpr(rpr, self.ctx, keep_changes=True))
        for c in kids[b:]:
            run.remove(c)
            right.append(c)
        parent.insert(list(parent).index(run) + 1, right)
        return run, right

    def _wrapper_copy(self, wrapper: ET.Element) -> ET.Element:
        """An empty ``w:ins`` like ``wrapper`` (same author and date) with a fresh id."""
        new = ET.Element(wrapper.tag, dict(wrapper.attrib))
        new.set(ID, self.ctx.continuation_id())
        return new

    def _insert_after(self, parent: ET.Element, node: ET.Element, new: ET.Element) -> None:
        """Put ``new`` right after ``node``, outside any ``w:ins`` ``parent`` is."""
        if parent is self.p:
            parent.insert(list(parent).index(node) + 1, new)
            return
        kids = list(parent)
        following = kids[kids.index(node) + 1 :]
        at = list(self.p).index(parent) + 1
        if following:
            rest = self._wrapper_copy(parent)
            for c in following:
                parent.remove(c)
                rest.append(c)
            self.p.insert(at, rest)
        self.p.insert(at, new)

    def _insert_before(self, parent: ET.Element, node: ET.Element, new: ET.Element) -> None:
        """Put ``new`` right before ``node``, outside any ``w:ins`` ``parent`` is."""
        if parent is self.p:
            parent.insert(list(parent).index(node), new)
            return
        kids = list(parent)
        at = list(self.p).index(parent)
        if any(isinstance(c.tag, str) for c in kids[: kids.index(node)]):
            rest = self._wrapper_copy(parent)
            for c in kids[kids.index(node) :]:
                parent.remove(c)
                rest.append(c)
            self.p.insert(at + 1, rest)
            self.p.insert(at + 1, new)
        else:
            self.p.insert(at, new)

    # -- the edits ---------------------------------------------------------------

    def replace(self, start: int, end: int, text: str | None) -> None:
        """Track the deletion of visible characters ``[start, end)`` and insert ``text`` there.

        ``text`` of ``None`` or ``""`` only deletes. With ``start == end`` the
        text is inserted at that offset. The caller has checked the offsets.
        """
        if start == end:
            if text:
                self._insert_at(start, text)
            return
        pieces = self.para.pieces_in(start, end)
        sites = [self._site(piece) for piece in pieces]
        first_piece, last_piece = pieces[0], pieces[-1]
        first_parent, first_run, first_k = sites[0]
        last_parent, last_run, last_k = sites[-1]
        rpr = first_run.find(RPR)
        run_props = _clone_rpr(rpr, self.ctx, keep_changes=False) if rpr is not None else None

        left, _ = self._cut(last_parent, last_run, last_k, end - last_piece.start)
        assert left is not None
        last_run = left
        _, right = self._cut(first_parent, first_run, first_k, start - first_piece.start)
        if right is not None:
            first_run = right
            if last_run is sites[0][1]:  # the range ended in the run that was just split
                last_run = right

        items = _flat(self.p)
        order = {id(node): i for i, (_, node) in enumerate(items)}
        span = items[order[id(first_run)] : order[id(last_run)] + 1]
        groups: list[list[tuple[ET.Element, ET.Element]]] = []
        broken = True
        for parent, node in span:
            kind = _item_kind(node)
            if kind == "other":
                raise unsupported(
                    "the range covers a field, link, drawing, reference or other structure",
                    **self._where(node.tag.removeprefix(W) if isinstance(node.tag, str) else "?"),
                )
            if kind == "text":
                if broken or groups[-1][-1][0] is not parent:
                    groups.append([])
                groups[-1].append((parent, node))
                broken = False
            else:
                broken = True
        last_del: ET.Element | None = None
        last_parent_of_del = self.p
        for group in groups:
            parent = group[0][0]
            runs = [node for _, node in group]
            at = list(parent).index(runs[0])
            deletion = self.ctx.revision("del")
            for run in runs:
                parent.remove(run)
                for child in run:
                    if child.tag == T:
                        child.tag = DELTEXT
                deletion.append(run)
            parent.insert(at, deletion)
            last_del, last_parent_of_del = deletion, parent
        if text:
            assert last_del is not None
            ins = inserted_content(self.ctx, text=text, run_props=run_props)
            assert ins is not None
            self._insert_after(last_parent_of_del, last_del, ins)

    def _insert_at(self, offset: int, text: str) -> None:
        """Insert ``text`` at visible ``offset`` (no characters deleted)."""
        after = self.para.pieces_in(offset, offset + 1)
        before = self.para.pieces_in(offset - 1, offset) if offset > 0 else []
        run_props: ET.Element | None = None
        if after and not after[0].in_field and self._editable(after[0]):
            parent, run, k = self._site(after[0])
            run_props = self._props_of(run)
            _, right = self._cut(parent, run, k, offset - after[0].start)
            target = right if right is not None else run
            ins = inserted_content(self.ctx, text=text, run_props=run_props)
            assert ins is not None
            self._insert_before(parent, target, ins)
        elif before and not before[0].in_field and self._editable(before[0]):
            parent, run, k = self._site(before[0])
            run_props = self._props_of(run)
            left, _ = self._cut(parent, run, k, offset - before[0].start)
            target = left if left is not None else run
            ins = inserted_content(self.ctx, text=text, run_props=run_props)
            assert ins is not None
            self._insert_after(parent, target, ins)
        elif not self.para.pieces:
            self._require_plain()
            ins = inserted_content(self.ctx, text=text, run_props=self._mark_props())
            assert ins is not None
            self.p.append(ins)
        else:
            raise unsupported(
                "the insertion point is next to a field, link or other structure",
                **self._where("insertion point"),
            )

    def _editable(self, piece: Piece) -> bool:
        try:
            self._site(piece)
        except DocControlError as exc:
            if exc.code is ErrorCode.UNSUPPORTED_CONTENT:
                return False
            raise
        return True

    def _props_of(self, run: ET.Element) -> ET.Element | None:
        rpr = run.find(RPR)
        return _clone_rpr(rpr, self.ctx, keep_changes=False) if rpr is not None else None

    def _mark_props(self) -> ET.Element | None:
        rpr = _mark_rpr(self.p)
        if rpr is None:
            return None
        props = _clone_rpr(rpr, self.ctx, keep_changes=False)
        for child in list(props):
            if child.tag in _MARK_TAGS:
                props.remove(child)
        return props if len(props) else None

    def delete_content(self) -> None:
        """Track the deletion of everything visible in the paragraph."""
        self._require_plain()
        if self.para.text:
            self.replace(0, len(self.para.text), None)

    def copy_runs(self) -> list[ET.Element]:
        """Copies of the paragraph's visible runs (for a move), made before it is edited."""
        self._require_plain()
        return [
            clone_run(node, self.ctx)
            for _, node in _flat(self.p)
            if node.tag == R and _run_kind(node) in ("text", "hidden")
        ]

    def mark(self, kind: str) -> None:
        set_mark(self.p, kind, self.ctx)


# -- simulating accept and reject (used to verify a write) ----------------------


def reject_revisions(p: ET.Element, ids: set[str]) -> None:
    """Reject, in place, the revisions of ``p`` whose id is in ``ids`` (marks excluded).

    Inserted content is removed; a deletion is unwrapped and its ``w:delText``
    becomes ``w:t`` again.
    """

    def walk(node: ET.Element) -> None:
        for child in list(node):
            if not isinstance(child.tag, str):
                continue
            mine = child.get(ID) in ids and node.tag != RPR
            if child.tag == INS and mine:
                node.remove(child)
            elif child.tag == DEL and mine:
                at = list(node).index(child)
                node.remove(child)
                for offset, sub in enumerate(list(child)):
                    for elem in sub.iter():
                        if elem.tag == DELTEXT:
                            elem.tag = T
                    node.insert(at + offset, sub)
                walk_range(node, at, len(child))
            else:
                walk(child)

    def walk_range(node: ET.Element, at: int, count: int) -> None:
        for sub in list(node)[at : at + count]:
            if isinstance(sub.tag, str):
                walk(sub)

    walk(p)
