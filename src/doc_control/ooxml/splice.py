"""Paragraph byte spans and the splice writer for ``word/document.xml``.

Writing never re-serialises a whole part. :func:`scan` finds the exact byte
span ``[start, end)`` of every ``w:p`` in the main body, and :func:`splice`
replaces, deletes or inserts paragraphs at those spans, copying every other
byte unchanged: the XML declaration, the root start tag with its namespace
declarations and ``mc:Ignorable`` list, and all untouched paragraphs.

A caller edits one paragraph with ElementTree::

    spans = scan(data)
    elem = spans.parse_paragraph(i)          # an ET element, comments kept
    ...edit elem...
    out = spans.splice({i: spans.serialize(elem, i)})

:meth:`DocumentSpans.serialize` writes the element with the document's own
prefixes (``w:``, ``w14:``…), never ElementTree's ``ns0:``.

Namespace declarations inside a paragraph
    ElementTree forgets ``xmlns`` declarations and prefixes, yet Markup
    Compatibility attributes name prefixes in their values
    (``mc:Ignorable="w14"``, ``mc:Choice Requires="wps"``, ``mc:ProcessContent``,
    ``mc:PreserveElements``, ``mc:PreserveAttributes``, ``mc:MustUnderstand``).
    :meth:`DocumentSpans.parse_paragraph` therefore records, for every element
    it builds, the prefix it was written with, the declarations on its start
    tag and the prefixes of its attributes, and :func:`serialize_element` writes
    them back on the same elements. (The record lives in a weak side table
    keyed by element; an element copied with ``copy.deepcopy`` loses it and is
    written with prefixes from the scope.) Every serialised paragraph, and with
    ``check`` every paragraph handed to :meth:`DocumentSpans.splice`, is then
    verified: each prefix named by such an attribute must be declared where it
    is used, or ``DocControlError(INTERNAL)`` is raised and nothing is written.

How paragraphs are found
    Only elements named ``p`` in the WordprocessingML namespace count, resolved
    through the ``xmlns`` declarations in scope, so any prefix works (including
    the default namespace and local re-declarations), and a ``p`` from another
    namespace does not. Only paragraphs inside ``w:body`` are reported, in
    document order: direct children, table cells, ``w:sdt`` content and any
    other container. A paragraph inside another paragraph (a text box's
    ``w:txbxContent``) is reported too, with ``depth > 0`` and ``parent`` set
    to the index of the enclosing paragraph; its span lies inside the parent's.
    Comments, CDATA sections and processing instructions are skipped whole, and
    quoted attribute values may contain ``>``.

What a splice accepts
    Each replacement (other than ``b""``) and each insertion must be exactly one
    ``w:p`` element, well-formed in the scope where it lands, with nothing
    around it; anything else raises ``DocControlError(SCHEMA_INVALID)``, so a
    value cannot close the body or smuggle in other markup.

Limits
    The part must be UTF-8 (Word always writes UTF-8) and must not contain a
    DOCTYPE or entity references other than the five predefined ones (both
    raise ``DocControlError(UNSUPPORTED_CONTENT)``). Malformed markup, invalid
    UTF-8 and invalid character references raise
    ``DocControlError(NOT_A_DOCX)``.
"""

from __future__ import annotations

import re
import weakref
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from xml.parsers import expat

from doc_control.errors import DocControlError, ErrorCode

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"

#: prefix -> namespace URI; ``""`` is the default namespace.
Scope = Mapping[str, str]

_BASE_SCOPE: dict[str, str] = {"xml": XML_NS}
# Used when a namespace needs a named prefix the document never declared, e.g.
# a w:-attribute in a document that puts WordprocessingML in the default namespace.
_CONVENTIONAL_PREFIXES = {W_NS: "w"}
_NAME = rb"[^\s/>=<]+"
_START_NAME = re.compile(rb"<(" + _NAME + rb")")
_ATTR = re.compile(rb"\s+(" + _NAME + rb")\s*=\s*(\"[^\"<]*\"|'[^'<]*')")
_START_CLOSE = re.compile(rb"\s*(/?)>")
_END_TAG = re.compile(rb"</(" + _NAME + rb")\s*>")
_REF = re.compile(r"&(#x[0-9a-fA-F]{1,8}|#[0-9]{1,10}|[A-Za-z_][\w.-]*);")
_ENTITIES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
_WRAPPER = "doc-control-wrapper"
# MCE attributes whose values list prefixes, and those that list prefix:local names.
_MCE_PREFIX_LISTS = frozenset({"Ignorable", "MustUnderstand"})
_MCE_QNAME_LISTS = frozenset({"ProcessContent", "PreserveElements", "PreserveAttributes"})


