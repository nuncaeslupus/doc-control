"""The five comment parts of a package, written together so they never disagree.

``word/comments.xml`` holds the comments; ``commentsExtended.xml`` the reply link
(``w15:paraIdParent``) and the resolved flag (``w15:done``) of each, keyed by the
``w14:paraId`` of the comment's *last* paragraph; ``commentsIds.xml`` maps that
``paraId`` to a ``w16cid:durableId``; ``commentsExtensible.xml`` repeats the durable id
with the creation time; ``people.xml`` lists the authors.

:class:`CommentParts` reads whichever of them exist and collects additions
(:meth:`add`) and resolved-state changes (:meth:`set_done`). :meth:`build` returns the
bytes of every part that changes or has to be created, together with the matching
``[Content_Types].xml`` overrides and ``word/_rels/document.xml.rels`` relationships.
Existing parts are edited as text: new elements are inserted before the closing root
tag and a ``w15:done`` flag is rewritten in place, so everything else, namespace
declarations under ``mc:Ignorable`` included, stays byte for byte.

Ids
    A comment's ``w:id`` is chosen by the caller (above every id in the story parts).
    ``paraId`` and ``durableId`` are 8 hex digits below ``0x80000000``, derived from the
    package hash and a counter, so the same document and request always give the same
    ids; they are unique among every ``paraId`` of every part and every durable id.

Replies
    ``paraIdParent`` always names the thread's root comment, as Word writes it. A thread
    is resolved by setting ``done`` on the root and on each reply.

A package that has ``comments.xml`` but lacks one of the other parts gets it, filled for
the comments that carry a ``paraId`` (``commentsExtensible`` copies the durable ids of
``commentsIds`` when that exists). A comment without a ``paraId`` that a reply or a
resolve names gets one added to its last paragraph.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Final
from xml.sax.saxutils import escape, quoteattr

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.parts import (
    DOCUMENT_RELS,
    MS_REL_2011,
    OFFICE_REL,
    W14,
    W15,
    W,
    related_part,
)
from doc_control.ooxml.zipio import Package

NS: Final[dict[str, str]] = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "w16cid": "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "w16cex": "http://schemas.microsoft.com/office/word/2018/wordml/cex",
}
W16CID = f"{{{NS['w16cid']}}}"
W16CEX = f"{{{NS['w16cex']}}}"

COMMENTS = "word/comments.xml"
EXTENDED = "word/commentsExtended.xml"
IDS = "word/commentsIds.xml"
EXTENSIBLE = "word/commentsExtensible.xml"
PEOPLE = "word/people.xml"
CONTENT_TYPES = "[Content_Types].xml"

_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml."
_PART_TYPES: Final[dict[str, tuple[str, str]]] = {
    COMMENTS: (_CT + "comments+xml", OFFICE_REL + "comments"),
    EXTENDED: (_CT + "commentsExtended+xml", MS_REL_2011 + "commentsExtended"),
    IDS: (
        _CT + "commentsIds+xml",
        "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
    ),
    EXTENSIBLE: (
        _CT + "commentsExtensible+xml",
        "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
    ),
    PEOPLE: (_CT + "people+xml", MS_REL_2011 + "people"),
}
_ROOTS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    COMMENTS: ("w:comments", ("w", "r", "mc", "w14", "w15", "wp14")),
    EXTENDED: ("w15:commentsEx", ("w", "r", "mc", "w14", "w15", "wp14")),
    IDS: ("w16cid:commentsIds", ("mc", "w14", "w15", "wp14", "w16cid")),
    EXTENSIBLE: ("w16cex:commentsExtensible", ("mc", "w14", "w15", "wp14", "w16cex")),
    PEOPLE: ("w15:people", ("w", "r", "mc", "w14", "w15", "wp14")),
}
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_ATTRS = r"""(?:"[^"]*"|'[^']*'|[^>"'])*"""
_HEX8 = re.compile(r"^[0-9A-F]{8}$")
_PARA_ID = re.compile(r"""\bparaId\s*=\s*["']([0-9A-Fa-f]{8})["']""")
_DURABLE_ID = re.compile(r"""\bdurableId\s*=\s*["']([0-9A-Fa-f]{8})["']""")
_REL_ID = re.compile(r"""\bId\s*=\s*["']rId(\d+)["']""")
_COMMENT_STYLES = ("CommentText", "CommentReference")
TEXT_ID: Final = "77777777"


