"""The style table of ``word/styles.xml``: ids, names, inheritance and heading levels."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from doc_control.model.format import para_format, run_format
from doc_control.model.parts import OFFICE_REL, W, parse_part, related_part
from doc_control.ooxml.zipio import Package

STYLES_PART = "word/styles.xml"
_HEADING_NAME = re.compile(r"heading ([1-9])", re.IGNORECASE)
_HEADING_ID = re.compile(r"Heading([1-9])")
_MAX_CHAIN = 64
_ON = ("1", "true", "on")


@dataclass(frozen=True)
class Style:
    id: str
    name: str
    type: str
    based_on: str | None
    next: str | None
    link: str | None
    default: bool
    custom: bool
    quick: bool
    ppr: dict[str, Any]
    rpr: dict[str, Any]


class StyleTable:
    """Every ``w:style`` of the document, in file order."""

    def __init__(self, styles: list[Style]) -> None:
        self.styles = tuple(styles)
        self._by_id = {s.id: s for s in styles}
        self._by_name = {s.name.lower(): s for s in reversed(styles)}
        self.default_paragraph: str | None = next(
            (s.id for s in styles if s.type == "paragraph" and s.default), None
        )

    def __len__(self) -> int:
        return len(self.styles)

    def get(self, style_id: str) -> Style | None:
        return self._by_id.get(style_id)

    def find(self, key: str) -> Style | None:
        """A style by id, else by name (case-insensitive)."""
        return self._by_id.get(key) or self._by_name.get(key.lower())

    def chain(self, style_id: str) -> list[Style]:
        """``style_id`` followed by its ``basedOn`` ancestors; a cycle ends the chain."""
        out: list[Style] = []
        seen: set[str] = set()
        current = self._by_id.get(style_id)
        while current is not None and current.id not in seen and len(out) < _MAX_CHAIN:
            out.append(current)
            seen.add(current.id)
            current = self._by_id.get(current.based_on) if current.based_on else None
        return out

    def effective(self, style_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Paragraph and run formatting after inheritance (nearest style wins per key)."""
        ppr: dict[str, Any] = {}
        rpr: dict[str, Any] = {}
        for style in reversed(self.chain(style_id)):
            ppr.update(style.ppr)
            rpr.update(style.rpr)
        return ppr, rpr

    def heading_level(self, style_id: str | None) -> int | None:
        """1-9 for a heading style, else None.

        The first outline level found walking up ``basedOn`` decides (9 means body
        text, so None); without any, a style named ``heading N`` or with id
        ``HeadingN`` is level N.
        """
        if style_id is None:
            return None
        chain = self.chain(style_id)
        for style in chain:
            level = style.ppr.get("outline_level")
            if level is not None:
                return level + 1 if 0 <= level <= 8 else None
        for style in chain:
            m = _HEADING_NAME.fullmatch(style.name) or _HEADING_ID.fullmatch(style.id)
            if m:
                return int(m.group(1))
        return None


def _flag(elem: ET.Element, tag: str) -> bool:
    child = elem.find(W + tag)
    return child is not None and (child.get(W + "val") or "1").lower() not in ("0", "false", "off")


def load_styles(pkg: Package) -> StyleTable:
    name = related_part(pkg, OFFICE_REL + "styles", STYLES_PART)
    if name is None:
        return StyleTable([])
    out: list[Style] = []
    for elem in parse_part(pkg, name).findall(W + "style"):
        style_id = elem.get(W + "styleId")
        if not style_id:
            continue
        label = elem.find(W + "name")
        based = elem.find(W + "basedOn")
        nxt = elem.find(W + "next")
        link = elem.find(W + "link")
        out.append(
            Style(
                id=style_id,
                name=(label.get(W + "val") if label is not None else None) or style_id,
                type=elem.get(W + "type") or "paragraph",
                based_on=based.get(W + "val") if based is not None else None,
                next=nxt.get(W + "val") if nxt is not None else None,
                link=link.get(W + "val") if link is not None else None,
                default=(elem.get(W + "default") or "0").lower() in _ON,
                custom=(elem.get(W + "customStyle") or "0").lower() in _ON,
                quick=_flag(elem, "qFormat"),
                ppr=para_format(elem.find(W + "pPr")),
                rpr=run_format(elem.find(W + "rPr")),
            )
        )
    return StyleTable(out)
