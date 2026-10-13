"""OpenDocument text (``.odt``) read as a .docx: its paragraphs, built in memory.

Read operations accept a ``.odt`` path and see the document as a minimal Word document;
write operations refuse it, as for plain text (:mod:`doc_control.ooxml.plaintext`).

What is kept: every ``text:p`` and ``text:h`` of the body in document order (inside lists,
sections, tables and the table of contents too), with its text: spans, links and fields
read through, ``text:s`` as its spaces, ``text:tab`` as a tab and ``text:line-break`` as a
line break. A heading (``text:h``) keeps its outline level as style Heading 1-9; any other
paragraph keeps the display name of its paragraph style (automatic styles resolved to
their parent), so ``segment`` can match scene-break or chapter styles by name.

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

from doc_control.ooxml.plaintext import Para, build_docx

ODT_SUFFIXES = (".odt",)
MIMETYPE = "application/vnd.oasis.opendocument.text"

_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_STYLE = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
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


def _style_names(content: ET.Element, styles: ET.Element | None) -> dict[str, str]:
    """Paragraph style name → display name, automatic styles resolved to their parent."""
    named: dict[str, str] = {}
    if styles is not None:
        for style in styles.iter(_STYLE + "style"):
            if style.get(_STYLE + "family") == "paragraph":
                name = style.get(_STYLE + "name", "")
                named[name] = _XML_BAD.sub("", style.get(_STYLE + "display-name", "")) or (
                    _display(name)
                )
    parents: dict[str, str] = {}
    automatic = content.find(_OFFICE + "automatic-styles")
    if automatic is not None:
        for style in automatic.iter(_STYLE + "style"):
            if style.get(_STYLE + "family") == "paragraph":
                parents[style.get(_STYLE + "name", "")] = style.get(
                    _STYLE + "parent-style-name", ""
                )
    out = dict(named)
    for name in parents:
        parent = parents[name]
        for _ in range(MAX_STYLE_CHAIN):  # an automatic parent: follow it, a few steps at most
            if parent not in parents or parent == name:
                break
            parent = parents[parent]
        out[name] = (named.get(parent) or _display(parent)) if parent else ""
    return out


def _text(elem: ET.Element) -> str:
    """The visible text of a paragraph element (see the module docstring)."""
    out: list[str] = [elem.text or ""]
    for child in elem:
        tag = child.tag
        if tag == _TEXT + "s":
            out.append(" " * min(_number(child.get(_TEXT + "c"), 1), MAX_SPACES))
        elif tag == _TEXT + "tab":
            out.append("\t")
        elif tag == _TEXT + "line-break":
            out.append("\n")
        elif not _skipped(tag):
            out.append(_text(child))
        out.append(child.tail or "")
    return "".join(out)


def _walk(elem: ET.Element, names: dict[str, str], out: list[Para]) -> None:
    for child in elem:
        if _skipped(child.tag):
            continue
        if child.tag in _PARAS:
            level = None
            if child.tag == _TEXT + "h":
                level = min(max(_number(child.get(_TEXT + "outline-level"), 1), 1), 9)
            style = names.get(child.get(_TEXT + "style-name", ""), "") or None
            if style in _DEFAULT_STYLES or level is not None:
                style = None
            out.append((_text(child), level, style))
            continue
        _walk(child, names, out)


def paragraphs(raw: bytes) -> list[Para]:
    """``(text, heading level, style name)`` per paragraph of the .odt bytes ``raw``."""
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
    out: list[Para] = []
    try:
        _walk(body, _style_names(content, styles), out)
    except RecursionError as exc:
        raise OdtError("elements nested too deeply") from exc
    return out


def package_bytes(raw: bytes) -> bytes:
    """A .docx (zip bytes) with the paragraphs of the .odt bytes ``raw``."""
    return build_docx(paragraphs(raw))