@dataclass(frozen=True)
class ExistingComment:
    """A comment of ``comments.xml`` with what the other parts say about it."""

    id: str
    author: str
    date: str
    para_id: str | None  # of the last paragraph
    parent_para_id: str | None
    done: bool


@dataclass(frozen=True)
class NewComment:
    id: str
    para_id: str
    durable_id: str
    author: str
    date: str
    parent_id: str | None  # the thread's root comment, for a reply


def initials_of(author: str) -> str:
    """Up to three capital letters, one per word of ``author`` (``Claude`` gives ``C``)."""
    letters = [word[0].upper() for word in author.split() if word[0].isalnum()]
    return "".join(letters[:3]) or author[:1].upper()


def _decode(name: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT, f"{name} is not UTF-8 text", part=name
        ) from exc


def _parse(name: str, text: str) -> ET.Element:
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise DocControlError(
            ErrorCode.NOT_A_DOCX, f"{name} is not well-formed XML: {exc}", part=name
        ) from exc


def _root_tag(text: str) -> re.Match[str] | None:
    """The start tag of the root element."""
    return re.search(rf"<(?![?!/])([^\s/>]+)({_ATTRS})(/?)>", text)


def _declared(root_tag: str) -> dict[str, str]:
    """``prefix -> uri`` for every ``xmlns:prefix`` on the root start tag."""
    return {
        m.group(1): m.group(2) or m.group(3)
        for m in re.finditer(r"""xmlns:([\w.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""", root_tag)
    }


class _Syntax:
    """Prefixes for writing into an existing part: its own where declared, else local ones."""

    def __init__(self, text: str) -> None:
        match = _root_tag(text)
        self.bound = _declared(match.group(0)) if match else {}

    def prefix(self, wanted: str) -> tuple[str, str]:
        """``(prefix, xmlns declaration to add on the element or '')`` for namespace ``wanted``."""
        uri = NS[wanted]
        for prefix, bound in self.bound.items():
            if bound == uri:
                return prefix, ""
        if self.bound.get(wanted, uri) == uri:
            return wanted, f" xmlns:{wanted}={quoteattr(uri)}"
        return wanted + "x", f" xmlns:{wanted}x={quoteattr(uri)}"


def _insert_before_close(name: str, text: str, fragment: str) -> str:
    """``text`` with ``fragment`` as the last child of its root element."""
    match = _root_tag(text)
    if match is None:
        raise DocControlError(ErrorCode.NOT_A_DOCX, f"{name} has no root element", part=name)
    if match.group(3):  # <root .../>
        start = match.group(0)
        opened = start[:-2].rstrip() + ">"
        return (
            text[: match.start()] + opened + fragment + f"</{match.group(1)}>" + text[match.end() :]
        )
    close = text.rfind("</")
    if close < match.end():
        raise DocControlError(ErrorCode.NOT_A_DOCX, f"{name} has no closing tag", part=name)
    return text[:close] + fragment + text[close:]


