"""Plain text and Markdown read as a .docx: the parts a :class:`Package` needs, built in memory.

Read operations accept a ``.txt`` or ``.md`` path and see the text as a minimal Word
document; nothing is written next to it, and write operations refuse it.

Encoding
    UTF-32 or UTF-16 with a byte order mark; else UTF-8 (with or without one), any line
    that is not UTF-8 read as Windows-1252; UTF-16 without a mark is refused. Line ends
    are ``\\n``, ``\\r\\n`` or ``\\r``. The ``doc`` block of a reply names the
    ``source``: format, encoding (``utf-8+cp1252`` when lines mix them) and mode.

Paragraphs (``.txt``), by mode
    ``blocks`` (the text holds an empty line): a block of lines between empty lines is
    a paragraph, its lines joined by one space (hard-wrapped prose, as in Project
    Gutenberg files). One empty line separates two paragraphs; each further empty line
    in a run is an empty paragraph, so ``chapter_blank_lines`` in ``segment`` counts the
    extra ones. ``lines`` (no empty line at all): every line is a paragraph.

Markdown (``.md``)
    Blocks as in ``blocks`` (soft-wrapped lines joined); a front matter block at the
    start (``---``, a ``key:`` line, ..., ``---``) is skipped. A line ``# Title`` to
    ``###### Title`` is its own paragraph in style Heading 1-6. A line made only of
    ``* _ - ~ #`` and spaces, three or more of them (``***``, ``* * *``, ``---``), is its
    own paragraph and keeps its characters, which scene detection reads as a break. A
    bullet item starts a paragraph, and so does a numbered one that is ``1.`` or follows
    another item. Emphasis markers (``*``, ``**``, and ``_``, ``__`` outside words) are
    dropped; a backslash-escaped character is kept, literal. Lines inside a backtick
    fence are kept as written, one paragraph each. Anything else (setext headings, hard
    breaks, inline code, tables, quotes) is kept as written.
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
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_BREAK = re.compile(r"^ {0,3}[-*_~#](?:[ \t]*[-*_~#]){2,}[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,})")
_BULLET = re.compile(r"^ {0,3}[-*+][ \t]")
_ORDERED = re.compile(r"^ {0,3}(\d{1,9})[.)][ \t]")
_YAML_KEY = re.compile(r"^[A-Za-z_][\w-]*[ \t]*:")
_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_EMPH = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])|(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_ESCAPE = re.compile(r"(\\[!-/:-@\[-`{-~])")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_BLANK = " \t"

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


def _line_bytes(raw: bytes) -> tuple[str, str]:
    """UTF-8, with any line that is not UTF-8 read as Windows-1252 (Latin-1 for its five
    unassigned bytes), so one stray byte does not garble every accent of the file."""
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    out: list[str] = []
    utf8 = False  # a line with non-ASCII characters read as UTF-8
    for line in raw.splitlines(keepends=True):
        try:
            out.append(line.decode("utf-8"))
            utf8 = utf8 or not line.isascii()
        except UnicodeDecodeError:
            try:
                out.append(line.decode("cp1252"))
            except UnicodeDecodeError:
                out.append(line.decode("latin-1"))
    return "".join(out), "utf-8+cp1252" if utf8 else "cp1252"


def decode(raw: bytes) -> tuple[str, str]:
    """``(text, encoding)``, line ends made ``\\n``; ``ValueError`` names an unreadable file.

    UTF-32 or UTF-16 with a byte order mark; else UTF-8 (with or without one), any line
    that is not UTF-8 read as Windows-1252.
    """
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        text, encoding = raw.decode("utf-32"), "utf-32"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text, encoding = raw.decode("utf-16"), "utf-16"
    elif b"\x00" in raw[:4096]:
        raise ValueError("UTF-16 without a byte order mark (or binary): save it as UTF-8")
    else:
        bom = raw.startswith(b"\xef\xbb\xbf")
        text, encoding = _line_bytes(raw[3:] if bom else raw)
        if bom and encoding == "utf-8":
            encoding = "utf-8-sig"
    return text.replace("\r\n", "\n").replace("\r", "\n"), encoding


def _emphasis(text: str) -> str:
    for _ in range(3):  # nested emphasis, e.g. ***x***
        stripped = _EMPH.sub(lambda m: m.group(1) or m.group(2), text)
        stripped = _STRONG.sub(lambda m: m.group(1) or m.group(2), stripped)
        if stripped == text:
            break
        text = stripped
    return text


def _inline(text: str) -> str:
    """Emphasis markers dropped; a backslash-escaped character kept, bare and literal."""
    parts = _ESCAPE.split(text)
    return "".join(part[1] if i % 2 else _emphasis(part) for i, part in enumerate(parts))


def _blank(line: str) -> bool:
    return not line.strip(_BLANK)


def _starts_item(line: str, pending: list[str]) -> bool:
    """A list item starts a paragraph: a bullet always, a numbered item when it is ``1.``
    or follows another item (``1914. was`` inside a sentence does not)."""
    if _BULLET.match(line):
        return True
    number = _ORDERED.match(line)
    if number is None:
        return False
    in_list = bool(pending) and bool(_BULLET.match(pending[0]) or _ORDERED.match(pending[0]))
    return not pending or number.group(1) == "1" or in_list


def _front_matter(lines: list[str]) -> int:
    """How many lines a YAML front matter block at the start takes (0: none)."""
    if len(lines) < 3 or lines[0].rstrip(_BLANK) != "---" or not _YAML_KEY.match(lines[1]):
        return 0
    for n, line in enumerate(lines[1:], start=1):
        if line.rstrip(_BLANK) in ("---", "..."):
            return n + 1
    return 0


def paragraphs(text: str, markdown: bool) -> tuple[list[tuple[str, int | None]], str]:
    """``(text, heading level or None)`` per paragraph, and the mode: ``markdown``,
    ``blocks`` or ``lines``; see the module docstring."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the final line end closes the last line, it does not open one
    if markdown:
        lines = lines[_front_matter(lines) :]
        mode = "markdown"
    else:
        mode = "blocks" if any(_blank(line) for line in lines) else "lines"
    out: list[tuple[str, int | None]] = []
    if mode == "lines":
        return [(line, None) for line in lines], mode
    pending: list[str] = []
    blanks = 0
    fence = ""  # the opening fence while inside a code block

    def flush() -> None:
        if pending:
            joined = " ".join(part.strip(_BLANK) for part in pending)
            out.append((_inline(joined) if markdown else joined, None))
            pending.clear()

    for line in lines:
        opening = _FENCE.match(line) if markdown else None
        if fence or opening:
            flush()
            out.append((line, None))  # code: one literal paragraph per line
            if not fence and opening:
                fence = opening.group(1)
            elif fence and line.strip(_BLANK).startswith(fence) and not line.strip(_BLANK + "`"):
                fence = ""
            blanks = 0
            continue
        if _blank(line):
            flush()
            # The first empty line of a run separates; each further one is a paragraph.
            if blanks or not out:
                out.append(("", None))
            blanks += 1
            continue
        blanks = 0
        if markdown and _BREAK.match(line):
            flush()
            out.append((line.strip(_BLANK), None))
            continue
        heading = _ATX.match(line) if markdown else None
        if heading:
            flush()
            out.append((_inline(heading.group(2) or ""), len(heading.group(1))))
            continue
        if markdown and _starts_item(line, pending):
            flush()
        pending.append(line)
    flush()
    return out, mode


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


def package_bytes(text: str, markdown: bool) -> tuple[bytes, str]:
    """A .docx (zip bytes) whose paragraphs are those of ``text``, and the mode used."""
    paras, mode = paragraphs(text, markdown)
    body = "".join(_paragraph_xml(t, level) for t, level in paras)
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        zf.writestr("word/styles.xml", _styles())
    return buf.getvalue(), mode
