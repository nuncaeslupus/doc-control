"""`fields.list`: every field in every story, read-only.

Complex fields (``w:fldChar`` begin/separate/end, the code split across any number of
``w:instrText`` runs, nested fields, fields spanning paragraphs) and ``w:fldSimple`` are
both listed, outer field first. A field in a text box is reported at the address of the
paragraph holding the box (``in: "textbox"``); one in a comment at ``comment:<id>``.
Citation fields (Word sources, Zotero, Mendeley) are listed like any other and are opaque
to every write operation (D-L4).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.model.parts import OFFICE_REL, W, parse_part, related_part
from doc_control.model.stories import list_stories
from doc_control.registry import operation

_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_SKIP = frozenset(
    {
        W + "pPr",
        W + "rPr",
        W + "del",
        W + "moveFrom",
        W + "delText",
        W + "delInstrText",
        _MC_FALLBACK,
    }
)
#: Field types whose result is legitimately empty.
_NO_RESULT = frozenset({"XE", "TC", "TA", "BOOKMARK", "SET", "AUTHOR"})
_KINDS = ["body", "notes", "headers", "comments"]
_CITATION_ADDINS = ("ADDIN ZOTERO", "ADDIN CSL_CITATION", "ADDIN EN.CITE", "ADDIN MENDELEY")


@dataclass
class _Frame:
    row: dict[str, Any]
    code: list[str] = field(default_factory=list)
    result: list[str] = field(default_factory=list)
    separated: bool = False
    dirty: bool = False


def _type_of(code: str) -> str:
    words = code.split()
    if not words:
        return ""
    head = words[0].upper()
    if head == "ADDIN" and len(words) > 1:
        return "ADDIN " + words[1]
    return head


class _Scanner:
    """Walks paragraphs in order, keeping the stack of open complex fields."""

    def __init__(self, story: str, rows: list[dict[str, Any]], box: bool = False) -> None:
        self.story, self.rows, self.box = story, rows, box
        self.stack: list[_Frame] = []
        self.at = ""

    def paragraph(self, elem: ET.Element, at: str) -> None:
        self.at = at
        self._walk(elem)

    def finish(self) -> None:
        while self.stack:  # a field the document leaves open: report what is there
            self._close(self.stack.pop())

    def _open(self, code: str = "", simple: bool = False) -> _Frame:
        row: dict[str, Any] = {"at": self.at, "story": self.story}
        if self.box:
            row["in"] = "textbox"
        frame = _Frame(row, [code] if code else [], separated=simple)
        self.rows.append(row)
        self.stack.append(frame)
        return frame

    def _close(self, frame: _Frame) -> None:
        code = "".join(frame.code).strip()
        kind = _type_of(code)
        result = "".join(frame.result)
        stale = (
            frame.dirty or not frame.separated or (not result.strip() and kind not in _NO_RESULT)
        )
        frame.row.update(
            {
                "code": code,
                "type": kind,
                "result": result,
                "stale": stale,
                "citation": kind in ("CITATION", "BIBLIOGRAPHY")
                or kind.startswith(_CITATION_ADDINS),
            }
        )

    def _walk(self, elem: ET.Element) -> None:
        for child in elem:
            tag = child.tag
            if tag in _SKIP:
                continue
            if tag == W + "fldChar":
                self._fld_char(child)
            elif tag == W + "instrText":
                if self.stack and not self.stack[-1].separated:
                    self.stack[-1].code.append(child.text or "")
            elif tag == W + "t":
                for frame in self.stack:
                    if frame.separated:
                        frame.result.append(child.text or "")
            elif tag == W + "fldSimple":
                frame = self._open(child.get(W + "instr", ""), simple=True)
                frame.dirty = child.get(W + "dirty") in ("1", "true", "on")
                self._walk(child)
                self._close(self.stack.pop())
            elif tag == W + "txbxContent":
                # A box has its own fields: scan it apart from the paragraph around it.
                inner = _Scanner(self.story, self.rows, box=True)
                inner.at = self.at
                inner._walk(child)
                inner.finish()
            else:
                self._walk(child)

    def _fld_char(self, elem: ET.Element) -> None:
        kind = elem.get(W + "fldCharType")
        if kind == "begin":
            self._open().dirty = elem.get(W + "dirty") in ("1", "true", "on")
        elif kind == "separate" and self.stack:
            self.stack[-1].separated = True
        elif kind == "end" and self.stack:
            self._close(self.stack.pop())


@operation(
    "fields.list",
    "read",
    "Every field of the document, by story: its address, instruction code, type (PAGE, TOC, "
    "REF, ADDIN ZOTERO_ITEM, CITATION, ...), result text and whether the result is stale. "
    "Covers complex fields (also nested, or split across runs), simple fields and fields in "
    "text boxes.",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            "stories": {
                "type": "array",
                "items": {"enum": _KINDS},
                "description": 'Stories to read (default all): "body", "notes", "headers" '
                'and "comments".',
            },
            "type": {
                "type": "string",
                "minLength": 1,
                "description": 'Only fields of this type, e.g. "TOC" or "ADDIN ZOTERO_ITEM".',
            },
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def fields_list(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    kinds = tuple(request.get("stories", _KINDS))
    rows: list[dict[str, Any]] = []
    for story in list_stories(doc.pkg, tuple(k for k in kinds if k != "comments")):
        view = doc.story_view(story.key)
        scan = _Scanner(story.key or "body", rows)
        for para in view.index.paragraphs:
            scan.paragraph(view.paragraph_element(para), str(para.address))
        scan.finish()
    if "comments" in kinds:
        name = related_part(doc.pkg, OFFICE_REL + "comments", "word/comments.xml")
        if name is not None:
            for comment in parse_part(doc.pkg, name).iter(W + "comment"):
                scan = _Scanner("comments", rows)
                for node in comment.iter(W + "p"):
                    scan.paragraph(node, f"comment:{comment.get(W + 'id', '')}")
                scan.finish()
    wanted = request.get("type")
    if wanted is not None:
        rows = [r for r in rows if r["type"] == wanted.upper()]
    return Reply({"fields": rows, "count": len(rows)}, doc=doc.envelope_doc())