class CommentParts:
    """Read the comment parts of ``pkg`` and plan additions to all of them."""

    def __init__(self, pkg: Package, *, styles: frozenset[str] = frozenset()) -> None:
        self.pkg = pkg
        self._styles = styles
        self._seed = pkg.sha256
        self._counter = 0
        self.names: dict[str, str | None] = {}
        self._text: dict[str, str] = {}
        for default, (_, rel) in _PART_TYPES.items():
            name = related_part(pkg, rel, default)
            self.names[default] = name
            if name is not None:
                self._text[default] = _decode(name, pkg.read(name))
        self._new_text: dict[str, str] = {}  # additions: part -> text; set after the first edit
        self._used_para = self._scan_ids(_PARA_ID)
        self._used_durable = self._scan_ids(_DURABLE_ID)
        self.existing = self._load()
        self.added: list[NewComment] = []
        self._added_xml: dict[str, str] = {}  # comment id -> its w:comment element
        self._done: dict[str, bool] = {}  # paraId -> new done flag
        self._fresh: dict[str, str] = {}  # comment id -> paraId given to a legacy comment
        self._people: list[str] = []

    # -- reading ---------------------------------------------------------------

    def _scan_ids(self, pattern: re.Pattern[str]) -> set[str]:
        used: set[str] = set()
        for name in self.pkg.names:
            if name.startswith("word/") and name.endswith(".xml"):
                used.update(m.upper() for m in pattern.findall(_decode(name, self.pkg.read(name))))
        return used

    def _load(self) -> dict[str, ExistingComment]:
        text = self._text.get(COMMENTS)
        if text is None:
            return {}
        parent: dict[str, str | None] = {}
        done: dict[str, bool] = {}
        if EXTENDED in self._text:
            for ex in _parse(EXTENDED, self._text[EXTENDED]).iter(W15 + "commentEx"):
                pid = (ex.get(W15 + "paraId") or "").upper()
                if pid:
                    value = ex.get(W15 + "paraIdParent")
                    parent[pid] = value.upper() if value else None
                    done[pid] = (ex.get(W15 + "done") or "0").lower() in ("1", "true", "on")
        out: dict[str, ExistingComment] = {}
        for c in _parse(COMMENTS, text).findall(W + "comment"):
            cid = c.get(W + "id")
            if cid is None:
                continue
            paras = c.findall(W + "p")
            raw = paras[-1].get(W14 + "paraId") if paras else None
            last_pid = raw.upper() if raw else None
            out[cid] = ExistingComment(
                id=cid,
                author=c.get(W + "author") or "",
                date=c.get(W + "date") or "",
                para_id=last_pid,
                parent_para_id=parent.get(last_pid) if last_pid else None,
                done=done.get(last_pid, False) if last_pid else False,
            )
        return out

    def comment_ids(self) -> set[str]:
        return set(self.existing) | {c.id for c in self.added}

    def para_id_of(self, cid: str) -> str:
        if cid in self._fresh:
            return self._fresh[cid]
        for c in self.added:
            if c.id == cid:
                return c.para_id
        known = self.existing[cid].para_id
        if known:
            return known
        return self._give_para_id(cid)

    def root_of(self, cid: str) -> str:
        """The id of the root comment of ``cid``'s thread (``cid`` itself for a root)."""
        seen = {cid}
        current = cid
        while True:
            parent_pid = self._parent_para_id(current)
            if parent_pid is None:
                return current
            nxt = self._comment_with_para_id(parent_pid)
            if nxt is None or nxt in seen:
                return current
            seen.add(nxt)
            current = nxt

    def _parent_para_id(self, cid: str) -> str | None:
        for c in self.added:
            if c.id == cid:
                return self.para_id_of(c.parent_id) if c.parent_id else None
        return self.existing[cid].parent_para_id

    def _comment_with_para_id(self, pid: str) -> str | None:
        for new in self.added:
            if new.para_id == pid:
                return new.id
        for cid, old in self.existing.items():
            if old.para_id == pid:
                return cid
        return None

    def thread(self, root: str) -> list[str]:
        """``root`` and every comment that replies into its thread, in file order."""
        members = [root]
        for cid in [*self.existing, *(c.id for c in self.added)]:
            if cid != root and self.root_of(cid) == root:
                members.append(cid)
        return members

    # -- ids -------------------------------------------------------------------

    def _hex_id(self, used: set[str], label: str) -> str:
        n = 0
        while True:
            digest = hashlib.blake2s(f"{self._seed}:{label}:{self._counter}:{n}".encode()).digest()
            value = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
            candidate = f"{value:08X}"
            if value and candidate not in used:
                used.add(candidate)
                self._counter += 1
                return candidate
            n += 1

    # -- planning --------------------------------------------------------------

    def add(
        self, cid: str, text: str, author: str, date: str, parent: str | None = None
    ) -> NewComment:
        """Add comment ``cid``; ``parent`` is the comment replied to (its thread's root is used)."""
        if cid in self.comment_ids():
            raise DocControlError(ErrorCode.INTERNAL, f"comment id {cid} is already taken")
        root = self.root_of(parent) if parent is not None else None
        parent_pid = self.para_id_of(root) if root is not None else None
        lines = text.split("\n")
        para_ids = [self._hex_id(self._used_para, "para") for _ in lines]
        durable = self._hex_id(self._used_durable, "durable")
        self._added_xml[cid] = self._comment_xml(
            cid, lines, para_ids, author, date, initials_of(author)
        )
        comment = NewComment(cid, para_ids[-1], durable, author, date, root)
        self.added.append(comment)
        if author not in self._people:
            self._people.append(author)
        if parent_pid is None:
            return comment
        return comment

    def set_done(self, cid: str, done: bool) -> bool:
        """Set the resolved flag of ``cid``; True if that changes anything."""
        pid = self.para_id_of(cid)
        current = self._done.get(pid)
        if current is None:
            current = self._existing_done(cid)
        self._done[pid] = done
        return current != done

    def _existing_done(self, cid: str) -> bool:
        if cid in self.existing:
            return self.existing[cid].done
        return False

    def _comment_xml(
        self,
        cid: str,
        lines: list[str],
        para_ids: list[str],
        author: str,
        date: str,
        initials: str,
    ) -> str:
        syntax = _Syntax(self._text.get(COMMENTS, ""))
        w, w_decl = syntax.prefix("w")
        w14, w14_decl = syntax.prefix("w14")
        paras = []
        style = (
            f'<{w}:pPr><{w}:pStyle {w}:val="CommentText"/></{w}:pPr>'
            if ("CommentText" in self._styles)
            else ""
        )
        ref_style = (
            f'<{w}:rPr><{w}:rStyle {w}:val="CommentReference"/></{w}:rPr>'
            if "CommentReference" in self._styles
            else ""
        )
        for i, (line, pid) in enumerate(zip(lines, para_ids, strict=True)):
            anchor = f"<{w}:r>{ref_style}<{w}:annotationRef/></{w}:r>" if i == 0 else ""
            paras.append(
                f'<{w}:p {w14}:paraId="{pid}" {w14}:textId="{TEXT_ID}">{style}{anchor}'
                f"{_runs(w, line)}</{w}:p>"
            )
        return (
            f"<{w}:comment{w_decl}{w14_decl} {w}:id={quoteattr(cid)} {w}:author={quoteattr(author)}"
            f" {w}:date={quoteattr(date)} {w}:initials={quoteattr(initials)}>"
            + "".join(paras)
            + f"</{w}:comment>"
        )

    # -- legacy comments without a paraId ----------------------------------------

    def _give_para_id(self, cid: str) -> str:
        pid = self._hex_id(self._used_para, "legacy")
        self._fresh[cid] = pid
        return pid

    def _with_legacy_ids(self, text: str) -> str:
        syntax = _Syntax(text)
        w14, decl = syntax.prefix("w14")
        for cid, pid in self._fresh.items():
            start = re.search(
                rf"<(?P<p>[\w.-]+:)?comment(?=[\s>])(?=(?:{_ATTRS})\b[\w.-]+:id=[\"']{re.escape(cid)}[\"'])"
                rf"{_ATTRS}>",
                text,
            )
            if start is None:
                raise DocControlError(ErrorCode.INTERNAL, f"comment {cid} not found in text")
            prefix = start.group("p") or ""
            end = text.find(f"</{prefix}comment>", start.end())
            paras = list(re.finditer(rf"<{re.escape(prefix)}p(?=[\s>/]){_ATTRS}>", text[:end]))
            paras = [m for m in paras if m.start() >= start.end()]
            if not paras:
                raise DocControlError(
                    ErrorCode.UNSUPPORTED_CONTENT,
                    f"comment {cid} has no paragraph to identify",
                    comment=cid,
                )
            tag = paras[-1]
            rewritten = tag.group(0)[:-1].rstrip("/")
            slash = "/" if tag.group(0).endswith("/>") else ""
            rewritten += f'{decl} {w14}:paraId="{pid}"{slash}>'
            text = text[: tag.start()] + rewritten + text[tag.end() :]
        return text

    # -- building the parts -------------------------------------------------------

    def build(self) -> dict[str, bytes]:
        """The new bytes of every part that changes or is created, content types and rels too."""
        out: dict[str, str] = {}
        now_fresh = bool(self.added or self._done)
        if not now_fresh:
            return {}
        comments = self._text.get(COMMENTS)
        if comments is None:
            comments = _fresh_root(COMMENTS)
        if self._fresh:
            comments = self._with_legacy_ids(comments)
        if self._added_xml:
            comments = _insert_before_close(COMMENTS, comments, "".join(self._added_xml.values()))
        out[COMMENTS] = comments
        out[EXTENDED] = self._extended()
        out[IDS], out[EXTENSIBLE] = self._ids_and_extensible()
        out[PEOPLE] = self._people_part()
        created = [name for name in out if self.names[name] is None]
        result = {
            (self.names[name] or name): text.encode("utf-8")
            for name, text in out.items()
            if text != self._text.get(name)
        }
        result.update(self._package_parts(created))
        return result

    def changed_parts(self) -> list[str]:
        return [self.names[n] or n for n in _PART_TYPES]

    def _existing_with_ids(self) -> list[ExistingComment]:
        return [c for c in self.existing.values() if c.para_id or c.id in self._fresh]

    def _extended(self) -> str:
        text = self._text.get(EXTENDED)
        entries: list[str] = []
        if text is None:
            text = _fresh_root(EXTENDED)
            for c in self._existing_with_ids():
                pid = c.para_id or self._fresh[c.id]
                entries.append(
                    _commentex(_prefixes(text), pid, c.parent_para_id, self._done.get(pid, c.done))
                )
        else:
            known = {m.upper() for m in _PARA_ID.findall(text)}
            for pid, flag in self._done.items():
                if pid in known:
                    text = _set_done(text, pid, flag)
            needed = set(self._done) | {
                self.para_id_of(n.parent_id) for n in self.added if n.parent_id
            }
            for c in self._existing_with_ids():
                pid = c.para_id or self._fresh[c.id]
                if (
                    pid not in known and pid in needed
                ):  # a reply or resolve names a comment the part lacks
                    entries.append(
                        _commentex(
                            _prefixes(text), pid, c.parent_para_id, self._done.get(pid, c.done)
                        )
                    )
                    known.add(pid)
            for pid, flag in self._done.items():
                if pid not in known and pid not in {c.para_id for c in self.added}:
                    entries.append(_commentex(_prefixes(text), pid, None, flag))
                    known.add(pid)
        syntax = _prefixes(text)
        for new in self.added:
            parent = self.para_id_of(new.parent_id) if new.parent_id else None
            entries.append(
                _commentex(syntax, new.para_id, parent, self._done.get(new.para_id, False))
            )
        return _insert_before_close(EXTENDED, text, "".join(entries)) if entries else text

    def _ids_and_extensible(self) -> tuple[str, str]:
        ids_text = self._text.get(IDS)
        ex_text = self._text.get(EXTENSIBLE)
        ids_entries: list[str] = []
        ex_entries: list[str] = []
        if ids_text is None:
            ids_text = _fresh_root(IDS)
        if ex_text is None:
            ex_text = _fresh_root(EXTENSIBLE)
        ids_syntax, ex_syntax = _prefixes(ids_text), _prefixes(ex_text)
        durable_of = _durable_map(ids_text)
        have_ids = IDS in self._text
        have_ex = EXTENSIBLE in self._text
        for c in self._existing_with_ids():  # backfill what a freshly created part lacks
            pid = c.para_id or self._fresh[c.id]
            if have_ids and have_ex:
                break
            if not have_ids and not have_ex:
                durable = self._hex_id(self._used_durable, "backfill")
                ids_entries.append(_comment_id(ids_syntax, pid, durable))
                ex_entries.append(_extensible(ex_syntax, durable, c.date))
            elif have_ids and not have_ex and pid.upper() in durable_of:
                ex_entries.append(_extensible(ex_syntax, durable_of[pid.upper()], c.date))
        for new in self.added:
            ids_entries.append(_comment_id(ids_syntax, new.para_id, new.durable_id))
            ex_entries.append(_extensible(ex_syntax, new.durable_id, new.date))
        if ids_entries:
            ids_text = _insert_before_close(IDS, ids_text, "".join(ids_entries))
        if ex_entries:
            ex_text = _insert_before_close(EXTENSIBLE, ex_text, "".join(ex_entries))
        return ids_text, ex_text

    def _people_part(self) -> str:
        text = self._text.get(PEOPLE) or _fresh_root(PEOPLE)
        syntax = _prefixes(text)
        known = {p.get(W15 + "author") for p in _parse(PEOPLE, text).iter(W15 + "person")}
        entries = []
        for author in self._people:
            if author in known:
                continue
            known.add(author)
            w15 = syntax.prefix("w15")
            entries.append(
                f"<{w15[0]}:person{w15[1]} {w15[0]}:author={quoteattr(author)}>"
                f'<{w15[0]}:presenceInfo {w15[0]}:providerId="None" '
                f"{w15[0]}:userId={quoteattr(author)}/></{w15[0]}:person>"
            )
        return _insert_before_close(PEOPLE, text, "".join(entries)) if entries else text

    def _package_parts(self, created: list[str]) -> dict[str, bytes]:
        """Content-type overrides and relationships for the parts being created."""
        if not created:
            return {}
        types = _decode(CONTENT_TYPES, self.pkg.read(CONTENT_TYPES))
        try:
            declared = {
                (o.get("PartName") or "").lstrip("/").lower()
                for o in ET.fromstring(types.encode("utf-8"))
                if o.tag.endswith("Override")
            }
        except ET.ParseError as exc:
            raise DocControlError(
                ErrorCode.NOT_A_DOCX, f"{CONTENT_TYPES} is not well-formed XML: {exc}"
            ) from exc
        rels_text = (
            _decode(DOCUMENT_RELS, self.pkg.read(DOCUMENT_RELS))
            if DOCUMENT_RELS in self.pkg
            else (
                _XML_DECL
                + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                + 'relationships"></Relationships>'
            )
        )
        next_rid = max((int(m) for m in _REL_ID.findall(rels_text)), default=0) + 1
        overrides = []
        rels = []
        for name in created:
            if name.lower() not in declared:
                overrides.append(
                    f'<Override PartName="/{name}" ContentType="{_PART_TYPES[name][0]}"/>'
                )
            target = posixpath.relpath(name, "word")
            rels.append(
                f'<Relationship Id="rId{next_rid}" Type="{_PART_TYPES[name][1]}" '
                f"Target={quoteattr(target)}/>"
            )
            next_rid += 1
        out: dict[str, bytes] = {}
        if overrides:
            out[CONTENT_TYPES] = _insert_before_close(
                CONTENT_TYPES, types, "".join(overrides)
            ).encode("utf-8")
        out[DOCUMENT_RELS] = _insert_before_close(DOCUMENT_RELS, rels_text, "".join(rels)).encode(
            "utf-8"
        )
        return out


