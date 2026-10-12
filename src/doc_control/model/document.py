"""A document opened for reading: the package, its paragraph index and styles.

Nothing here writes. Derived tables (styles, per-paragraph style ids) are built
on first use and cached for the life of the object, which is one operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from doc_control.model.index import DocumentIndex, Paragraph, build_index
from doc_control.model.parts import W
from doc_control.model.styles import StyleTable, load_styles
from doc_control.ooxml.zipio import Package

DOCUMENT_PART = "word/document.xml"


@dataclass(frozen=True)
class ParaProps:
    """The properties of a paragraph that decide its style and heading level."""

    style: str | None  # explicit ``w:pStyle``, else None
    outline_level: int | None  # explicit ``w:outlineLvl`` (0-9), else None


class Document:
    def __init__(self, path: str) -> None:
        self.path = path
        self.pkg = Package(Path(path))
        self.index: DocumentIndex = build_index(self.pkg.read(DOCUMENT_PART))

    @property
    def sha256(self) -> str:
        return self.pkg.sha256

    def envelope_doc(self) -> dict[str, Any]:
        """The ``doc`` block of a response."""
        return {"path": self.path, "sha256": self.sha256, "paragraphs": len(self.index)}

    @cached_property
    def styles(self) -> StyleTable:
        return load_styles(self.pkg)

    @cached_property
    def props(self) -> tuple[ParaProps, ...]:
        out: list[ParaProps] = []
        spans = self.index.spans
        for para in self.index.paragraphs:
            elem = spans.parse_paragraph(para.span_index)
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
