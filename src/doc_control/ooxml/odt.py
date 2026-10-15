"""OpenDocument text (``.odt``) read as a .docx: its paragraphs, built in memory.

Read operations accept a ``.odt`` path and see the document as a minimal Word document;
write operations refuse it, as for plain text (:mod:`doc_control.ooxml.plaintext`).

What is kept: every ``text:p`` and ``text:h`` of the body in document order (inside lists,
sections, tables and the table of contents too), with its text: spans, links and fields
read through, ``text:s`` as its spaces, ``text:tab`` as a tab and ``text:line-break`` as a
line break. A heading (``text:h``) keeps its outline level as style Heading 1-9; any other
paragraph keeps the display name of its paragraph style (automatic styles resolved to
their parent), so ``doc.segment`` can match scene-break or chapter styles by name; a style the
file does not define keeps its own name.

Layout kept for the page estimate and structure detection: ``text:soft-page-break`` (the
page starts LibreOffice wrote when it last laid the document out) becomes a rendered page
mark (``w:lastRenderedPageBreak``), between paragraphs or inside one; ``fo:break-before="page"``
becomes ``pageBreakBefore`` and ``fo:break-after="page"`` a break before the next paragraph;
``fo:text-align="center"`` becomes centred. Both are read through automatic and parent styles
and the paragraph default style.

What is left out, as Word leaves it out of the main story: footnotes and endnotes
(``text:note``), comments (``office:annotation``), text in frames and text boxes
(``draw:frame``), and deleted text of tracked changes (kept apart in
``text:tracked-changes``; inserted text is inline and read).
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import NamedTuple

from doc_control.ooxml.plaintext import Layout, Para, build_docx

ODT_SUFFIXES = (".odt",)
MIMETYPE = "application/vnd.oasis.opendocument.text"

_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_STYLE = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
_FO = "{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}"
_DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
_SKIP = {
    _TEXT + "note",
    _TEXT + "ruby-text",  # the gloss; the base text is read, as in .docx
    _OFFICE + "annotation",
    _OFFICE + "annotation-end",
    _TEXT + "tracked-changes",
}
#: Whole namespaces of anchored drawing objects (frames, shapes, groups, 3D, SVG titles):
#: their text is not the main story.
_SKIP_NAMESPACES = (
    _DRAW,
    "{urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0}",
    "{urn:oasis:names:tc:opendocument:xmlns:dr3d:1.0}",
    "{urn:oasis:names:tc:opendocument:xmlns:chart:1.0}",
    "{http://www.w3.org/2000/svg}",
)
MAX_STYLE_CHAIN = 16  # automatic styles chained to automatic styles (one is usual)
MAX_SPACES = 1000  # one text:s element; a larger text:c is a hostile or broken file
MAX_CONTENT = 256 * 1024 * 1024  # bytes of content.xml once inflated
_DIGITS = re.compile(r"[0-9]{1,12}")
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_PARAS = {_TEXT + "p", _TEXT + "h"}
_SOFT_BREAK = _TEXT + "soft-page-break"
_ENCODED = re.compile(r"_([0-9A-Fa-f]{2,4})_")
_DEFAULT_STYLES = {"Standard", "Default Paragraph Style", "Default"}


class OdtError(ValueError):
    """The file is not a readable OpenDocument text."""


def _skipped(tag: str) -> bool:
    return tag in _SKIP or tag.startswith(_SKIP_NAMESPACES)


def _number(raw: str | None, default: int) -> int:
    """A non-negative integer attribute; ``default`` when absent or not one."""
    return int(raw) if raw is not None and _DIGITS.fullmatch(raw) else default


def _display(name: str) -> str:
    """A style name as shown to the writer (``Text_20_body`` → ``Text body``), keeping only
    characters XML allows."""
    return _XML_BAD.sub("", _ENCODED.sub(lambda m: chr(int(m.group(1), 16)), name))


class _Style(NamedTuple):
    display: str
    parent: str
    align: str | None
    break_before: bool | None
    break_after: bool | None


def _facts(style: ET.Element, display: str) -> _Style:
    props = style.find(_STYLE + "paragraph-properties")
    get = props.get if props is not None else (lambda _key: None)
    before, after = get(_FO + "break-before"), get(_FO + "break-after")
    return _Style(
        display,
        style.get(_STYLE + "parent-style-name", ""),
        get(_FO + "text-align"),
        None if before is None else before == "page",
        None if after is None else after == "page",
    )


def _styles(content: ET.Element, styles: ET.Element | None) -> dict[str, _Style]:
    """Paragraph style name → its facts: named styles (``styles.xml``), then the automatic
    styles of ``content.xml``, whose display name is the one of their named parent."""
    out: dict[str, _Style] = {}
    if styles is not None:
        for style in styles.iter(_STYLE + "style"):
            if style.get(_STYLE + "family") == "paragraph":
                name = style.get(_STYLE + "name", "")
                display = _XML_BAD.sub("", style.get(_STYLE + "display-name", ""))
                out[name] = _facts(style, display or _display(name))
        for style in styles.iter(_STYLE + "default-style"):
            if style.get(_STYLE + "family") == "paragraph":
                out[""] = _facts(style, "")._replace(parent="")
    automatic = content.find(_OFFICE + "automatic-styles")
    if automatic is not None:
        for style in automatic.iter(_STYLE + "style"):
            if style.get(_STYLE + "family") == "paragraph":
                out[style.get(_STYLE + "name", "")] = _facts(style, "")
    return out


class _Resolved(NamedTuple):
    display: str
    layout: Layout
    break_after: bool


def _resolve(name: str, styles: dict[str, _Style], cache: dict[str, _Resolved]) -> _Resolved:
    """A paragraph style's display name and layout, inherited along its parents (at most
    ``MAX_STYLE_CHAIN`` steps); an undefined style keeps its own name."""
    if name in cache:
        return cache[name]
    display = ""
    align = before = after = None
    current, seen = name, 0
    while seen <= MAX_STYLE_CHAIN:
        style = styles.get(current)
        if style is None:
            display = display or _display(current)
            break
        display = display or style.display
        align = style.align if align is None else align
        before = style.break_before if before is None else before
        after = style.break_after if after is None else after
        if not style.parent or style.parent == current:
            break
        current, seen = style.parent, seen + 1
    default = styles.get("")
    if default is not None and current != "":
        align = default.align if align is None else align
    resolved = _Resolved(display, Layout(align == "center", bool(before)), bool(after))
    cache[name] = resolved
    return resolved


def _text(elem: ET.Element, out: list[str], marks: list[int]) -> None:
    """Append the visible text of a paragraph element to ``out`` (see the module docstring),
    and the offset of each page LibreOffice started inside it to ``marks``."""
    out.append(elem.text or "")
    for child in elem:
        tag = child.tag
        if tag == _TEXT + "s":
            out.append(" " * min(_number(child.get(_TEXT + "c"), 1), MAX_SPACES))
        elif tag == _TEXT + "tab":
            out.append("\t")
        elif tag == _TEXT + "line-break":
            out.append("\n")
        elif tag == _SOFT_BREAK:
            marks.append(sum(map(len, out)))
        elif not _skipped(tag):
            _text(child, out, marks)
        out.append(child.tail or "")


class _Reader:
    def __init__(self, styles: dict[str, _Style]) -> None:
        self.styles = styles
        self.cache: dict[str, _Resolved] = {}
        self.paras: list[Para] = []
        self.layouts: list[Layout] = []
        self.page_pending = False  # a soft page break between paragraphs
        self.break_pending = False  # the previous paragraph breaks the page after it

    def walk(self, elem: ET.Element) -> None:
        for child in elem:
            if child.tag == _SOFT_BREAK:
                self.page_pending = True
            elif _skipped(child.tag):
                continue
            elif child.tag in _PARAS:
                self.paragraph(child)
            else:
                self.walk(child)

    def paragraph(self, elem: ET.Element) -> None:
        level = None
        if elem.tag == _TEXT + "h":
            level = min(max(_number(elem.get(_TEXT + "outline-level"), 1), 1), 9)
        resolved = _resolve(elem.get(_TEXT + "style-name", ""), self.styles, self.cache)
        style: str | None = resolved.display or None
        if style in _DEFAULT_STYLES or level is not None:
            style = None
        out: list[str] = []
        marks = [0] if self.page_pending else []
        _text(elem, out, marks)
        layout = resolved.layout
        if self.break_pending:
            layout = layout._replace(break_before=True)
        self.paras.append(("".join(out), level, style))
        self.layouts.append(layout._replace(page_marks=tuple(marks)))
        self.page_pending = False
        self.break_pending = resolved.break_after


def _read(raw: bytes) -> _Reader:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            members = set(zf.namelist())
            if "mimetype" in members and zf.read("mimetype").decode("ascii", "replace").strip() != (
                MIMETYPE
            ):
                raise OdtError("not an OpenDocument text (mimetype)")
            if "content.xml" not in members:
                raise OdtError("no content.xml")
            if "META-INF/manifest.xml" in members and b"encryption-data" in zf.read(
                "META-INF/manifest.xml"
            ):
                raise OdtError("encrypted OpenDocument files are not supported")
            if zf.getinfo("content.xml").file_size > MAX_CONTENT:
                raise OdtError(f"content.xml is larger than {MAX_CONTENT // 2**20} MB")
            content = ET.fromstring(zf.read("content.xml"))
            styles = ET.fromstring(zf.read("styles.xml")) if "styles.xml" in members else None
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise OdtError(str(exc)) from exc
    body = content.find(f"{_OFFICE}body/{_OFFICE}text")
    if body is None:
        raise OdtError("no office:text body")
    reader = _Reader(_styles(content, styles))
    try:
        reader.walk(body)
    except RecursionError as exc:
        raise OdtError("elements nested too deeply") from exc
    return reader


def paragraphs(raw: bytes) -> list[Para]:
    """``(text, heading level, style name)`` per paragraph of the .odt bytes ``raw``."""
    return _read(raw).paras


def package_bytes(raw: bytes) -> bytes:
    """A .docx (zip bytes) with the paragraphs of the .odt bytes ``raw`` and their layout."""
    reader = _read(raw)
    return build_docx(reader.paras, reader.layouts)