# -- text helpers ----------------------------------------------------------------------


def _runs(w: str, line: str) -> str:
    out = []
    for part in re.split(r"(\t)", line):
        if part == "\t":
            out.append(f"<{w}:r><{w}:tab/></{w}:r>")
        elif part:
            out.append(f'<{w}:r><{w}:t xml:space="preserve">{escape(part)}</{w}:t></{w}:r>')
    return "".join(out)


def _fresh_root(part: str) -> str:
    tag, prefixes = _ROOTS[part]
    decls = "".join(f" xmlns:{p}={quoteattr(NS[p])}" for p in prefixes)
    ignorable = ' mc:Ignorable="w14 w15 wp14"'
    return f"{_XML_DECL}<{tag}{decls}{ignorable}></{tag.split(':')[0]}:{tag.split(':')[1]}>"


def _prefixes(text: str) -> _Syntax:
    return _Syntax(text)


def _commentex(syntax: _Syntax, pid: str, parent: str | None, done: bool) -> str:
    p, decl = syntax.prefix("w15")
    link = f' {p}:paraIdParent="{parent}"' if parent else ""
    return f'<{p}:commentEx{decl} {p}:paraId="{pid}"{link} {p}:done="{int(done)}"/>'


def _comment_id(syntax: _Syntax, pid: str, durable: str) -> str:
    p, decl = syntax.prefix("w16cid")
    return f'<{p}:commentId{decl} {p}:paraId="{pid}" {p}:durableId="{durable}"/>'


