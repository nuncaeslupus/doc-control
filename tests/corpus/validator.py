"""The corpus validator: structural lint, plus a LibreOffice round trip when available.

The .NET Open XML SDK validator is not used here (it arrives with T27). When
``soffice`` is on PATH every document is also converted to HTML with
LibreOffice headless, in one batch. A document passes that step only if
LibreOffice produced output and that output contains every visible text
segment of the main story and the footnotes and endnotes (see
:func:`expected_segments`), compared with all whitespace removed. LibreOffice
is more lenient than Word, so this catches gross breakage only.

Without ``soffice`` the validator runs in lint-only mode and says so in
:func:`validator_name`. Set ``DOC_CONTROL_REQUIRE_SOFFICE=1`` to make a missing
LibreOffice an error instead (local evidence runs do; CI has no LibreOffice).

Documents are identified by caller-supplied ids (short sha256 prefixes for
private documents), never by their original file names. Problem messages carry
counts and positions only, never document text.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from tests.corpus.structural import W, validate_structure

NO_SOFFICE_ENV = "DOC_CONTROL_NO_SOFFICE"
REQUIRE_ENV = "DOC_CONTROL_REQUIRE_SOFFICE"
SOFFICE_FILTER = "html:HTML (StarWriter):UTF8"
OUTPUT_LIMIT = 600

# Exceptions that mean "this is not a readable .docx package".
UNREADABLE = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    OSError,
    KeyError,
    ET.ParseError,
    NotImplementedError,
    RuntimeError,
    ValueError,
)


def soffice_path() -> str | None:
    if os.environ.get(NO_SOFFICE_ENV):
        return None
    return shutil.which("soffice") or shutil.which("libreoffice")


@functools.cache
def _soffice_version(soffice: str) -> str:
    try:
        proc = subprocess.run(
            [soffice, "--version"], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = re.search(r"LibreOffice\s+(\d+(?:\.\d+)*)", proc.stdout)
    return match.group(1) if match else ""


def validator_name() -> str:
    soffice = soffice_path()
    if soffice is None:
        return (
            "structural lint only (LibreOffice not found; lint-only mode, "
            f"set {REQUIRE_ENV}=1 to require the round trip)"
        )
    version = _soffice_version(soffice)
    label = f"LibreOffice {version}" if version else "LibreOffice"
    return f"structural lint + {label} headless round trip"


def require_soffice() -> None:
    """Raise when ``DOC_CONTROL_REQUIRE_SOFFICE`` is set but LibreOffice is unavailable."""
    if os.environ.get(REQUIRE_ENV) and soffice_path() is None:
        raise RuntimeError(f"{REQUIRE_ENV} is set but LibreOffice (soffice) is not available")


# -- expected text ------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """A run of contiguous visible text inside one paragraph."""

    story: str  # "body", "footnote" or "endnote"
    paragraph: int  # 1-based paragraph number within the story
    text: str


# Containers whose children are walked as part of the same text flow.
_TRANSPARENT = frozenset(
    W + t for t in ("r", "ins", "moveTo", "hyperlink", "smartTag", "customXml", "sdt", "sdtContent")
)
# Elements that render nothing and do not interrupt the text around them.
_SILENT = frozenset(
    W + t
    for t in (
        "rPr",
        "pPr",
        "sdtPr",
        "sdtEndPr",
        "bookmarkStart",
        "bookmarkEnd",
        "commentRangeStart",
        "commentRangeEnd",
        "proofErr",
        "permStart",
        "permEnd",
        "lastRenderedPageBreak",
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
    )
)
# Block containers walked to find paragraphs (drawings and text boxes are not).
_BLOCKS = frozenset(W + t for t in ("body", "tbl", "tr", "tc", "sdt", "sdtContent", "customXml"))
_OFF = frozenset({"0", "false", "off"})


def _hidden(run: ET.Element) -> bool:
    rpr = run.find(W + "rPr")
    if rpr is None:
        return False
    vanish = rpr.find(W + "vanish")
    return vanish is not None and vanish.get(W + "val", "true") not in _OFF


class _StoryWalker:
    """Collect segments in document order.

    A segment ends at the paragraph end and at anything that renders as
    something other than its ``w:t`` text (tabs, breaks, note references,
    symbols, drawings, deletions, field boundaries). Field results are skipped
    because LibreOffice recomputes fields such as PAGE and DATE; the text
    around a field is still checked. Hidden (``w:vanish``) runs are skipped.
    """

    def __init__(self, story: str) -> None:
        self.story = story
        self.paragraph = 0
        self.field_depth = 0
        self.current: list[str] = []
        self.segments: list[Segment] = []

    def flush(self) -> None:
        text = "".join(self.current)
        self.current = []
        if text.strip():
            self.segments.append(Segment(self.story, self.paragraph, text))

    def blocks(self, el: ET.Element) -> None:
        for child in el:
            if child.tag == W + "p":
                self.paragraph += 1
                self.inline(child)
                self.flush()
            elif child.tag in _BLOCKS:
                self.blocks(child)

    def inline(self, el: ET.Element) -> None:
        for child in el:
            tag = child.tag
            if tag == W + "t":
                if self.field_depth == 0:
                    self.current.append(child.text or "")
            elif tag in _SILENT:
                continue
            elif tag == W + "fldChar":
                self.flush()
                kind = child.get(W + "fldCharType")
                if kind == "begin":
                    self.field_depth += 1
                elif kind == "end" and self.field_depth:
                    self.field_depth -= 1
            elif tag == W + "r" and _hidden(child):
                self.flush()
            elif tag in _TRANSPARENT:
                self.inline(child)
            else:
                self.flush()


def expected_segments(data: bytes) -> list[Segment]:
    """Visible text segments of the main story, footnotes and endnotes.

    Raises one of :data:`UNREADABLE` when ``data`` is not a readable package.
    """
    with zipfile.ZipFile(BytesIO(data)) as zf:
        names = set(zf.namelist())
        stories = [("body", ET.fromstring(zf.read("word/document.xml")))]
        for story in ("footnote", "endnote"):
            name = f"word/{story}s.xml"
            if name in names:
                stories.append((story, ET.fromstring(zf.read(name))))
    segments: list[Segment] = []
    for story, root in stories:
        walker = _StoryWalker(story)
        if story == "body":
            body = root.find(W + "body")
            if body is not None:
                walker.blocks(body)
        else:
            for note in root.iter(W + story):
                if note.get(W + "type") in (None, "normal"):
                    walker.blocks(note)
        walker.flush()
        segments += walker.segments
    return segments


# -- LibreOffice output -------------------------------------------------


class _TextExtractor(HTMLParser):
    _IGNORED = frozenset({"head", "style", "script", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._IGNORED:
            self.ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED and self.ignored:
            self.ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def html_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(parser.parts)


def _squash(text: str) -> str:
    return "".join(text.split())


def missing_segments(segments: list[Segment], output: str) -> list[Segment]:
    """Segments whose text (whitespace removed) does not occur in ``output``.

    Searches forward from the previous match first, so a long document costs a
    single pass; falls back to the whole output when the order differs.
    """
    haystack = _squash(output)
    missing = []
    cursor = 0
    for segment in segments:
        needle = _squash(segment.text)
        at = haystack.find(needle, cursor)
        if at < 0:
            at = haystack.find(needle)
        if at < 0:
            missing.append(segment)
        else:
            cursor = at + len(needle)
    return missing


def _soffice_excerpt(doc_id: str, output: str) -> str:
    """LibreOffice's stdout/stderr lines about ``doc_id``, else its tail, truncated."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    relevant = [ln for ln in lines if f"{doc_id}.docx" in ln or ln.startswith("Error")]
    text = " | ".join(relevant or lines)
    if len(text) > OUTPUT_LIMIT:
        text = "..." + text[-OUTPUT_LIMIT:]
    return text or "(no output)"


