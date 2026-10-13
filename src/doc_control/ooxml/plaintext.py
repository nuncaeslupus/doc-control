"""Plain text and Markdown read as a .docx: the parts a :class:`Package` needs, built in memory.

Read operations accept a ``.txt`` or ``.md`` path and see the text as a minimal Word
document; nothing is written next to it, and write operations refuse it.

Encoding
    UTF-8 (with or without a byte order mark), UTF-16 with a byte order mark, else
    Windows-1252; UTF-16 without a mark is refused. Line ends are ``\n``, ``\r\n`` or
    ``\r``. The ``doc`` block of a reply names the ``source``: format, encoding, mode.

Paragraphs (``.txt``), by mode
    ``wrapped`` (hard-wrapped prose, as in Project Gutenberg files: most lines inside
    blocks end near one width): a block of lines between empty lines is a paragraph,
    its lines joined by one space. One empty line separates two paragraphs; each
    further empty line in a run is an empty paragraph, so ``chapter_blank_lines`` in
    ``segment`` counts the extra ones. ``lines``: every line is a paragraph, every empty
    line an empty paragraph.

Markdown (``.md``)
    Blocks as in ``wrapped`` (soft-wrapped lines joined); a front matter block
    (``---`` ... ``---``) at the start is skipped. A line ``# Title`` to ``###### Title``
    is its own paragraph in style Heading 1-6. A line made only of ``* _ - ~ #`` and
    spaces, three or more of them (``***``, ``* * *``, ``---``), is its own paragraph and
    keeps its characters, which scene detection reads as a break. A list item starts a
    paragraph. Emphasis markers (``*``, ``**``, and ``_``, ``__`` outside words) are
    dropped; a backslash-escaped character is kept, literal. Lines inside a ``` fence
    are kept as written, one paragraph each. Anything else is kept as written.
"""

from __future__ import annotations

import io
import re
import zipfile
from itertools import pairwise
from pathlib import Path
from xml.sax.saxutils import escape

from doc_control.errors import DocControlError, ErrorCode

TEXT_SUFFIXES = (".txt", ".md", ".markdown")

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_BREAK = re.compile(r"^ {0,3}[-*_~#](?:[ \t]*[-*_~#]){2,}[ \t]*$")
_FENCE = re.compile(r"^ {0,3}```")
_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])[ \t]")
_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_EMPH = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])|(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_ESCAPE = re.compile(r"(\\[!-/:-@\[-`{-~])")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_BLANK = " \t"
#: Hard-wrapped text: the lines inside blocks mostly end near one width, at most this.
_WRAP_MAX = 120
_WRAP_SHARE = 0.6
_WRAP_NEAR = 0.75

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


def decode(raw: bytes) -> tuple[str, str]:
    """``(text, encoding)``, line ends made ``\\n``; ``ValueError`` names an unreadable file.

    UTF-8 (with or without a byte order mark), UTF-16 with one, else Windows-1252.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text, encoding = raw.decode("utf-16"), "utf-16"
    elif raw.startswith(b"\xef\xbb\xbf"):
        text, encoding = raw[3:].decode("utf-8"), "utf-8-sig"
    elif b"\x00" in raw[:4096]:
        raise ValueError("UTF-16 without a byte order mark (or binary): save it as UTF-8")
    else:
        try:
            text, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, encoding = raw.decode("cp1252"), "cp1252"
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


def _wrapped(lines: list[str]) -> bool:
    """True when most lines inside blocks end near one width (hard-wrapped prose)."""
    inner = [
        len(line.rstrip(_BLANK))
        for line, after in pairwise(lines)
        if not _blank(line) and not _blank(after)
    ]
    if len(inner) < 3:
        return False
    width = sorted(inner)[int(len(inner) * 0.9)]
    near = sum(width * _WRAP_NEAR <= n <= width for n in inner)
    return width <= _WRAP_MAX and near >= len(inner) * _WRAP_SHARE


def _front_matter(lines: list[str]) -> int:
    """How many lines a YAML front matter block at the start takes (0: none)."""
    if not lines or lines[0].rstrip(_BLANK) != "---":
        return 0
    for n, line in enumerate(lines[1:], start=1):
        if line.rstrip(_BLANK) in ("---", "..."):
            return n + 1
    return 0


def paragraphs(text: str, markdown: bool) -> tuple[list[tuple[str, int | None]], str]:
    """``(text, heading level or None)`` per paragraph, and the mode: ``markdown``,
    ``wrapped`` or ``lines``; see the module docstring."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the final line end closes the last line, it does not open one
    if markdown:
        lines = lines[_front_matter(lines) :]
        mode = "markdown"
    else:
        mode = "wrapped" if _wrapped(lines) else "lines"
    out: list[tuple[str, int | None]] = []
    if mode == "lines":
        return [(line, None) if not _blank(line) else ("", None) for line in lines], mode
    pending: list[str] = []
    blanks = 0
    code = False

    def flush() -> None:
        if pending:
            joined = " ".join(part.strip(_BLANK) for part in pending)
            out.append((_inline(joined) if markdown else joined, None))
            pending.clear()

    for line in lines:
        if markdown and (code or _FENCE.match(line)):
            flush()
            out.append((line, None))  # code: one literal paragraph per line
            if _FENCE.match(line):
                code = not code
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
        if markdown and _ITEM.match(line):
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
