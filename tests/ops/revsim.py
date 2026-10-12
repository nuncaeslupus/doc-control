"""An independent model of accepting and rejecting the revisions one author wrote.

It reads ``word/document.xml`` with plain ElementTree and shares no code with
``doc_control``. For every paragraph of the main story it computes the text the
paragraph has when the author's revisions are accepted, rejected, or left as
they are (``current``), and joins a paragraph into the next one when its mark
is a revision that mode removes (an inserted mark when rejecting, a deleted
mark when accepting). Other authors' revisions are always left as they are:
their insertions count, their deletions do not.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path

from tests.helpers import CONTENT_TYPES, RELS

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
_SKIP = frozenset(
    W + n
    for n in ("pPr", "rPr", "sdtPr", "sdtEndPr", "txbxContent", "drawing", "pict", "object", "p")
) | {"{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"}
_FIXED = {
    W + "tab": "\t",
    W + "cr": "\n",
    W + "noBreakHyphen": "\N{NON-BREAKING HYPHEN}",
    W + "softHyphen": "\N{SOFT HYPHEN}",
}

NAMESPACES = (
    f'xmlns:w="{W_NS}" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" mc:Ignorable="w14"'
)


def make_doc(path: Path, body: str, extra: dict[str, bytes] | None = None) -> Path:
    """Write a minimal .docx whose body is the given WordprocessingML."""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {NAMESPACES}><w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("_rels/.rels", RELS, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("word/document.xml", document, zipfile.ZIP_DEFLATED)
        for name, data in (extra or {}).items():
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return path


def document_xml(docx: bytes) -> bytes:
    with zipfile.ZipFile(BytesIO(docx)) as zf:
        return zf.read("word/document.xml")


def numbered_paragraphs(root: ET.Element) -> list[ET.Element]:
    """Main-story paragraphs in order; a paragraph inside a paragraph is not one."""
    out: list[ET.Element] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            if child.tag == W + "p":
                out.append(child)
            else:
                walk(child)

    body = root.find(W + "body")
    assert body is not None
    walk(body)
    return out


ANY = "*"  # every author's revisions, not one's


def _mine(elem: ET.Element, author: str | None) -> bool:
    return author is not None and (author == ANY or elem.get(W + "author") == author)


def _collect(node: ET.Element, author: str | None, mode: str, shown: bool, out: list[str]) -> None:
    """Append the text under ``node``; ``shown`` means deleted text of the author is visible."""
    for child in node:
        tag = child.tag
        if not isinstance(tag, str) or tag in _SKIP:
            continue
        if tag in (W + "ins", W + "moveTo"):
            if not (_mine(child, author) and mode == "reject"):
                _collect(child, author, mode, shown, out)
        elif tag in (W + "del", W + "moveFrom"):
            if _mine(child, author) and mode == "reject":
                _collect(child, author, mode, True, out)
        elif tag == W + "r":
            for item in child:
                if item.tag == W + "t" or (item.tag == W + "delText" and shown):
                    out.append("".join(item.itertext()))
                elif item.tag in _FIXED:
                    out.append(_FIXED[item.tag])
                elif item.tag == W + "br" and item.get(W + "type") not in ("page", "column"):
                    out.append("\n")
        elif tag.startswith(W):
            _collect(child, author, mode, shown, out)


def paragraph_text(p: ET.Element, author: str | None, mode: str) -> str:
    out: list[str] = []
    _collect(p, author, mode, False, out)
    return "".join(out)


def mark_removed(p: ET.Element, author: str | None, mode: str) -> bool:
    """True if the paragraph mark is one of the author's revisions that ``mode`` removes."""
    rpr = p.find(f"{W}pPr/{W}rPr")
    if rpr is None:
        return False
    for mark in rpr:
        if not _mine(mark, author):
            continue
        if (mark.tag in (W + "ins", W + "moveTo") and mode == "reject") or (
            mark.tag in (W + "del", W + "moveFrom") and mode == "accept"
        ):
            return True
    return False


def merge(texts: list[str], joins: list[bool]) -> list[str]:
    out: list[str] = []
    carry = ""
    open_ = False
    for text, join in zip(texts, joins, strict=True):
        text = carry + text
        if join:
            carry, open_ = text, True
        else:
            out.append(text)
            carry, open_ = "", False
    if open_:  # the last mark cannot go: its paragraph stays, even when empty
        out.append(carry)
    return out


def raw_texts(data: bytes, author: str | None, mode: str) -> tuple[list[str], list[bool]]:
    root = ET.fromstring(data)
    paras = numbered_paragraphs(root)
    return (
        [paragraph_text(p, author, mode) for p in paras],
        [mark_removed(p, author, mode) for p in paras],
    )


def resolved(data: bytes, author: str | None, mode: str) -> list[str]:
    """Paragraph texts of ``word/document.xml`` bytes with the author's revisions resolved."""
    texts, joins = raw_texts(data, author, mode)
    return merge(texts, joins)


def all_ids(parts: dict[str, bytes]) -> set[int]:
    """Every ``w:id`` value in the document, comments, footnotes and endnotes parts."""
    ids: set[int] = set()
    for name, data in parts.items():
        if re.fullmatch(r"word/(document|comments|footnotes|endnotes)\.xml", name):
            ids.update(int(m) for m in re.findall(rb'\bw:id="(-?\d+)"', data))
    return ids


def package_parts(docx: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(BytesIO(docx)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def tool_ids(data: bytes, author: str) -> list[int]:
    """The ids of every revision element by ``author`` (inserted and deleted runs, marks)."""
    root = ET.fromstring(data)
    return [
        int(e.get(W + "id", "-1"))
        for e in root.iter()
        if e.tag in (W + "ins", W + "del") and e.get(W + "author") == author
    ]


def deleted_marks(data: bytes) -> list[bool]:
    """For each paragraph: is its mark a tracked deletion (by anyone)?"""
    root = ET.fromstring(data)
    return [p.find(f"{W}pPr/{W}rPr/{W}del") is not None for p in numbered_paragraphs(root)]