def _run_soffice(soffice: str, paths: list[str], workdir: Path) -> tuple[int | None, str]:
    out = workdir / "lo-out"
    profile = workdir / "lo-profile"
    try:
        proc = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile.resolve().as_uri()}",
                "--headless",
                "--norestore",
                "--convert-to",
                SOFFICE_FILTER,
                "--outdir",
                str(out),
                *paths,
            ],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        partial = partial.decode(errors="replace") if isinstance(partial, bytes) else partial
        return None, f"soffice timed out after {exc.timeout}s; {partial}"
    return proc.returncode, proc.stdout + proc.stderr


def _iter_readable(
    docs: Mapping[str, bytes], results: dict[str, list[str]]
) -> Iterator[tuple[str, bytes, list[Segment]]]:
    for doc_id, data in docs.items():
        try:
            segments = expected_segments(data)
        except UNREADABLE as exc:
            results[doc_id] = [
                f"unreadable package, not sent to LibreOffice ({type(exc).__name__})"
            ]
            continue
        yield doc_id, data, segments


def libreoffice_round_trip(docs: Mapping[str, bytes], workdir: Path) -> dict[str, list[str]]:
    """Convert every readable document to HTML in one soffice run; return problems per id."""
    soffice = soffice_path()
    if soffice is None:
        return {doc_id: [] for doc_id in docs}
    src = workdir / "lo-in"
    out = workdir / "lo-out"
    src.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[str]] = {}
    expected: dict[str, list[Segment]] = {}
    for doc_id, data, segments in _iter_readable(docs, results):
        (src / f"{doc_id}.docx").write_bytes(data)
        expected[doc_id] = segments
    if not expected:
        return results
    paths = [str(src / f"{doc_id}.docx") for doc_id in expected]
    code, output = _run_soffice(soffice, paths, workdir)
    for doc_id, segments in expected.items():
        html = out / f"{doc_id}.html"
        if not html.is_file():
            results[doc_id] = [
                "LibreOffice could not load the document or produced no Writer output "
                f"(soffice exit {code}; output: {_soffice_excerpt(doc_id, output)})"
            ]
            continue
        lost = missing_segments(segments, html_text(html.read_text("utf-8", errors="replace")))
        if lost:
            first = lost[0]
            results[doc_id] = [
                f"LibreOffice output lacks {len(lost)} of {len(segments)} text segments "
                f"(first missing: {first.story} paragraph {first.paragraph})"
            ]
        else:
            results[doc_id] = []
    return results


def validate_documents(docs: Mapping[str, bytes], workdir: Path) -> dict[str, list[str]]:
    """Run the full validator over ``docs``; return ``{id: problems}``."""
    check_ids(docs)
    require_soffice()
    results = {doc_id: validate_structure(data) for doc_id, data in docs.items()}
    for doc_id, problems in libreoffice_round_trip(docs, workdir).items():
        results[doc_id] += problems
    return results


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def check_ids(docs: Mapping[str, bytes]) -> None:
    for doc_id in docs:
        if not _SAFE_ID.match(doc_id):
            raise ValueError(f"unsafe document id {doc_id!r}")