@dataclass(frozen=True)
class ParagraphSpan:
    """One ``w:p`` element: ``data[start:end]`` is its exact markup.

    ``outer_scope`` is the namespace scope around the element; ``own_decls``
    holds the ``xmlns`` declarations written on its own start tag, in order.
    """

    index: int
    start: int
    end: int
    depth: int
    parent: int | None
    outer_scope: Scope
    own_decls: tuple[tuple[str, str], ...]

    @property
    def nested(self) -> bool:
        """True for a paragraph inside another paragraph (e.g. in a text box)."""
        return self.depth > 0

    @property
    def scope(self) -> dict[str, str]:
        """The namespace scope inside the paragraph's start tag."""
        out = dict(self.outer_scope)
        out.update(self.own_decls)
        return out


@dataclass(frozen=True)
class DocumentSpans:
    """The paragraph spans of one ``word/document.xml``.

    ``root_start``/``root_end`` bound the root start tag; ``body_content``
    is the offset just after the ``w:body`` start tag (where an insertion
    with key ``-1`` lands) and ``body_scope`` the namespace scope there.
    """

    data: bytes = field(repr=False)
    paragraphs: tuple[ParagraphSpan, ...]
    root_start: int
    root_end: int
    root_scope: Scope
    body_content: int | None
    known_prefixes: Mapping[str, str]  # uri -> first prefix declared for it anywhere
    body_scope: Scope | None = None

    # -- reading ---------------------------------------------------------

    def paragraph_bytes(self, index: int) -> bytes:
        p = self.paragraphs[index]
        return self.data[p.start : p.end]

    def parse_paragraph(self, index: int) -> ET.Element:
        """Parse paragraph ``index`` into an ElementTree element.

        Comments and processing instructions inside it are kept as
        ``ET.Comment``/``ET.ProcessingInstruction`` nodes so that
        :meth:`serialize` writes them back. The element's tail is empty. Each
        element's original prefix and ``xmlns`` declarations are recorded for
        :meth:`serialize` (see the module docstring).

        Raises ``DocControlError``: ``UNSUPPORTED_CONTENT`` for an entity
        reference other than the predefined five, ``NOT_A_DOCX`` for any other
        malformed content (bad character reference, duplicate attribute…).
        """
        p = self.paragraphs[index]
        reader = _FragmentReader(p.outer_scope, build=True)
        try:
            root = reader.read(self.data[p.start : p.end])
        except expat.ExpatError as exc:
            if exc.code == expat.errors.codes[expat.errors.XML_ERROR_UNDEFINED_ENTITY]:
                raise DocControlError(
                    ErrorCode.UNSUPPORTED_CONTENT,
                    f"paragraph {index} uses an undefined entity: {exc}",
                ) from exc
            raise _malformed(f"paragraph {index}: {exc}", p.start) from exc
        except _BadFragment as exc:
            raise _malformed(f"paragraph {index}: {exc}", p.start) from exc
        assert root is not None
        elem = root[0]
        elem.tail = None
        return elem

    # -- writing ---------------------------------------------------------

    def serialize(self, elem: ET.Element, at: int | None = None) -> bytes:
        """Serialise ``elem`` for splicing at paragraph ``at``.

        With ``at`` the element is written for the scope of that paragraph
        (its own ``xmlns`` declarations are written back on the start tag);
        with ``at=None`` (a paragraph for the body level) the scope just inside
        ``w:body`` is used. Elements from :meth:`parse_paragraph` keep their
        original prefixes and declarations. Otherwise namespaces already in
        scope keep the document's prefixes, and a namespace that is not in
        scope is declared on ``elem`` itself, under the prefix the document uses
        for it elsewhere when that is free (``w`` for WordprocessingML),
        otherwise ``ns0``, ``ns1``… Raises ``DocControlError(INTERNAL)`` when
        the result would not be well-formed or would leave an MCE attribute
        naming an undeclared prefix.
        """
        if at is None:
            scope = self.body_scope if self.body_scope is not None else self.root_scope
            return serialize_element(elem, scope, (), self.known_prefixes)
        p = self.paragraphs[at]
        return serialize_element(elem, p.outer_scope, p.own_decls, self.known_prefixes)

    def splice(
        self,
        replacements: Mapping[int, bytes] | None = None,
        *,
        insert_after: Mapping[int, Sequence[bytes]] | None = None,
        delete: Iterable[int] = (),
        check: bool = True,
        insert_before: Mapping[int, Sequence[bytes]] | None = None,
    ) -> bytes:
        """Return the document with paragraph operations applied.

        - ``replacements[i]`` replaces paragraph ``i``'s bytes with one paragraph
          (``b""`` deletes it; use ``insert_after`` to add more).
        - ``insert_after[i]`` inserts the given paragraphs right after paragraph
          ``i`` (after its replacement, if any; also allowed when ``i`` is
          deleted). Key ``-1`` inserts at the very start of the body content,
          at body level, before any paragraph, table or ``w:sdt``.
        - ``insert_before[i]`` inserts the given paragraphs right before paragraph
          ``i`` (before its replacement, if any), in the container of ``i``. Where
          one offset is both the end of paragraph ``j`` and the start of ``i``,
          the ``insert_after[j]`` paragraphs come first.
        - ``delete`` removes paragraphs.

        Indices are those of :attr:`paragraphs` in this (original) document.
        Every byte outside the touched spans is copied unchanged. Every
        replacement and insertion must be exactly one ``w:p`` element that is
        well-formed in the scope where it lands, or ``SCHEMA_INVALID`` is
        raised. Operations that overlap — the same index replaced and deleted,
        or a nested paragraph touched together with its enclosing one — raise
        ``BATCH_CONFLICT``. An unknown index raises ``IndexError``, a non-int
        index (``bool`` included) ``TypeError``. With ``check`` (the default)
        every new paragraph must leave no MCE attribute naming an undeclared
        prefix and the result must parse with ElementTree, or ``INTERNAL`` is
        raised and nothing is returned.
        """
        replacements = dict(replacements or {})
        inserts = {i: list(paras) for i, paras in (insert_after or {}).items()}
        befores = {i: list(paras) for i, paras in (insert_before or {}).items()}
        deletes = set(delete)
        n = len(self.paragraphs)
        for i in [*replacements, *deletes, *inserts, *befores]:
            if isinstance(i, bool) or not isinstance(i, int):
                raise TypeError(f"paragraph index must be an int, not {type(i).__name__}")
            if not (0 <= i < n or (i == -1 and i in inserts and i not in befores)):
                raise IndexError(f"no paragraph {i} (document has {n})")

        both = deletes & replacements.keys()
        if both:
            raise _conflict(f"paragraphs both replaced and deleted: {sorted(both)}")
        touched = deletes | replacements.keys()
        for i in sorted(touched | inserts.keys() | befores.keys()):
            if i < 0:
                continue
            anc = self.paragraphs[i].parent
            while anc is not None:
                if anc in touched:
                    raise _conflict(f"paragraph {i} lies inside paragraph {anc}, also edited")
                anc = self.paragraphs[anc].parent

        mce_problems: list[str] = []
        for i, new in replacements.items():
            if new != b"":
                found = _validate_fragment(new, self.paragraphs[i].outer_scope)
                mce_problems += found or []
        for i, paras in inserts.items():
            scope = self._insert_scope(i)
            for new in paras:
                found = _validate_fragment(new, scope)
                mce_problems += found or []
        for i, paras in befores.items():
            for new in paras:
                found = _validate_fragment(new, self.paragraphs[i].outer_scope)
                mce_problems += found or []
        if check and mce_problems:
            raise DocControlError(
                ErrorCode.INTERNAL,
                "new paragraph names an undeclared prefix: " + "; ".join(mce_problems),
            )

        # (start, end, new bytes), applied in offset order. Spans are disjoint,
        # so the only ties are a zero-width insertion at the offset where a
        # replaced paragraph starts (the previous paragraph's end, or the body
        # content start for ``-1``): sorting by (start, end) puts the insertion
        # first, i.e. before it.
        ops: list[tuple[int, int, bytes]] = []
        for i in touched:
            p = self.paragraphs[i]
            ops.append((p.start, p.end, replacements.get(i, b"")))
        for i, paras in inserts.items():
            if i == -1:
                pos = self.body_content
                assert pos is not None  # _insert_scope refused a missing body
            else:
                pos = self.paragraphs[i].end
            ops.append((pos, pos, b"".join(paras)))
        for i, paras in befores.items():
            pos = self.paragraphs[i].start
            ops.append((pos, pos, b"".join(paras)))
        ops.sort(key=lambda op: (op[0], op[1]))

        out = bytearray()
        cursor = 0
        for start, end, new in ops:
            out += self.data[cursor:start]
            out += new
            cursor = end
        out += self.data[cursor:]
        result = bytes(out)
        if check and result != self.data:
            try:
                ET.fromstring(result)
            except ET.ParseError as exc:
                raise DocControlError(
                    ErrorCode.INTERNAL, f"spliced document is not well-formed: {exc}"
                ) from exc
        return result

    def _insert_scope(self, i: int) -> Scope:
        if i != -1:
            return self.paragraphs[i].outer_scope
        if self.body_content is None or self.body_scope is None:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT, "document has no w:body to insert into"
            )
        return self.body_scope


