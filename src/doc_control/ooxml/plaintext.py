"""Plain text and Markdown read as a .docx: the parts a :class:`Package` needs, built in memory.

Read operations accept a ``.txt`` or ``.md`` path and see the text as a minimal Word
document; nothing is written next to it, and write operations refuse it.

Paragraphs
    The file is UTF-8 (a byte order mark is dropped); line ends are ``\\n``, ``\\r\\n``
    or ``\\r``. When the text holds an empty line, a paragraph is a block of lines
    between empty lines, its lines joined by one space (hard-wrapped prose, as in
    Project Gutenberg files). One empty line separates two paragraphs; each further
    empty line in a run becomes an empty paragraph, so a heading set off by three empty
    lines has two empty paragraphs around it (see ``chapter_blank_lines`` in
    ``segment``). A text with no empty line has one paragraph per line.

Markdown (``.md``)
    A line ``# Title`` to ``###### Title`` is its own paragraph in style Heading 1-6.
    A thematic break (``***``, ``---``, ``* * *``, ``___``) is its own paragraph and
    keeps its characters, which scene detection reads as a break. Emphasis markers
    (``*``, ``**``, ``_``, ``__``) around words and backslash escapes are dropped.
    Anything else (lists, quotes, links, code) is kept as written.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from doc_control.errors import DocControlError, ErrorCode

TEXT_SUFFIXES = (".txt", ".md", ".markdown")

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$")
_BREAK = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_EMPH = re.compile(r"(?<![\w*])(\*|_)(?=\S)(.+?)(?<=\S)\1(?![\w*])")
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_HIDDEN = 0xF0000  # supplementary private use area; escapable characters are ASCII
_UNHIDE = re.compile("[\U000f0021-\U000f007e]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
    'relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)


def _styles() -> str:
    headings = "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
        f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        f'<w:pPr><w:outlineLvl w:val="{n - 1}"/></w:pPr></w:style>'
        for n in range(1, 7)
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{_W}">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:qFormat/></w:style>'
        f"{headings}</w:styles>"
    )


def refuse_write(path: Path) -> None:
    """``UNSUPPORTED_CONTENT`` when ``path`` is a text file: it is read, never written."""
    if path.suffix.lower() in TEXT_SUFFIXES:
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            f"{path} is plain text: only read operations accept it; write to a .docx",
            path=str(path),
        )


def decode(raw: bytes) -> str:
    """The text of a UTF-8 file, line ends made ``\\n`` (``UnicodeDecodeError`` if not UTF-8)."""
    text = raw.decode("utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _inline(text: str) -> str:
    # Escaped characters are hidden from the emphasis patterns, then put back bare.
    text = _ESCAPE.sub(lambda m: chr(_HIDDEN + ord(m.group(1))), text)
    for _ in range(3):  # nested emphasis, e.g. ***x***
        stripped = _EMPH.sub(r"\2", _STRONG.sub(r"\2", text))
        if stripped == text:
            break
        text = stripped
    return _UNHIDE.sub(lambda m: chr(ord(m.group()) - _HIDDEN), text)


def paragraphs(text: str, markdown: bool) -> list[tuple[str, int | None]]:
    """``(text, heading level or None)`` per paragraph; see the module docstring."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the final line end closes the last line, it does not open one
    blocks = any(not line.strip() for line in lines)
    out: list[tuple[str, int | None]] = []
    pending: list[str] = []
    blanks = 0

    def flush() -> None:
        if pending:
            joined = " ".join(part.strip() for part in pending)
            out.append((_inline(joined) if markdown else joined, None))
            pending.clear()

    for line in lines:
        if not line.strip():
            flush()
            # The first empty line of a run separates; each further one is a paragraph.
            if blanks or not out:
                out.append(("", None))
            blanks += 1
            continue
        blanks = 0
        heading = _ATX.match(line) if markdown else None
        if heading:
            flush()
            out.append((_inline(heading.group(2) or ""), len(heading.group(1))))
        elif markdown and _BREAK.match(line):
            flush()
            out.append((line.strip(), None))
        elif blocks:
            pending.append(line)
        else:
            out.append((_inline(line) if markdown else line, None))
    flush()
    return out


def _paragraph_xml(text: str, level: int | None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>' if level else ""
    if not text:
        return f"<w:p>{ppr}</w:p>"
    runs = []
    for i, part in enumerate(_CONTROL.sub("", text).split("\t")):
        if i:
            runs.append("<w:tab/>")
        if part:
            runs.append(f'<w:t xml:space="preserve">{escape(part)}</w:t>')
    return f"<w:p>{ppr}<w:r>{''.join(runs)}</w:r></w:p>"


def document_xml(text: str, markdown: bool) -> bytes:
    body = "".join(_paragraph_xml(t, level) for t, level in paragraphs(text, markdown))
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    ).encode()


def package_bytes(text: str, markdown: bool) -> bytes:
    """A .docx (zip bytes) whose paragraphs are those of ``text``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", document_xml(text, markdown))
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        zf.writestr("word/styles.xml", _styles())
    return buf.getvalue()
