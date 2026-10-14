"""A document opened for reading: the package, its paragraph index and styles.

Nothing here writes. Derived tables (styles, per-paragraph style ids) are built
on first use and cached for the life of the object, which is one operation.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, TypeVar

from doc_control.model.index import DocumentIndex, Paragraph, build_index
from doc_control.model.parts import W
from doc_control.model.styles import StyleTable, load_styles
from doc_control.ooxml.zipio import Package

DOCUMENT_PART = "word/document.xml"
_TC_OPEN = re.compile(rb"<(?:[\w.-]+:)?tc[\s>]")
_TC_CLOSE = re.compile(rb"</(?:[\w.-]+:)?tc\s*>")


def _stamp(path: str) -> tuple[int, int, int]:
    """Changes whenever the file is written (``ctime`` too, which no tool can set back)."""
    st = Path(path).stat()
    return st.st_mtime_ns, st.st_ctime_ns, st.st_size


@dataclass(frozen=True)
class ParaProps:
    """The properties of a paragraph that decide its style and heading level."""

    style: str | None  # explicit ``w:pStyle``, else None
    outline_level: int | None  # explicit ``w:outlineLvl`` (0-9), else None


#: Documents shared across operations while :func:`shared_documents` is active, keyed by
#: path and holding the file's (mtime, size) when it was read.
_SHARED: ContextVar[dict[str, tuple[tuple[int, int, int], Document]] | None] = ContextVar(
    "doc_control_shared_documents", default=None
)


@contextmanager
def shared_documents(cache: dict[str, tuple[tuple[int, int, int], Document]]) -> Iterator[None]:
    """Reuse one parse of each unchanged file for every ``Document(path)`` made inside."""
    token = _SHARED.set(cache)
    try:
        yield
    finally:
        _SHARED.reset(token)


_T = TypeVar("_T")


class Document:
    def __new__(cls, path: str) -> Document:
        cache = _SHARED.get()
        if cache is not None:
            hit = cache.get(path)
            if hit is not None and Path(path).exists() and hit[0] == _stamp(path):
                return hit[1]
        return super().__new__(cls)

    def __init__(self, path: str) -> None:
        if "index" in self.__dict__:
            return  # a shared, already parsed document
        self.path = path
        cache = _SHARED.get()
        # Stamped before reading, so a write racing the read only causes a re-parse.
        stamp = _stamp(path) if cache is not None and Path(path).exists() else (0, 0, 0)
        self.pkg = Package(Path(path))
        self.index: DocumentIndex = build_index(self.pkg.read(DOCUMENT_PART))
        if cache is not None:
            cache[path] = (stamp, self)

    @property
    def sha256(self) -> str:
        return self.pkg.sha256

    def envelope_doc(self) -> dict[str, Any]:
        """The ``doc`` block of a response."""
        doc: dict[str, Any] = {
            "path": self.path,
            "sha256": self.sha256,
            "paragraphs": len(self.index),
        }
        if self.pkg.text_source is not None:
            doc["source"] = self.pkg.text_source
        return doc

    def memo(self, key: Hashable, build: Callable[[], _T]) -> _T:
        """``build()`` once per document and ``key``: derived tables shared by every
        operation run on an open document (doc_control.api); never change what it returns."""
        cache: dict[Hashable, Any] = self.__dict__.setdefault("_memo", {})
        if key not in cache:
            cache[key] = build()
        value: _T = cache[key]
        return value

    def story_view(self, key: str) -> Document:
        """This document seen through one story (``""`` the body, ``fn3``, ``hdr1.default``):
        the same package and styles, with that story's paragraph index, so every
        paragraph-level helper reads it as it reads the body."""
        if not key:
            return self

        def build() -> Document:
            from doc_control.model.stories import find_story, story_index

            view = object.__new__(Document)
            view.path, view.pkg = self.path, self.pkg
            view.index = story_index(self.pkg, find_story(self.pkg, key))
            view.__dict__["styles"] = self.styles
            return view

        return self.memo(("story", key), build)

    def paragraph_element(self, para: Paragraph) -> ET.Element:
        """The parsed ``w:p`` of ``para``, parsed once per document; read it, never change it."""
        return self.index.spans.parsed(para.span_index)

    @cached_property
    def styles(self) -> StyleTable:
        return load_styles(self.pkg)

    @cached_property
    def props(self) -> tuple[ParaProps, ...]:
        out: list[ParaProps] = []
        spans = self.index.spans
        for para in self.index.paragraphs:
            elem = spans.parsed(para.span_index)
            ppr = elem.find(W + "pPr")
            style = level = None
            if ppr is not None:
                pstyle = ppr.find(W + "pStyle")
                style = pstyle.get(W + "val") if pstyle is not None else None
                outline = ppr.find(W + "outlineLvl")
                raw = outline.get(W + "val") if outline is not None else None
                level = int(raw) if raw is not None and raw.isdigit() else None
            out.append(ParaProps(style, level))
        return tuple(out)

    @cached_property
    def cells(self) -> tuple[int, ...]:
        """Per paragraph, the byte offset of its innermost table cell (-1: not in a cell)."""
        data = self.index.spans.data
        events = sorted(
            [(m.start(), True) for m in _TC_OPEN.finditer(data)]
            + [(m.start(), False) for m in _TC_CLOSE.finditer(data)]
        )
        spans = self.index.spans.paragraphs
        out: list[int] = []
        stack: list[int] = []
        e = 0
        for para in self.index.paragraphs:
            start = spans[para.span_index].start
            while e < len(events) and events[e][0] < start:
                pos, opens = events[e]
                if opens:
                    stack.append(pos)
                elif stack:
                    stack.pop()
                e += 1
            out.append(stack[-1] if stack else -1)
        return tuple(out)

    def style_id(self, para: Paragraph) -> str | None:
        """The paragraph's style id: explicit, else the document's default paragraph style."""
        return self.props[para.number - 1].style or self.styles.default_paragraph

    def heading_level(self, para: Paragraph) -> int | None:
        """1-9 when the paragraph is a heading (direct outline level, else its style's)."""
        props = self.props[para.number - 1]
        if props.outline_level is not None:
            return props.outline_level + 1 if props.outline_level <= 8 else None
        return self.styles.heading_level(props.style)

    def paragraph_bytes(self, para: Paragraph) -> bytes:
        return self.index.spans.paragraph_bytes(para.span_index)

    def count_elements(self, local_name: str) -> int:
        """How many ``<…:local_name>`` start tags ``word/document.xml`` holds (any prefix)."""
        pattern = rb"<(?:[\w.-]+:)?" + re.escape(local_name.encode()) + rb"[\s/>]"
        return len(re.findall(pattern, self.index.spans.data))