def _extensible(syntax: _Syntax, durable: str, date: str) -> str:
    p, decl = syntax.prefix("w16cex")
    when = f" {p}:dateUtc={quoteattr(date)}" if date else ""
    return f'<{p}:commentExtensible{decl} {p}:durableId="{durable}"{when}/>'


def _durable_map(ids_text: str) -> dict[str, str]:
    if not ids_text or "commentId" not in ids_text:
        return {}
    root = ET.fromstring(ids_text.encode("utf-8"))
    return {
        (e.get(W16CID + "paraId") or "").upper(): (e.get(W16CID + "durableId") or "").upper()
        for e in root.iter(W16CID + "commentId")
    }


def _set_done(text: str, pid: str, done: bool) -> str:
    """Rewrite ``w15:done`` on the ``commentEx`` element whose ``paraId`` is ``pid``."""
    for m in re.finditer(rf"<(?:[\w.-]+:)?commentEx(?=[\s/>]){_ATTRS}>", text):
        tag = m.group(0)
        found = _PARA_ID.search(tag)
        if found is None or found.group(1).upper() != pid:
            continue
        prefix = re.search(r"([\w.-]+):paraId", tag)
        name = f"{prefix.group(1)}:done" if prefix else "done"
        value = f'{name}="{int(done)}"'
        if re.search(rf"""\b{re.escape(name)}\s*=\s*(?:"[^"]*"|'[^']*')""", tag):
            new = re.sub(rf"""\b{re.escape(name)}\s*=\s*(?:"[^"]*"|'[^']*')""", value, tag, count=1)
        else:
            closing = "/>" if tag.endswith("/>") else ">"
            new = tag[: -len(closing)].rstrip() + f" {value}{closing}"
        return text[: m.start()] + new + text[m.end() :]
    return text