def splice(
    original: bytes,
    replacements: Mapping[int, bytes] | None = None,
    *,
    insert_after: Mapping[int, Sequence[bytes]] | None = None,
    delete: Iterable[int] = (),
    check: bool = True,
    insert_before: Mapping[int, Sequence[bytes]] | None = None,
) -> bytes:
    """Scan ``original`` and apply :meth:`DocumentSpans.splice` to it."""
    return scan(original).splice(
        replacements,
        insert_after=insert_after,
        delete=delete,
        check=check,
        insert_before=insert_before,
    )


# ---------------------------------------------------------------------------
# Tokenizer


def _malformed(msg: str, pos: int) -> DocControlError:
    return DocControlError(
        ErrorCode.NOT_A_DOCX, f"word/document.xml: {msg} at byte {pos}", offset=pos
    )


def _conflict(msg: str) -> DocControlError:
    return DocControlError(ErrorCode.BATCH_CONFLICT, msg)


def _is_xml_char(code: int) -> bool:
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _unescape(raw: str, pos: int) -> str:
    """Resolve the references in an attribute value, as an XML parser would."""

    def sub(m: re.Match[str]) -> str:
        ref = m.group(1)
        if ref.startswith("#"):
            code = int(ref[2:], 16) if ref.startswith("#x") else int(ref[1:])
            if not _is_xml_char(code):
                raise _malformed(f"invalid character reference &{ref};", pos)
            return chr(code)
        if ref in _ENTITIES:
            return _ENTITIES[ref]
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            f"word/document.xml uses the undefined entity &{ref}; at byte {pos}",
            offset=pos,
        )

    if "&" in _REF.sub("", raw):
        raise _malformed("'&' that starts no valid reference", pos)
    return _REF.sub(sub, raw)


