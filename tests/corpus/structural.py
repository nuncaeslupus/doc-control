"""Stdlib structural lint for .docx packages.

This is not a schema validator. It checks the invariants whose violation makes
Word refuse or "repair" a file, as listed in docs/research/01-tooling-and-risks.md
§1 (Q1.5, Q1.7): every part parses, every part has a content type, every
relationship target exists, comment ids agree across the comment parts,
revision ids are unique, ``w:delText`` sits only inside a deletion,
``mc:Ignorable`` prefixes are declared, and ``w14:paraId`` values are unique
and in range.

Messages name package parts and ids only, never the file path, so the lint can
run on private documents without leaking their names.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from collections import Counter
from collections.abc import Iterable
from io import BytesIO
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
W15 = "{http://schemas.microsoft.com/office/word/2012/wordml}"
W16CID = "{http://schemas.microsoft.com/office/word/2016/wordml/cid}"
W16CEX = "{http://schemas.microsoft.com/office/word/2018/wordml/cex}"
MC_IGNORABLE = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable"
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"

REVISION_TAGS = frozenset(
    W + tag
    for tag in (
        "ins",
        "del",
        "moveFrom",
        "moveTo",
        "rPrChange",
        "pPrChange",
        "sectPrChange",
        "tblPrChange",
        "trPrChange",
        "tcPrChange",
        "tblGridChange",
        "numberingChange",
    )
)
DELETION_CONTAINERS = frozenset({W + "del", W + "moveFrom"})
DELETED_TEXT = frozenset({W + "delText", W + "delInstrText"})
STORY_PARTS = re.compile(r"^word/(document|footnotes|endnotes|comments|header\d*|footer\d*)\.xml$")
_START_TAG = re.compile(rb"<(?![?!])[^>]*>", re.S)
_XMLNS = re.compile(rb"\sxmlns:([A-Za-z_][\w.-]*)\s*=")
_IGNORABLE = re.compile(rb"\s[\w.-]+:Ignorable\s*=\s*([\"'])(.*?)\1", re.S)


def validate_structure(data: bytes) -> list[str]:
    """Return a list of problems; an empty list means the package passed."""
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        return ["not a zip package"]
    with zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        raw = {n: zf.read(n) for n in names}

    problems: list[str] = []
    trees: dict[str, ET.Element] = {}
    for name, content in raw.items():
        if name.endswith((".xml", ".rels")):
            try:
                trees[name] = ET.fromstring(content)
            except ET.ParseError as exc:
                problems.append(f"{name}: does not parse ({exc})")
            else:
                problems += _check_ignorable(name, content)
    for required in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
        if required not in raw:
            problems.append(f"missing required part {required}")
    if problems:
        return problems

    problems += _check_content_types(trees["[Content_Types].xml"], names)
    problems += _check_relationships(trees, set(names))
    problems += _check_revisions(trees)
    problems += _check_para_ids(trees)
    problems += _check_comments(trees)
    problems += _check_footnotes(trees)
    return problems


def _check_ignorable(name: str, content: bytes) -> list[str]:
    match = _START_TAG.search(content)
    if match is None:
        return []
    tag = match.group(0)
    ign = _IGNORABLE.search(tag)
    if ign is None:
        return []
    declared = {m.decode() for m in _XMLNS.findall(tag)}
    missing = [p for p in ign.group(2).decode().split() if p not in declared]
    return [f"{name}: mc:Ignorable prefix {p!r} is not declared on the root" for p in missing]


def _check_content_types(types: ET.Element, names: Iterable[str]) -> list[str]:
    defaults = {
        el.get("Extension", "").lower() for el in types.iter(CT + "Default") if el.get("Extension")
    }
    overrides = {el.get("PartName", "").lower() for el in types.iter(CT + "Override")}
    problems = []
    for name in names:
        if name == "[Content_Types].xml":
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if "/" + name.lower() not in overrides and ext not in defaults:
            problems.append(f"{name}: no content type (Default or Override)")
    for part in overrides:
        if part.lstrip("/") not in {n.lower() for n in names}:
            problems.append(f"[Content_Types].xml: Override for missing part {part}")
    return problems


def _rels_source(rels_name: str) -> str:
    """``word/_rels/document.xml.rels`` -> ``word/document.xml``; root rels -> ''."""
    folder, base = posixpath.split(rels_name)
    parent = posixpath.dirname(folder)
    return posixpath.join(parent, base.removesuffix(".rels")) if base != ".rels" else ""


def _check_relationships(trees: dict[str, ET.Element], names: set[str]) -> list[str]:
    problems = []
    for rels_name, tree in trees.items():
        if not rels_name.endswith(".rels"):
            continue
        source = _rels_source(rels_name)
        if source and source not in names:
            problems.append(f"{rels_name}: relationships for missing part {source}")
        base = posixpath.dirname(source)
        ids = Counter(el.get("Id") for el in tree.iter(PR + "Relationship"))
        problems += [f"{rels_name}: duplicate relationship id {i}" for i, n in ids.items() if n > 1]
        for el in tree.iter(PR + "Relationship"):
            if el.get("TargetMode") == "External":
                continue
            target = el.get("Target", "")
            resolved = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join(base, target))
            )
            if resolved not in names:
                problems.append(f"{rels_name}: target {target} ({el.get('Id')}) does not exist")
    doc_rels = trees.get("word/_rels/document.xml.rels")
    rel_ids = (
        {el.get("Id") for el in doc_rels.iter(PR + "Relationship")}
        if doc_rels is not None
        else set()
    )
    for el in trees["word/document.xml"].iter():
        rid = el.get(R + "id")
        if rid is not None and rid not in rel_ids:
            problems.append(f"word/document.xml: r:id {rid} has no relationship")
    return problems


def _stories(trees: dict[str, ET.Element]) -> list[tuple[str, ET.Element]]:
    return [(n, t) for n, t in sorted(trees.items()) if STORY_PARTS.match(n)]


def _check_revisions(trees: dict[str, ET.Element]) -> list[str]:
    problems = []
    ids: Counter[str] = Counter()
    for name, tree in _stories(trees):
        stack: list[tuple[ET.Element, bool]] = [(tree, False)]
        while stack:
            el, in_deletion = stack.pop()
            if el.tag in REVISION_TAGS:
                rid = el.get(W + "id")
                if rid is None:
                    problems.append(f"{name}: {el.tag.removeprefix(W)} without w:id")
                else:
                    ids[rid] += 1
                if el.get(W + "author") is None:
                    problems.append(f"{name}: revision {rid} without w:author")
            if el.tag in DELETED_TEXT and not in_deletion:
                problems.append(f"{name}: {el.tag.removeprefix(W)} outside w:del")
            if el.tag == W + "t" and in_deletion:
                problems.append(f"{name}: w:t inside a deletion")
            child_flag = in_deletion or el.tag in DELETION_CONTAINERS
            stack.extend((child, child_flag) for child in el)
    problems += [f"revision w:id {i} used {n} times" for i, n in sorted(ids.items()) if n > 1]
    return problems


def _check_para_ids(trees: dict[str, ET.Element]) -> list[str]:
    problems = []
    seen: Counter[str] = Counter()
    for name, tree in _stories(trees):
        for p in tree.iter(W + "p"):
            pid = p.get(W14 + "paraId")
            if pid is None:
                continue
            seen[pid] += 1
            try:
                value = int(pid, 16)
            except ValueError:
                value = -1
            if len(pid) != 8 or not 0 < value < 0x80000000:
                problems.append(f"{name}: w14:paraId {pid} out of range")
    problems += [f"w14:paraId {i} used {n} times" for i, n in sorted(seen.items()) if n > 1]
    return problems


def _ids(tree: ET.Element | None, tag: str, attr: str) -> list[str]:
    if tree is None:
        return []
    return [v for el in tree.iter(tag) if (v := el.get(attr)) is not None]


def _check_comments(trees: dict[str, ET.Element]) -> list[str]:
    problems: list[str] = []
    comments = trees.get("word/comments.xml")
    extended = trees.get("word/commentsExtended.xml")
    cids = trees.get("word/commentsIds.xml")
    cex = trees.get("word/commentsExtensible.xml")
    if comments is None:
        problems += [
            f"{name} present without word/comments.xml"
            for name, tree in (
                ("word/commentsExtended.xml", extended),
                ("word/commentsIds.xml", cids),
                ("word/commentsExtensible.xml", cex),
            )
            if tree is not None
        ]
    defined = _ids(comments, W + "comment", W + "id")
    problems += [f"comment id {i} defined twice" for i, n in Counter(defined).items() if n > 1]
    doc = trees["word/document.xml"]
    refs = _ids(doc, W + "commentReference", W + "id")
    starts = _ids(doc, W + "commentRangeStart", W + "id")
    ends = _ids(doc, W + "commentRangeEnd", W + "id")
    problems += [f"commentReference {i} has no comment" for i in sorted(set(refs) - set(defined))]
    problems += [f"comment {i} is never referenced" for i in sorted(set(defined) - set(refs))]
    if Counter(starts) != Counter(ends):
        problems.append("commentRangeStart / commentRangeEnd ids are not paired")
    problems += [f"comment range {i} has no comment" for i in sorted(set(starts) - set(defined))]

    last_para: set[str] = set()
    if comments is not None:
        for c in comments.iter(W + "comment"):
            paras = list(c.iter(W + "p"))
            pid = paras[-1].get(W14 + "paraId") if paras else None
            if pid is not None:
                last_para.add(pid)
    ex_ids = _ids(extended, W15 + "commentEx", W15 + "paraId")
    if extended is not None:
        problems += [
            f"commentsExtended paraId {p} matches no comment's last paragraph"
            for p in sorted(set(ex_ids) - last_para)
        ]
        for el in extended.iter(W15 + "commentEx"):
            parent = el.get(W15 + "paraIdParent")
            if parent is not None and parent not in ex_ids:
                problems.append(f"commentsExtended paraIdParent {parent} is not a comment")
            if el.get(W15 + "done") not in (None, "0", "1"):
                problems.append(f"commentsExtended paraId {el.get(W15 + 'paraId')}: bad w15:done")
    id_paras = _ids(cids, W16CID + "commentId", W16CID + "paraId")
    if cids is not None:
        problems += [
            f"commentsIds paraId {p} matches no comment" for p in sorted(set(id_paras) - last_para)
        ]
    durable = set(_ids(cids, W16CID + "commentId", W16CID + "durableId"))
    ext_durable = set(_ids(cex, W16CEX + "commentExtensible", W16CEX + "durableId"))
    if cids is not None and cex is not None and durable != ext_durable:
        problems.append("commentsIds and commentsExtensible durableIds differ")
    return problems


def _check_footnotes(trees: dict[str, ET.Element]) -> list[str]:
    refs = set(_ids(trees["word/document.xml"], W + "footnoteReference", W + "id"))
    defined = set(_ids(trees.get("word/footnotes.xml"), W + "footnote", W + "id"))
    return [f"footnoteReference {i} has no footnote" for i in sorted(refs - defined)]
