"""Header and footer text for ``layout.apply`` (LAYOUT T5).

A plan's header or footer value is one paragraph of text in which ``{PAGE}``, ``{NUMPAGES}``
and ``{chapter}`` stand for the fields ``PAGE``, ``NUMPAGES`` and ``STYLEREF "Heading 1"``.
:func:`rewrite_paragraph` turns an existing paragraph into that text as tracked changes (its
live content deleted, the new content inserted); :func:`new_part_bytes` makes a new header or
footer part holding it, and :func:`package_entries` the relationship and content-type entries
that make such a part reachable.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Sequence
from xml.sax.saxutils import quoteattr

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.revisions import (
    DEL,
    DELTEXT,
    INS,
    PPR,
    RPR,
    R,
    RevisionContext,
    T,
    W,
    text_children,
    unsupported,
)
from doc_control.ooxml.revisions import P as P_TAG
from doc_control.ooxml.splice import W_NS, Container, scan
from doc_control.ooxml.zipio import Package

CONTENT_TYPES = "[Content_Types].xml"
DOCUMENT_RELS = "word/_rels/document.xml.rels"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
#: group -> (root element, content type, relationship type)
GROUPS = {
    "header": (
        "hdr",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
        R_NS + "/header",
    ),
    "footer": (
        "ftr",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
        R_NS + "/footer",
    ),
}
_PLACEHOLDER = re.compile(r"\{(PAGE|NUMPAGES|chapter)\}")
_FIELDS = {"PAGE": " PAGE ", "NUMPAGES": " NUMPAGES ", "chapter": ' STYLEREF "Heading 1" '}
_REL_ID = re.compile(r"""\bId\s*=\s*["']rId(\d+)["']""")
_FLD_CHAR, _INSTR = W + "fldChar", W + "instrText"
#: Kept as they are when a paragraph's content is deleted: marks, not content.
_KEPT = frozenset(
    W + n
    for n in [
        "pPr",
        "del",
        "bookmarkStart",
        "bookmarkEnd",
        "proofErr",
        "commentRangeStart",
        "commentRangeEnd",
        "permStart",
        "permEnd",
    ]
)
_WRAPPERS = frozenset({INS, W + "hyperlink", W + "smartTag"})
_OBJECTS = frozenset({W + "drawing", W + "pict", W + "object"})


def root_name(data: bytes, part: str) -> str:
    """``hdr`` or ``ftr``: the root element of a header or footer part."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocControlError(
            ErrorCode.NOT_A_DOCX, f"{part} is not well-formed XML: {exc}"
        ) from exc
    name = root.tag.rsplit("}", 1)[-1]
    if root.tag != W + name or name not in ("hdr", "ftr"):
        raise unsupported(f"{part} is not a header or footer part", part=part)
    return name


# -- content -------------------------------------------------------------------------------


def _run(rpr: ET.Element | None, *children: ET.Element) -> ET.Element:
    run = ET.Element(R)
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    run.extend(children)
    return run


def _field(code: str, rpr: ET.Element | None) -> list[ET.Element]:
    instr = ET.Element(_INSTR, {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
    instr.text = _FIELDS[code]
    runs = [
        _run(rpr, ET.Element(_FLD_CHAR, {W + "fldCharType": "begin"})),
        _run(rpr, instr),
        _run(rpr, ET.Element(_FLD_CHAR, {W + "fldCharType": "separate"})),
    ]
    if code != "chapter":  # a placeholder result; the application updates it
        result = ET.Element(T)
        result.text = "1"
        runs.append(_run(rpr, result))
    runs.append(_run(rpr, ET.Element(_FLD_CHAR, {W + "fldCharType": "end"})))
    return runs


def content_runs(text: str, rpr: ET.Element | None) -> list[ET.Element]:
    """Runs reading as ``text``, its placeholders as fields."""
    runs: list[ET.Element] = []
    pos = 0
    for match in _PLACEHOLDER.finditer(text):
        if match.start() > pos:
            runs.append(_run(rpr, *text_children(text[pos : match.start()])))
        runs += _field(match.group(1), rpr)
        pos = match.end()
    if pos < len(text):
        runs.append(_run(rpr, *text_children(text[pos:])))
    return runs


def _clean_rpr(rpr: ET.Element | None) -> ET.Element | None:
    if rpr is None:
        return None
    clone = copy.deepcopy(rpr)
    for child in [c for c in clone if c.tag in (INS, DEL, W + "rPrChange")]:
        clone.remove(child)
    return clone


def first_rpr(p: ET.Element) -> ET.Element | None:
    """A copy of the formatting of the paragraph's first live run."""

    def search(parent: ET.Element | None) -> ET.Element | None:
        for node in parent if parent is not None else ():
            if node.tag == R:
                return _clean_rpr(node.find(RPR))
            if node.tag in _WRAPPERS or node.tag == W + "sdt":
                found = search(node.find(W + "sdtContent") if node.tag == W + "sdt" else node)
                if found is not None:
                    return found
        return None

    return search(p)


def _simple_to_runs(fld: ET.Element) -> list[ET.Element]:
    """A ``w:fldSimple`` as the complex field runs it stands for."""
    inner = [c for c in fld if c.tag == R]
    rpr = _clean_rpr(inner[0].find(RPR)) if inner else None
    instr = ET.Element(_INSTR, {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
    instr.text = fld.get(W + "instr", "")
    return [
        _run(rpr, ET.Element(_FLD_CHAR, {W + "fldCharType": "begin"})),
        _run(rpr, instr),
        _run(rpr, ET.Element(_FLD_CHAR, {W + "fldCharType": "separate"})),
        *inner,
        _run(rpr, ET.Element(_FLD_CHAR, {W + "fldCharType": "end"})),
    ]


def _deleted(run: ET.Element, ctx: RevisionContext) -> ET.Element:
    if any(node.tag in _OBJECTS for node in run.iter()):
        raise unsupported("the header or footer holds a picture or object; it is not rewritten")
    for node in run.iter():
        if node.tag == T:
            node.tag = DELTEXT
        elif node.tag == _INSTR:
            node.tag = W + "delInstrText"
    wrapper = ctx.revision("del")
    wrapper.append(run)
    return wrapper


def _delete_content(parent: ET.Element, ctx: RevisionContext) -> None:
    """Every live run under ``parent`` becomes a tracked deletion, in place."""
    k = 0
    while k < len(parent):
        node = parent[k]
        if not isinstance(node.tag, str) or node.tag in _KEPT:
            pass
        elif node.tag == R:
            parent[k] = _deleted(node, ctx)
        elif node.tag in _WRAPPERS:
            _delete_content(node, ctx)
        elif node.tag == W + "sdt":
            content = node.find(W + "sdtContent")
            if content is not None:
                _delete_content(content, ctx)
        elif node.tag == W + "fldSimple":
            runs = _simple_to_runs(node)
            parent[k : k + 1] = runs
            continue  # the runs are deleted on the next passes
        else:
            raise unsupported(
                f"the header or footer holds {node.tag.rsplit('}', 1)[-1]}; it is not rewritten"
            )
        k += 1


def rewrite_paragraph(p: ET.Element, text: str, ctx: RevisionContext) -> None:
    """``p`` reading as ``text``, as tracked changes: its live content deleted, the new
    content one insertion at its end, in the formatting of its first live run."""
    rpr = first_rpr(p)
    _delete_content(p, ctx)
    runs = content_runs(text, rpr)
    if runs:
        inserted = ctx.revision("ins")
        inserted.extend(runs)
        p.append(inserted)


# -- new parts -----------------------------------------------------------------------------


def _paragraph_props(like: ET.Element | None) -> ET.Element | None:
    ppr = like.find(PPR) if like is not None else None
    if ppr is None:
        return None
    clone = copy.deepcopy(ppr)
    for change in clone.findall(W + "pPrChange"):
        clone.remove(change)
    mark = clone.find(RPR)
    if mark is not None:
        for child in [c for c in mark if c.tag in (INS, DEL, W + "rPrChange")]:
            mark.remove(child)
    return clone


def new_part_bytes(group: str, part: str, text: str, like: ET.Element | None) -> bytes:
    """A header or footer part of one paragraph reading as ``text``, its paragraph and run
    formatting copied from the paragraph ``like`` (another part's) when given."""
    root = GROUPS[group][0]
    template = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:{root} xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:p/></w:{root}>'
    ).encode()
    spans = scan(template, Container(root), part)
    p = ET.Element(P_TAG)
    ppr = _paragraph_props(like)
    if ppr is not None:
        p.append(ppr)
    p.extend(content_runs(text, first_rpr(like) if like is not None else None))
    return spans.splice({0: spans.serialize(p, 0)})


def free_part_names(pkg: Package, group: str, taken: Iterable[str] = ()) -> Iterator[str]:
    """``word/header{N}.xml`` (or footer) names the package does not use, in order."""
    used = {n.lower() for n in pkg.names} | {n.lower() for n in taken}
    n = 1
    while True:
        name = f"word/{group}{n}.xml"
        if name not in used:
            yield name
        n += 1


def free_rids(pkg: Package) -> Iterator[str]:
    """Relationship ids the main document's relationships do not use, in order."""
    text = pkg.read(DOCUMENT_RELS).decode("utf-8") if DOCUMENT_RELS in pkg else ""
    n = max((int(m) for m in _REL_ID.findall(text)), default=0) + 1
    while True:
        yield f"rId{n}"
        n += 1


def _decode(pkg: Package, name: str) -> str:
    if name not in pkg:
        raise unsupported(f"the document has no {name}", part=name)
    try:
        return pkg.read(name).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise unsupported(f"{name} is not UTF-8 text", part=name) from exc


def _insert_before_close(name: str, text: str, fragment: str) -> str:
    close = text.rfind("</")
    if close < 0:
        raise DocControlError(ErrorCode.NOT_A_DOCX, f"{name} has no closing tag", part=name)
    return text[:close] + fragment + text[close:]


def package_entries(pkg: Package, created: Sequence[tuple[str, str, str]]) -> dict[str, bytes]:
    """The relationships part and content types with an entry for each new part, given as
    ``(part, rId, group)``."""
    if not created:
        return {}
    types = _decode(pkg, CONTENT_TYPES)
    rels = _decode(pkg, DOCUMENT_RELS)
    declared = {m.lower() for m in re.findall(r"""PartName\s*=\s*["']/?([^"']+)["']""", types)}
    known = set(_REL_ID.findall(rels))
    overrides, relationships = [], []
    for part, rid, group in created:
        _, content_type, rel_type = GROUPS[group]
        if rid.removeprefix("rId") in known:
            raise DocControlError(ErrorCode.INTERNAL, f"relationship {rid} is already used")
        if part.lower() not in declared:
            overrides.append(f'<Override PartName="/{part}" ContentType="{content_type}"/>')
        target = part.removeprefix("word/")
        relationships.append(
            f'<Relationship Id="{rid}" Type="{rel_type}" Target={quoteattr(target)}/>'
        )
    out = {
        DOCUMENT_RELS: _insert_before_close(DOCUMENT_RELS, rels, "".join(relationships)).encode()
    }
    if overrides:
        out[CONTENT_TYPES] = _insert_before_close(CONTENT_TYPES, types, "".join(overrides)).encode()
    return out


__all__ = [
    "CONTENT_TYPES",
    "DOCUMENT_RELS",
    "GROUPS",
    "content_runs",
    "free_part_names",
    "free_rids",
    "new_part_bytes",
    "package_entries",
    "rewrite_paragraph",
    "root_name",
]