def _resolve(qname: bytes, scope: Scope, pos: int) -> tuple[str, str]:
    name = qname.decode("utf-8")
    prefix, sep, local = name.rpartition(":")
    if sep and not prefix:
        raise _malformed(f"bad name {name!r}", pos)
    uri = scope.get(prefix) if prefix else scope.get("", "")
    if uri is None:
        raise _malformed(f"undeclared prefix {prefix!r}", pos)
    return uri, local


@dataclass
class _Open:
    qname: bytes
    scope: Scope
    para: int | None
    body: bool


def scan(data: bytes) -> DocumentSpans:
    """Tokenize ``data`` and return the span of every body paragraph."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise DocControlError(ErrorCode.UNSUPPORTED_CONTENT, "word/document.xml is not UTF-8")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _malformed("invalid UTF-8", exc.start) from exc
    starts: list[int] = []
    ends: list[int] = []
    depths: list[int] = []
    parents: list[int | None] = []
    outer_scopes: list[Scope] = []
    own: list[tuple[tuple[str, str], ...]] = []
    known: dict[str, str] = {}

    stack: list[_Open] = []
    para_stack: list[int] = []
    body_depth = 0
    root: tuple[int, int, Scope, str, str] | None = None
    body_content: int | None = None
    body_scope: Scope | None = None
    root_closed = False
    pos = 0
    n = len(data)
    while True:
        lt = data.find(b"<", pos)
        if lt < 0:
            break
        if data.startswith(b"<!--", lt):
            close = data.find(b"-->", lt + 4)
            if close < 0:
                raise _malformed("unterminated comment", lt)
            pos = close + 3
            continue
        if data.startswith(b"<![CDATA[", lt):
            close = data.find(b"]]>", lt + 9)
            if close < 0:
                raise _malformed("unterminated CDATA section", lt)
            pos = close + 3
            continue
        if data.startswith(b"<?", lt):
            close = data.find(b"?>", lt + 2)
            if close < 0:
                raise _malformed("unterminated processing instruction", lt)
            pos = close + 2
            continue
        if data.startswith(b"<!", lt):
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT, "word/document.xml has a DOCTYPE or declaration"
            )
        if data.startswith(b"</", lt):
            m = _END_TAG.match(data, lt)
            if m is None:
                raise _malformed("bad end tag", lt)
            if not stack or stack[-1].qname != m.group(1):
                raise _malformed(f"unexpected end tag {m.group(1)!r}", lt)
            top = stack.pop()
            pos = m.end()
            if top.para is not None:
                ends[top.para] = pos
                para_stack.pop()
            if top.body:
                body_depth -= 1
            if not stack:
                root_closed = True
            continue

        # Start tag.
        if root_closed:
            raise _malformed("content after the root element", lt)
        m = _START_NAME.match(data, lt)
        if m is None:
            raise _malformed("bad start tag", lt)
        qname = m.group(1)
        cur = m.end()
        attrs: list[tuple[bytes, bytes]] = []
        while True:
            am = _ATTR.match(data, cur)
            if am is None:
                break
            attrs.append((am.group(1), am.group(2)[1:-1]))
            cur = am.end()
        cm = _START_CLOSE.match(data, cur)
        if cm is None:
            raise _malformed("bad attribute or unterminated start tag", cur)
        end = cm.end()
        self_closing = bool(cm.group(1))

        parent_scope: Scope = stack[-1].scope if stack else _BASE_SCOPE
        decls: list[tuple[str, str]] = []
        for aname, aval in attrs:
            if aname == b"xmlns" or aname.startswith(b"xmlns:"):
                prefix = aname[6:].decode("utf-8")
                uri = _unescape(aval.decode("utf-8"), lt)
                if prefix and not uri:
                    raise _malformed(f"empty namespace for prefix {prefix!r}", lt)
                decls.append((prefix, uri))
                if prefix:
                    known.setdefault(uri, prefix)
        scope: Scope = parent_scope
        if decls:
            new_scope = dict(parent_scope)
            new_scope.update(decls)
            scope = new_scope
        uri, local = _resolve(qname, scope, lt)

        para: int | None = None
        is_body = False
        if root is None:
            root = (lt, end, scope, uri, local)
        elif uri == W_NS:
            if local == "body" and body_depth == 0 and len(stack) == 1:
                is_body = True
                body_content = end
                body_scope = scope
            elif local == "p" and body_depth > 0:
                para = len(starts)
                starts.append(lt)
                ends.append(end)
                depths.append(len(para_stack))
                parents.append(para_stack[-1] if para_stack else None)
                outer_scopes.append(parent_scope)
                own.append(tuple(decls))
        if self_closing:
            if is_body:
                body_content = None
            if not stack:
                root_closed = True
        else:
            stack.append(_Open(qname, scope, para, is_body))
            if para is not None:
                para_stack.append(para)
            if is_body:
                body_depth += 1
        pos = end

    if root is None:
        raise _malformed("no root element", 0)
    if stack:
        raise _malformed(f"unclosed element {stack[-1].qname!r}", n)
    root_start, root_end, root_scope, root_uri, root_local = root
    if (root_uri, root_local) != (W_NS, "document"):
        raise DocControlError(
            ErrorCode.NOT_A_DOCX, "word/document.xml root is not a WordprocessingML w:document"
        )
    paragraphs = tuple(
        ParagraphSpan(i, starts[i], ends[i], depths[i], parents[i], outer_scopes[i], own[i])
        for i in range(len(starts))
    )
    return DocumentSpans(
        data=data,
        paragraphs=paragraphs,
        root_start=root_start,
        root_end=root_end,
        root_scope=root_scope,
        body_content=body_content,
        known_prefixes=known,
        body_scope=body_scope,
    )


# ---------------------------------------------------------------------------
# Fragment reader: expat without namespace processing, so prefixes and
# declarations stay visible; names are resolved here, against the scope where
# the fragment sits.


@dataclass(frozen=True)
class _Origin:
    """How an element parsed by :meth:`DocumentSpans.parse_paragraph` was written."""

    prefix: str  # prefix of its name ("" when unprefixed)
    decls: tuple[tuple[str, str], ...]  # xmlns declarations on its start tag, in order
    attr_prefixes: Mapping[str, str]  # Clark attribute name -> prefix it was written with


_ORIGINS: weakref.WeakKeyDictionary[ET.Element, _Origin] = weakref.WeakKeyDictionary()


class _BadFragment(ValueError):
    """A fragment that is well-formed XML but not namespace-well-formed."""


def _resolve_name(qname: str, scope: Scope, *, element: bool) -> tuple[str, str, str]:
    """Return ``(uri, local, prefix)`` for a raw element or attribute name."""
    prefix, sep, local = qname.partition(":")
    if not sep:
        return (scope.get("", "") if element else ""), qname, ""
    if not prefix or not local or ":" in local:
        raise _BadFragment(f"bad name {qname!r}")
    uri = scope.get(prefix)
    if uri is None:
        raise _BadFragment(f"undeclared prefix {prefix!r}")
    return uri, local, prefix


def _clark(uri: str, local: str) -> str:
    return f"{{{uri}}}{local}" if uri else local


class _FragmentReader:
    """Reads markup that sits in ``scope``; optionally builds ElementTree nodes.

    ``top`` lists the resolved names of the fragment's top-level elements,
    ``stray`` is set by any text, comment or processing instruction beside them,
    and ``mce_problems`` describes every MCE attribute value that names a
    prefix not declared where it is used.
    """

    def __init__(self, scope: Scope, *, build: bool) -> None:
        self._scopes: list[Scope] = [{**_BASE_SCOPE, **scope}]
        self._tags: list[str] = []
        self._depth = 0
        self._builder = ET.TreeBuilder(insert_comments=True, insert_pis=True) if build else None
        self.top: list[tuple[str, str]] = []
        self.stray = False
        self.mce_problems: list[str] = []

    def read(self, fragment: bytes) -> ET.Element | None:
        parser = expat.ParserCreate()
        parser.ordered_attributes = True
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._data
        parser.CommentHandler = self._comment
        parser.ProcessingInstructionHandler = self._pi
        parser.Parse(f"<{_WRAPPER}>".encode(), False)
        parser.Parse(fragment, False)
        parser.Parse(f"</{_WRAPPER}>".encode(), True)
        return self._builder.close() if self._builder is not None else None

    def _start(self, name: str, flat: list[str]) -> None:
        if self._depth == 0:
            self._depth = 1
            if self._builder is not None:
                self._builder.start(_WRAPPER, {})
            return
        decls: list[tuple[str, str]] = []
        plain: list[tuple[str, str]] = []
        for k, v in zip(flat[::2], flat[1::2], strict=True):
            if k == "xmlns":
                decls.append(("", v))
            elif k.startswith("xmlns:"):
                prefix = k[6:]
                if not prefix or ":" in prefix or not v or prefix == "xmlns":
                    raise _BadFragment(f"bad namespace declaration {k}={v!r}")
                decls.append((prefix, v))
            else:
                plain.append((k, v))
        scope = self._scopes[-1]
        if decls:
            scope = {**scope, **dict(decls)}
        uri, local, prefix = _resolve_name(name, scope, element=True)
        attrib: dict[str, str] = {}
        attr_prefixes: dict[str, str] = {}
        for k, v in plain:
            auri, alocal, aprefix = _resolve_name(k, scope, element=False)
            key = _clark(auri, alocal)
            if key in attrib:
                raise _BadFragment(f"duplicate attribute {key!r} on {name!r}")
            attrib[key] = v
            if aprefix:
                attr_prefixes[key] = aprefix
            self._check_mce(name, uri, local, k, auri, alocal, v, scope)
        if self._depth == 1:
            self.top.append((uri, local))
        self._depth += 1
        self._scopes.append(scope)
        tag = _clark(uri, local)
        self._tags.append(tag)
        if self._builder is not None:
            elem = self._builder.start(tag, attrib)
            _ORIGINS[elem] = _Origin(prefix, tuple(decls), attr_prefixes)

    def _check_mce(
        self,
        elem_name: str,
        uri: str,
        local: str,
        attr_name: str,
        auri: str,
        alocal: str,
        value: str,
        scope: Scope,
    ) -> None:
        if auri == MC_NS and alocal in _MCE_PREFIX_LISTS:
            prefixes = value.split()
        elif auri == MC_NS and alocal in _MCE_QNAME_LISTS:
            prefixes = [tok.partition(":")[0] for tok in value.split() if ":" in tok]
        elif (uri, local, attr_name) == (MC_NS, "Choice", "Requires"):
            prefixes = value.split()
        else:
            return
        missing = [p for p in prefixes if p not in scope or p == ""]
        if missing:
            self.mce_problems.append(
                f"{attr_name}={value!r} on <{elem_name}> names undeclared {missing}"
            )

    def _end(self, name: str) -> None:
        self._depth -= 1
        if self._depth == 0:
            if self._builder is not None:
                self._builder.end(_WRAPPER)
            return
        self._scopes.pop()
        tag = self._tags.pop()
        if self._builder is not None:
            self._builder.end(tag)

    def _data(self, text: str) -> None:
        if self._depth == 1:
            self.stray = True
        if self._builder is not None:
            self._builder.data(text)

    def _comment(self, text: str) -> None:
        if self._depth == 1:
            self.stray = True
        if self._builder is not None:
            self._builder.comment(text)

    def _pi(self, target: str, data: str) -> None:
        if self._depth == 1:
            self.stray = True
        if self._builder is not None:
            self._builder.pi(target, data)


def _validate_fragment(fragment: bytes, scope: Scope) -> list[str]:
    """Check that ``fragment`` is exactly one ``w:p`` in ``scope``.

    Returns the MCE prefix problems found in it (empty when there are none);
    raises ``SCHEMA_INVALID`` when it is not a single well-formed paragraph.
    """
    if not isinstance(fragment, bytes | bytearray):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f"paragraph must be bytes, not {type(fragment).__name__}"
        )
    reader = _FragmentReader(scope, build=False)
    try:
        reader.read(bytes(fragment))
    except (expat.ExpatError, _BadFragment) as exc:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f"new paragraph is not well-formed: {exc}"
        ) from exc
    if reader.top != [(W_NS, "p")] or reader.stray:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            "each replacement or insertion must be exactly one w:p element and nothing else",
        )
    return reader.mce_problems


def _mce_problems(fragment: bytes, scope: Scope) -> list[str]:
    """Return the MCE attributes in ``fragment`` that name prefixes undeclared in scope.

    ``scope`` is the namespace scope where ``fragment`` sits. Raises
    ``DocControlError(INTERNAL)`` when the fragment is not well-formed there.
    """
    reader = _FragmentReader(scope, build=False)
    try:
        reader.read(fragment)
    except (expat.ExpatError, _BadFragment) as exc:
        raise DocControlError(
            ErrorCode.INTERNAL, f"serialised paragraph is not well-formed: {exc}"
        ) from exc
    return reader.mce_problems


# ---------------------------------------------------------------------------
# Serialiser


def _escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")


def _escape_attr(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\t", "&#9;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )


def _split(name: str) -> tuple[str, str]:
    if name.startswith("{"):
        uri, _, local = name[1:].partition("}")
        return uri, local
    return "", name


def serialize_element(
    elem: ET.Element,
    outer_scope: Scope,
    own_decls: Sequence[tuple[str, str]] = (),
    known_prefixes: Mapping[str, str] | None = None,
) -> bytes:
    """Serialise ``elem`` (and its subtree, without its tail) as UTF-8 bytes.

    ``outer_scope`` is the namespace scope at the place the bytes will go;
    ``own_decls`` are written as ``xmlns`` declarations on ``elem`` (as the
    original start tag had them). Elements built by
    :meth:`DocumentSpans.parse_paragraph` get their recorded declarations back
    on the same elements and keep their prefixes while those still resolve to
    the same namespace. Other names use a prefix already bound to their
    namespace; namespaces not in scope are declared on ``elem`` (or, when an
    inner declaration shadows that prefix, on the element that needs it),
    preferring ``known_prefixes[uri]`` when that prefix is free.

    Raises ``ValueError`` for a no-namespace element while a default namespace
    is in effect, which cannot be written without redefining it, and
    ``DocControlError(INTERNAL)`` when the output is not well-formed in
    ``outer_scope`` or leaves an MCE attribute naming an undeclared prefix.
    """
    known = known_prefixes or {}
    top_origin = _ORIGINS.get(elem)
    top_decls: list[tuple[str, str]] = list(own_decls)
    if top_origin is not None:
        have = {p for p, _ in top_decls}
        top_decls += [d for d in top_origin.decls if d[0] not in have]
    top_scope: dict[str, str] = {**_BASE_SCOPE, **outer_scope, **dict(top_decls)}
    generated: set[str] = set()

    def pick(uri: str, scope: Mapping[str, str], named: bool) -> str | None:
        """A prefix bound to ``uri``: the document's own named ones first."""
        found: str | None = None
        default = False
        for p, u in scope.items():
            if u != uri:
                continue
            if not p:
                default = True
            elif p not in generated or found is None:
                found = p
        if found is not None and found not in generated:
            return found
        if default and not named:
            return ""
        return found

    def fresh(uri: str, scope: Mapping[str, str]) -> str:
        want = known.get(uri) or _CONVENTIONAL_PREFIXES.get(uri)
        if want and want not in scope and want != "xmlns":
            return want
        k = 0
        while f"ns{k}" in scope:
            k += 1
        return f"ns{k}"

    # First pass: namespaces the subtree uses that nothing in scope binds get
    # declared on elem itself, as before.
    missing: list[str] = []

    def need(node: ET.Element, scope: Mapping[str, str]) -> None:
        if not isinstance(node.tag, str):
            return
        origin = _ORIGINS.get(node) if node is not elem else None
        if origin is not None and origin.decls:
            scope = {**scope, **dict(origin.decls)}
        uri, _ = _split(node.tag)
        if uri and pick(uri, scope, False) is None and uri not in missing:
            missing.append(uri)
        for name in node.attrib:
            auri, _ = _split(name)
            if auri and pick(auri, scope, True) is None and auri not in missing:
                missing.append(auri)
        for child in node:
            need(child, scope)

    need(elem, top_scope)
    for uri in missing:
        prefix = fresh(uri, top_scope)
        top_scope[prefix] = uri
        top_decls.append((prefix, uri))
        generated.add(prefix)

    parts: list[str] = []

    def write(node: ET.Element, scope: Mapping[str, str], decls: list[tuple[str, str]]) -> None:
        if node.tag is ET.Comment:
            parts.append(f"<!--{node.text or ''}-->")
        elif node.tag is ET.ProcessingInstruction:
            parts.append(f"<?{node.text or ''}?>")
        else:
            assert isinstance(node.tag, str)
            origin = _ORIGINS.get(node)
            here = dict(scope)

            def name_for(uri: str, local: str, kept: str | None, named: bool) -> str:
                if not uri:
                    return local
                if kept is not None and (kept or not named) and here.get(kept) == uri:
                    prefix: str | None = kept
                else:
                    prefix = pick(uri, here, named)
                if prefix is None:  # shadowed by an inner declaration
                    prefix = fresh(uri, here)
                    here[prefix] = uri
                    generated.add(prefix)
                    decls.append((prefix, uri))
                return f"{prefix}:{local}" if prefix else local

            euri, elocal = _split(node.tag)
            if not euri and here.get(""):
                raise ValueError(f"no-namespace element {node.tag!r} inside a default namespace")
            tag = name_for(euri, elocal, origin.prefix if origin else None, False)
            attrs: list[str] = []
            for name, value in node.attrib.items():
                auri, alocal = _split(name)
                kept = origin.attr_prefixes.get(name) if origin else None
                attrs.append(f' {name_for(auri, alocal, kept, True)}="{_escape_attr(value)}"')
            parts.append(f"<{tag}")
            for prefix, uri in decls:
                parts.append(f' xmlns{":" + prefix if prefix else ""}="{_escape_attr(uri)}"')
            parts.extend(attrs)
            if len(node) == 0 and not node.text:
                parts.append("/>")
            else:
                parts.append(">")
                if node.text:
                    parts.append(_escape_text(node.text))
                for child in node:
                    child_origin = _ORIGINS.get(child)
                    child_decls = list(child_origin.decls) if child_origin is not None else []
                    write(child, {**here, **dict(child_decls)}, child_decls)
                parts.append(f"</{tag}>")
        if node is not elem and node.tail:
            parts.append(_escape_text(node.tail))

    write(elem, top_scope, top_decls)
    out = "".join(parts).encode("utf-8")
    problems = _mce_problems(out, outer_scope)
    if problems:
        raise DocControlError(
            ErrorCode.INTERNAL,
            "serialised paragraph names an undeclared prefix: " + "; ".join(problems),
        )
    return out
