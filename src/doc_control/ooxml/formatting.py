"""Direct paragraph and run properties: set them on a ``w:pPr`` / ``w:rPr`` in schema order.

:func:`apply_ppr` and :func:`apply_rpr` take a property dict (the keys of
:data:`PARAGRAPH_KEYS` and :data:`RUN_KEYS`) and change the element in place. They return
whether anything changed; setting what is already there changes nothing. Children are
inserted where CT_PPrBase / CT_RPr put them, so Word accepts the result. Lengths come in
centimetres and points and are written in twips and half-points.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from doc_control.ooxml.splice import W_NS

W = f"{{{W_NS}}}"

PARAGRAPH_KEYS = (
    "align",
    "indent_left_cm",
    "indent_right_cm",
    "indent_first_line_cm",
    "spacing_before_pt",
    "spacing_after_pt",
    "line_spacing",
)
RUN_KEYS = ("bold", "italic", "underline", "small_caps", "font", "size_pt")
ALIGNMENTS = {"left": "left", "center": "center", "right": "right", "justify": "both"}

_PPR_NAMES = """
pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr
shd tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE
autoSpaceDN bidi adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents
suppressOverlap jc textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle
rPr sectPr pPrChange
"""
_RPR_NAMES = """
ins del moveFrom moveTo rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline
shadow emboss imprint noProof snapToGrid vanish webHidden color spacing w kern position sz
szCs highlight u effect bdr shd fitText vertAlign rtl cs em lang eastAsianLayout specVanish
oMath rPrChange
"""
_PPR_ORDER = tuple(W + n for n in _PPR_NAMES.split())
_RPR_ORDER = tuple(W + n for n in _RPR_NAMES.split())
_THEME_FONTS = tuple(
    W + n for n in ("asciiTheme", "hAnsiTheme", "cstheme", "eastAsiaTheme", "hint")
)
_OFF = frozenset({"0", "false", "off"})
_TWIPS_PER_CM = 1440 / 2.54


def twips(cm: float) -> int:
    return round(cm * _TWIPS_PER_CM)


def _child(parent: ET.Element, tag: str, order: tuple[str, ...]) -> tuple[ET.Element, bool]:
    """The child ``tag`` of ``parent``, created in schema position if missing."""
    found = parent.find(tag)
    if found is not None:
        return found, False
    rank = order.index(tag)
    at = len(parent)
    for i, existing in enumerate(parent):
        if (
            isinstance(existing.tag, str)
            and existing.tag in order
            and order.index(existing.tag) > rank
        ):
            at = i
            break
    elem = ET.Element(tag)
    parent.insert(at, elem)
    return elem, True


def _set_attrs(elem: ET.Element, attrs: dict[str, str], drop: tuple[str, ...] = ()) -> bool:
    changed = False
    for name in drop:
        if name in elem.attrib:
            del elem.attrib[name]
            changed = True
    for name, value in attrs.items():
        if elem.get(name) != value:
            elem.set(name, value)
            changed = True
    return changed


def _set_toggle(rpr: ET.Element, name: str, on: bool) -> bool:
    tag = W + name
    existing = rpr.find(tag)
    if existing is not None:
        current = (existing.get(W + "val") or "1").lower() not in _OFF
        if current == on:
            return False
    elem, _ = _child(rpr, tag, _RPR_ORDER)
    if on:
        elem.attrib.pop(W + "val", None)
    else:
        elem.set(W + "val", "0")
    return True


def _set_val(parent: ET.Element, name: str, value: str, order: tuple[str, ...]) -> bool:
    elem, created = _child(parent, W + name, order)
    return _set_attrs(elem, {W + "val": value}) or created


def apply_rpr(rpr: ET.Element, props: dict[str, Any]) -> bool:
    """Set run properties (``RUN_KEYS`` plus ``style``, a character style id)."""
    changed = False
    if "style" in props:
        changed |= _set_val(rpr, "rStyle", props["style"], _RPR_ORDER)
    if "bold" in props:
        changed |= _set_toggle(rpr, "b", props["bold"])
    if "italic" in props:
        changed |= _set_toggle(rpr, "i", props["italic"])
    if "small_caps" in props:
        changed |= _set_toggle(rpr, "smallCaps", props["small_caps"])
    if "underline" in props:
        changed |= _set_val(rpr, "u", "single" if props["underline"] else "none", _RPR_ORDER)
    if "font" in props:
        fonts, created = _child(rpr, W + "rFonts", _RPR_ORDER)
        name = props["font"]
        changed |= (
            _set_attrs(
                fonts,
                {W + k: name for k in ("ascii", "hAnsi", "cs", "eastAsia")},
                drop=_THEME_FONTS,
            )
            or created
        )
    if "size_pt" in props:
        half = str(round(props["size_pt"] * 2))
        changed |= _set_val(rpr, "sz", half, _RPR_ORDER)
        changed |= _set_val(rpr, "szCs", half, _RPR_ORDER)
    return changed


def apply_ppr(ppr: ET.Element, props: dict[str, Any]) -> bool:
    """Set paragraph properties (``PARAGRAPH_KEYS`` plus ``style``, a paragraph style id)."""
    changed = False
    if "style" in props:
        changed |= _set_val(ppr, "pStyle", props["style"], _PPR_ORDER)
    if "align" in props:
        changed |= _set_val(ppr, "jc", ALIGNMENTS[props["align"]], _PPR_ORDER)
    ind: dict[str, str] = {}
    ind_drop: tuple[str, ...] = ()
    if "indent_left_cm" in props:
        ind[W + "left"] = str(twips(props["indent_left_cm"]))
        ind_drop += (W + "start", W + "leftChars")
    if "indent_right_cm" in props:
        ind[W + "right"] = str(twips(props["indent_right_cm"]))
        ind_drop += (W + "end", W + "rightChars")
    if "indent_first_line_cm" in props:
        value = twips(props["indent_first_line_cm"])
        if value >= 0:
            ind[W + "firstLine"] = str(value)
            ind_drop += (W + "hanging", W + "hangingChars", W + "firstLineChars")
        else:
            ind[W + "hanging"] = str(-value)
            ind_drop += (W + "firstLine", W + "firstLineChars", W + "hangingChars")
    if ind:
        elem, created = _child(ppr, W + "ind", _PPR_ORDER)
        changed |= _set_attrs(elem, ind, drop=ind_drop) or created
    spacing: dict[str, str] = {}
    spacing_drop: tuple[str, ...] = ()
    if "spacing_before_pt" in props:
        spacing[W + "before"] = str(round(props["spacing_before_pt"] * 20))
        spacing_drop += (W + "beforeLines", W + "beforeAutospacing")
    if "spacing_after_pt" in props:
        spacing[W + "after"] = str(round(props["spacing_after_pt"] * 20))
        spacing_drop += (W + "afterLines", W + "afterAutospacing")
    if "line_spacing" in props:
        spacing[W + "line"] = str(round(props["line_spacing"] * 240))
        spacing[W + "lineRule"] = "auto"
    if spacing:
        elem, created = _child(ppr, W + "spacing", _PPR_ORDER)
        changed |= _set_attrs(elem, spacing, drop=spacing_drop) or created
    return changed
