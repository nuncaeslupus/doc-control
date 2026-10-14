"""The stories of a document: the body, each footnote and endnote, and each header and
footer a section defines, with the part and container that hold their paragraphs.

A story's key is its address prefix (see :mod:`doc_control.model.address`): ``""`` for the
body, ``fn3`` and ``en2`` for notes by their ``w:id``, ``hdr1.default`` and ``ftr2.first``
for the header or footer a section's ``w:headerReference``/``w:footerReference`` points at.
A section with no reference of a kind inherits the previous section's, so a header part is
addressed through the section that defines it and reports every section that uses it.
"""

from __future__ import annotations

import io
import posixpath
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex, build_index
from doc_control.model.parts import DOCUMENT_RELS, OFFICE_REL, W, parse_part, related_part
from doc_control.ooxml.splice import BODY, Container
from doc_control.ooxml.zipio import Package

DOCUMENT_PART = "word/document.xml"
HF_KINDS = ("default", "first", "even")
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass(frozen=True)
class Story:
    """One story: where its paragraphs are, and what it is."""

    key: str
    kind: str  # "body", "footnote", "endnote", "header" or "footer"
    part: str
    container: Container
    note_id: int | None = None
    section: int | None = None
    hf_kind: str | None = None  # "default", "first" or "even"
    used_by_sections: tuple[int, ...] = ()


BODY_STORY = Story("", "body", DOCUMENT_PART, BODY)


def _notes(pkg: Package, kind: str) -> list[Story]:
    name = related_part(pkg, OFFICE_REL + f"{kind}s", f"word/{kind}s.xml")
    if name is None:
        return []
    out = []
    for note in parse_part(pkg, name).iter(W + kind):
        if note.get(W + "type", "normal") != "normal":
            continue  # separators and continuation notices are not text
        ident = note.get(W + "id", "")
        if not ident.isdigit() or ident.startswith("0"):
            continue
        prefix = "fn" if kind == "footnote" else "en"
        container = Container(f"{kind}s", kind, ident)
        out.append(Story(f"{prefix}{ident}", kind, name, container, note_id=int(ident)))
    return out


def _targets(pkg: Package) -> dict[str, str]:
    """Relationship id to part name, for the document's internal relationships."""
    if DOCUMENT_RELS not in pkg:
        return {}
    out = {}
    for rel in parse_part(pkg, DOCUMENT_RELS):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        out[rel.get("Id", "")] = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("word", target))
        )
    return out


def section_properties(pkg: Package) -> list[ET.Element]:
    """Every section's ``w:sectPr``, in document order (the body's own one last)."""
    sections = []
    for _, elem in ET.iterparse(io.BytesIO(pkg.read(DOCUMENT_PART)), events=("end",)):
        if elem.tag == W + "sectPr":
            sections.append(elem)
        elif elem.tag == W + "p":
            elem.clear()  # paragraph content is not needed; keep the memory flat
    return sections


def _pages(pkg: Package) -> list[Story]:
    targets = _targets(pkg)
    sections = section_properties(pkg)
    defined: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for number, sect in enumerate(sections, start=1):
        for tag, kind in (("headerReference", "header"), ("footerReference", "footer")):
            for ref in sect.findall(W + tag):
                which = ref.get(W + "type", "default")
                name = targets.get(ref.get(_R + "id", ""))
                if which in HF_KINDS and name is not None and name in pkg:
                    defined.setdefault((kind, which), []).append((number, name))
    out = []
    for (kind, which), refs in defined.items():
        for i, (number, name) in enumerate(refs):
            until = refs[i + 1][0] if i + 1 < len(refs) else len(sections) + 1
            prefix = "hdr" if kind == "header" else "ftr"
            out.append(
                Story(
                    f"{prefix}{number}.{which}",
                    kind,
                    name,
                    Container("hdr" if kind == "header" else "ftr"),
                    section=number,
                    hf_kind=which,
                    used_by_sections=tuple(range(number, until)),
                )
            )
    order = {"header": 0, "footer": 1}
    out.sort(key=lambda s: (s.section or 0, order[s.kind], HF_KINDS.index(s.hf_kind or "")))
    return out


def list_stories(
    pkg: Package, kinds: tuple[str, ...] = ("body", "notes", "headers")
) -> list[Story]:
    """The document's stories of ``kinds``: the body, then footnotes and endnotes in id
    order, then each section's headers and footers."""
    out: list[Story] = []
    if "body" in kinds:
        out.append(BODY_STORY)
    if "notes" in kinds:
        out += _notes(pkg, "footnote") + _notes(pkg, "endnote")
    if "headers" in kinds:
        out += _pages(pkg)
    return out


def find_story(pkg: Package, key: str) -> Story:
    """The story whose address prefix is ``key``; ``ANCHOR_NOT_FOUND`` when there is none."""
    if not key:
        return BODY_STORY
    kinds = ("notes",) if key.startswith(("fn", "en")) else ("headers",)
    for story in list_stories(pkg, kinds):
        if story.key == key:
            return story
    raise DocControlError(ErrorCode.ANCHOR_NOT_FOUND, f"the document has no story {key}", story=key)


def story_index(pkg: Package, story: Story) -> DocumentIndex:
    """The paragraph index of ``story``, numbered from 1 within it."""
    return build_index(pkg.read(story.part), story.container, story.part, story.key)
