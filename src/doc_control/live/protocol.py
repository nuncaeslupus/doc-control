"""What an app adapter (LibreOffice over UNO, Word over COM) does for the live engine.

Paragraphs are numbered from 0 over the main story, as the file engine numbers them,
and their text leaves out tracked deletions, so file-engine addresses and fingerprints
hold on the open document. Offsets are characters of that text, the end exclusive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class LiveUnavailable(Exception):
    """The app cannot be driven; the message is the reason given to the caller."""


@dataclass(frozen=True)
class Span:
    """A range of the open document: paragraph ``start`` offset ``a`` to ``end`` offset ``b``.

    ``b`` is None for "to the end of paragraph ``end``".
    """

    start: int
    end: int
    a: int = 0
    b: int | None = None


class LiveAdapter(Protocol):
    app: str

    def probe(self) -> dict[str, Any]:
        """``{"app", "available", "reachable", "version"}`` plus ``reason`` when not reachable.

        Never raises.
        """

    def is_open(self, path: Path) -> bool: ...

    def open(self, path: Path) -> None:
        """Attach to the window holding ``path``, opening the file when none does."""

    def dirty(self, path: Path) -> bool: ...

    def paragraphs(self, path: Path) -> list[str]:
        """The paragraph texts as the writer sees them (tracked deletions left out), for
        finding one: an app may leave note marks in them (``text`` is exact)."""

    def text(self, path: Path, span: Span) -> str:
        """The text of ``span``, tracked deletions left out."""

    def select(self, path: Path, span: Span) -> str:
        """Select ``span`` in the app's window and return its text."""

    def selection(self, path: Path) -> tuple[str, int]:
        """The selected text and the index of the paragraph holding the cursor."""

    def editable(self, path: Path, span: Span, replace: bool) -> None:
        """Refuse (``UNSUPPORTED_CONTENT``) a comment on ``span``, or with ``replace`` a
        tracked replace of it, that the file engine would refuse too: one range across
        containers, or (Word only so far) into a field or link."""

    def comment(self, path: Path, span: Span, text: str, author: str) -> None: ...

    def tracked_replace(self, path: Path, span: Span, text: str, author: str) -> None: ...

    def tracked_insert(self, path: Path, after: int, text: str, author: str) -> None:
        """A new paragraph after paragraph ``after`` (-1: at the start), as a tracked insertion."""

    def save(self, path: Path) -> None: ...

    def close(self, path: Path) -> None:
        """Close without saving; the caller has saved or been told to discard."""


@dataclass
class _FakeDoc:
    paragraphs: list[str]
    dirty: bool = False
    selection: Span = field(default_factory=lambda: Span(0, 0, 0, 0))
    comments: list[tuple[Span, str, str]] = field(default_factory=list)


class FakeAdapter:
    """An in-memory app for tests: documents are lists of paragraph texts."""

    app = "fake"

    def __init__(self, files: dict[str, list[str]] | None = None) -> None:
        self.files = {str(Path(p).resolve()): list(t) for p, t in (files or {}).items()}
        self.docs: dict[str, _FakeDoc] = {}

    def _doc(self, path: Path) -> _FakeDoc:
        return self.docs[str(path.resolve())]

    def probe(self) -> dict[str, Any]:
        return {"app": self.app, "available": True, "reachable": True, "version": "0"}

    def is_open(self, path: Path) -> bool:
        return str(path.resolve()) in self.docs

    def open(self, path: Path) -> None:
        key = str(path.resolve())
        self.docs.setdefault(key, _FakeDoc(list(self.files.get(key, []))))

    def dirty(self, path: Path) -> bool:
        return self._doc(path).dirty

    def paragraphs(self, path: Path) -> list[str]:
        return list(self._doc(path).paragraphs)

    def _text(self, doc: _FakeDoc, span: Span) -> str:
        parts = doc.paragraphs[span.start : span.end + 1]
        if span.b is not None:
            parts[-1] = parts[-1][: span.b]
        parts[0] = parts[0][span.a :]
        return "\n".join(parts)

    def text(self, path: Path, span: Span) -> str:
        return self._text(self._doc(path), span)

    def select(self, path: Path, span: Span) -> str:
        doc = self._doc(path)
        doc.selection = span
        return self._text(doc, span)

    def selection(self, path: Path) -> tuple[str, int]:
        doc = self._doc(path)
        return self._text(doc, doc.selection), doc.selection.end

    def editable(self, path: Path, span: Span, replace: bool) -> None:
        self.text(path, span)

    def comment(self, path: Path, span: Span, text: str, author: str) -> None:
        doc = self._doc(path)
        doc.comments.append((span, text, author))
        doc.dirty = True

    def tracked_replace(self, path: Path, span: Span, text: str, author: str) -> None:
        doc = self._doc(path)
        head = doc.paragraphs[span.start][: span.a]
        tail = "" if span.b is None else doc.paragraphs[span.end][span.b :]
        doc.paragraphs[span.start : span.end + 1] = [head + text + tail]
        doc.dirty = True

    def tracked_insert(self, path: Path, after: int, text: str, author: str) -> None:
        doc = self._doc(path)
        doc.paragraphs.insert(after + 1, text)
        doc.dirty = True

    def save(self, path: Path) -> None:
        doc = self._doc(path)
        self.files[str(path.resolve())] = list(doc.paragraphs)
        doc.dirty = False

    def close(self, path: Path) -> None:
        del self.docs[str(path.resolve())]
