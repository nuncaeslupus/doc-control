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

Limits
    The part must be UTF-8 (Word always writes UTF-8) and must not contain a
    DOCTYPE. Malformed markup raises ``DocControlError(NOT_A_DOCX)``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from doc_control.errors import DocControlError, ErrorCode

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"

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
_CHAR_REF = re.compile(r"&(#x[0-9a-fA-F]+|#[0-9]+|lt|gt|amp|quot|apos);")
_ENTITIES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}


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
    lands when the body has no paragraph yet).
    """

    data: bytes = field(repr=False)
    paragraphs: tuple[ParagraphSpan, ...]
    root_start: int
    root_end: int
    root_scope: Scope
    body_content: int | None
    known_prefixes: Mapping[str, str]  # uri -> first prefix declared for it anywhere

    # -- reading ---------------------------------------------------------

    def paragraph_bytes(self, index: int) -> bytes:
        p = self.paragraphs[index]
        return self.data[p.start : p.end]

    def parse_paragraph(self, index: int) -> ET.Element:
        """Parse paragraph ``index`` into an ElementTree element.

        Comments and processing instructions inside it are kept as
        ``ET.Comment``/``ET.ProcessingInstruction`` nodes so that
        :meth:`serialize` writes them back. The element's tail is empty.
        """
        p = self.paragraphs[index]
        decls = "".join(
            f' xmlns{":" + k if k else ""}="{_escape_attr(v)}"'
            for k, v in p.outer_scope.items()
            if k != "xml"
        )
        wrapped = (
            f"<doc-control-wrapper{decls}>".encode()
            + self.data[p.start : p.end]
            + b"</doc-control-wrapper>"
        )
        root = _parse_with_comments(wrapped)
        elem = root[0]
        elem.tail = None
        return elem

    # -- writing ---------------------------------------------------------

    def serialize(self, elem: ET.Element, at: int | None = None) -> bytes:
        """Serialise ``elem`` for splicing at paragraph ``at``.

        With ``at`` the element is written for the scope of that paragraph
        (its own ``xmlns`` declarations are written back on the start tag);
        with ``at=None`` (a paragraph for the body level) the root's scope is
        used. Namespaces already in scope keep the document's prefixes. A
        namespace that is not in scope is declared on ``elem`` itself, under
        the prefix the document uses for it elsewhere when that is free (``w``
        for WordprocessingML), otherwise ``ns0``, ``ns1``…
        """
        if at is None:
            return serialize_element(elem, self.root_scope, (), self.known_prefixes)
        p = self.paragraphs[at]
        return serialize_element(elem, p.outer_scope, p.own_decls, self.known_prefixes)

    def splice(
        self,
        replacements: Mapping[int, bytes] | None = None,
        *,
        insert_after: Mapping[int, Sequence[bytes]] | None = None,
        delete: Iterable[int] = (),
        check: bool = True,
    ) -> bytes:
        """Return the document with paragraph operations applied.

        - ``replacements[i]`` replaces paragraph ``i``'s bytes (``b""`` deletes it;
          several paragraphs in one value insert extras in its place).
        - ``insert_after[i]`` inserts the given paragraphs right after paragraph
          ``i`` (after its replacement, if any; also allowed when ``i`` is
          deleted). Key ``-1`` inserts before the first body paragraph, or at
          the start of the body when it has none.
        - ``delete`` removes paragraphs.

        Indices are those of :attr:`paragraphs` in this (original) document.
        Every byte outside the touched spans is copied unchanged. Operations
        that overlap — the same index replaced and deleted, or a nested
        paragraph touched together with its enclosing one — raise
        ``BATCH_CONFLICT``. An unknown index raises ``IndexError``. With
        ``check`` (the default) the result must parse with ElementTree, or
        ``INTERNAL`` is raised and nothing is returned.
        """
        replacements = dict(replacements or {})
        inserts = dict(insert_after or {})
        deletes = set(delete)
        n = len(self.paragraphs)
        for i in [*replacements, *deletes, *inserts]:
            if not (0 <= i < n or (i == -1 and i in inserts)):
                raise IndexError(f"no paragraph {i} (document has {n})")
        both = deletes & replacements.keys()
        if both:
            raise _conflict(f"paragraphs both replaced and deleted: {sorted(both)}")
        touched = deletes | replacements.keys()
        for i in sorted(touched | inserts.keys()):
            if i < 0:
                continue
            anc = self.paragraphs[i].parent
            while anc is not None:
                if anc in touched:
                    raise _conflict(f"paragraph {i} lies inside paragraph {anc}, also edited")
                anc = self.paragraphs[anc].parent

        # (start, end, new bytes), applied in offset order. Spans are disjoint,
        # so the only ties are a zero-width insertion at the offset where a
        # replaced paragraph starts (the previous paragraph's end, or ``-1``):
        # sorting by (start, end) puts the insertion first, i.e. before it.
        ops: list[tuple[int, int, bytes]] = []
        for i in touched:
            p = self.paragraphs[i]
            ops.append((p.start, p.end, replacements.get(i, b"")))
        for i, paras in inserts.items():
            if i == -1:
                pos = self.paragraphs[0].start if n else self.body_content
                if pos is None:
                    raise DocControlError(ErrorCode.UNSUPPORTED_CONTENT, "document has no w:body")
            else:
                pos = self.paragraphs[i].end
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


def splice(
    original: bytes,
    replacements: Mapping[int, bytes] | None = None,
    *,
    insert_after: Mapping[int, Sequence[bytes]] | None = None,
    delete: Iterable[int] = (),
    check: bool = True,
) -> bytes:
    """Scan ``original`` and apply :meth:`DocumentSpans.splice` to it."""
    return scan(original).splice(
        replacements, insert_after=insert_after, delete=delete, check=check
    )


# ---------------------------------------------------------------------------
# Tokenizer


def _malformed(msg: str, pos: int) -> DocControlError:
    return DocControlError(
        ErrorCode.NOT_A_DOCX, f"word/document.xml: {msg} at byte {pos}", offset=pos
    )


def _conflict(msg: str) -> DocControlError:
    return DocControlError(ErrorCode.BATCH_CONFLICT, msg)


def _unescape(raw: str) -> str:
    def sub(m: re.Match[str]) -> str:
        ref = m.group(1)
        if ref.startswith("#x"):
            return chr(int(ref[2:], 16))
        if ref.startswith("#"):
            return chr(int(ref[1:]))
        return _ENTITIES[ref]

    return _CHAR_REF.sub(sub, raw)


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
                uri = _unescape(aval.decode("utf-8"))
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
    )


# ---------------------------------------------------------------------------
# Serialiser


def _parse_with_comments(data: bytes) -> ET.Element:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    parser.feed(data)
    return parser.close()


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
    original start tag had them). Element and attribute names use the prefix
    already bound to their namespace; namespaces not in scope are declared on
    ``elem``, preferring ``known_prefixes[uri]`` when that prefix is free.
    Raises ``ValueError`` for a no-namespace element while a default namespace
    is in effect, which cannot be written without redefining it.
    """
    scope = dict(outer_scope)
    scope.update(own_decls)
    scope.setdefault("xml", XML_NS)
    decls: list[tuple[str, str]] = list(own_decls)
    elem_prefix: dict[str, str] = {}
    attr_prefix: dict[str, str] = {}
    for prefix, uri in scope.items():  # later (inner) bindings win
        elem_prefix[uri] = prefix
        if prefix:
            attr_prefix[uri] = prefix
    for prefix, uri in scope.items():  # prefer a named prefix for elements too
        if prefix and elem_prefix.get(uri) == "":
            elem_prefix[uri] = prefix
    known = known_prefixes or {}

    def declare(uri: str) -> str:
        want = known.get(uri) or _CONVENTIONAL_PREFIXES.get(uri)
        if want and want not in scope:
            prefix = want
        else:
            k = 0
            while f"ns{k}" in scope:
                k += 1
            prefix = f"ns{k}"
        scope[prefix] = uri
        decls.append((prefix, uri))
        elem_prefix.setdefault(uri, prefix)
        attr_prefix[uri] = prefix
        return prefix

    # First pass: bind every namespace the subtree uses.
    for node in elem.iter():
        if not isinstance(node.tag, str):
            continue
        uri, _ = _split(node.tag)
        if uri and uri not in elem_prefix:
            declare(uri)
        if not uri and scope.get(""):
            raise ValueError(f"no-namespace element {node.tag!r} inside a default namespace")
        for name in node.attrib:
            auri, _ = _split(name)
            if auri and auri not in attr_prefix:
                declare(auri)

    def qname(name: str, table: Mapping[str, str]) -> str:
        uri, local = _split(name)
        if not uri:
            return local
        prefix = table[uri]
        return f"{prefix}:{local}" if prefix else local

    parts: list[str] = []

    def write(node: ET.Element, extra: Sequence[tuple[str, str]]) -> None:
        if node.tag is ET.Comment:
            parts.append(f"<!--{node.text or ''}-->")
        elif node.tag is ET.ProcessingInstruction:
            parts.append(f"<?{node.text or ''}?>")
        else:
            assert isinstance(node.tag, str)
            tag = qname(node.tag, elem_prefix)
            parts.append(f"<{tag}")
            for prefix, uri in extra:
                parts.append(f' xmlns{":" + prefix if prefix else ""}="{_escape_attr(uri)}"')
            for name, value in node.attrib.items():
                parts.append(f' {qname(name, attr_prefix)}="{_escape_attr(value)}"')
            if len(node) == 0 and not node.text:
                parts.append("/>")
            else:
                parts.append(">")
                if node.text:
                    parts.append(_escape_text(node.text))
                for child in node:
                    write(child, ())
                parts.append(f"</{tag}>")
        if node is not elem and node.tail:
            parts.append(_escape_text(node.tail))

    write(elem, decls)
    return "".join(parts).encode("utf-8")
