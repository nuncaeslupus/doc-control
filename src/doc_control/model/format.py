"""Summaries of run and paragraph properties (``w:rPr`` / ``w:pPr``) as plain dicts.

Only properties that are present are reported, so an empty dict means "inherits
everything". Lengths stay in the units OOXML uses (twips), sizes are in points.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from doc_control.model.parts import W

_OFF = frozenset({"0", "false", "off", "none"})


def _val(elem: ET.Element | None) -> str | None:
    return None if elem is None else elem.get(W + "val")


def _toggle(props: ET.Element, name: str) -> bool | None:
    elem = props.find(W + name)
    if elem is None:
        return None
    return (elem.get(W + "val") or "1").lower() not in _OFF


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def run_format(rpr: ET.Element | None) -> dict[str, Any]:
    """Character formatting set by a ``w:rPr``."""
    out: dict[str, Any] = {}
    if rpr is None:
        return out
    if (style := _val(rpr.find(W + "rStyle"))) is not None:
        out["style"] = style
    for key, tag in (
        ("bold", "b"),
        ("italic", "i"),
        ("small_caps", "smallCaps"),
        ("strike", "strike"),
    ):
        if (flag := _toggle(rpr, tag)) is not None:
            out[key] = flag
    if (underline := _val(rpr.find(W + "u"))) is not None:
        out["underline"] = underline
    fonts = rpr.find(W + "rFonts")
    if fonts is not None:
        font = fonts.get(W + "ascii") or fonts.get(W + "hAnsi")
        if font:
            out["font"] = font
    if (half_points := _int(_val(rpr.find(W + "sz")))) is not None:
        out["size_pt"] = half_points / 2
    if (color := _val(rpr.find(W + "color"))) is not None:
        out["color"] = color
    if (highlight := _val(rpr.find(W + "highlight"))) is not None:
        out["highlight"] = highlight
    if (align := _val(rpr.find(W + "vertAlign"))) is not None:
        out["vertical_align"] = align
    return out


def para_format(ppr: ET.Element | None) -> dict[str, Any]:
    """Paragraph formatting set by a ``w:pPr`` (its ``w:rPr`` is not included)."""
    out: dict[str, Any] = {}
    if ppr is None:
        return out
    if (align := _val(ppr.find(W + "jc"))) is not None:
        out["alignment"] = align
    ind = ppr.find(W + "ind")
    if ind is not None:
        indent: dict[str, int] = {}
        for key, names in (
            ("left", ("left", "start")),
            ("right", ("right", "end")),
            ("first_line", ("firstLine",)),
            ("hanging", ("hanging",)),
        ):
            for name in names:
                if (value := _int(ind.get(W + name))) is not None:
                    indent[key] = value
                    break
        if indent:
            out["indent"] = indent
    spacing = ppr.find(W + "spacing")
    if spacing is not None:
        space: dict[str, Any] = {}
        for key in ("before", "after", "line"):
            if (value := _int(spacing.get(W + key))) is not None:
                space[key] = value
        if (rule := spacing.get(W + "lineRule")) is not None:
            space["line_rule"] = rule
        if space:
            out["spacing"] = space
    for key, tag in (
        ("keep_next", "keepNext"),
        ("keep_lines", "keepLines"),
        ("page_break_before", "pageBreakBefore"),
    ):
        if (flag := _toggle(ppr, tag)) is not None:
            out[key] = flag
    if (level := _int(_val(ppr.find(W + "outlineLvl")))) is not None:
        out["outline_level"] = level
    return out
