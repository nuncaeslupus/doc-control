"""Building and editing ``w:style`` elements of ``word/styles.xml``.

Styles are edited as text: the styles part is split into its ``w:style`` blocks, the
one block that changes is parsed, edited in order (children keep the schema
sequence) and put back, so every other byte of the part stays as it was. The
property vocabulary is the one :mod:`doc_control.model.format` reads: run keys
``bold italic small_caps strike underline font size_pt color highlight
vertical_align style``; paragraph keys ``alignment indent{left right first_line
hanging} spacing{before after line line_rule} keep_next keep_lines
page_break_before outline_level`` (lengths in twips).
"""

from __future__ import annotations

import contextlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import quoteattr, unescape

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.parts import W_NS, W

STYLES_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

STYLE_ORDER = [
    "name",
    "aliases",
    "basedOn",
    "next",
    "link",
    "autoRedefine",
    "hidden",
    "uiPriority",
    "semiHidden",
    "unhideWhenUsed",
    "qFormat",
    "locked",
    "personal",
    "personalCompose",
    "personalReply",
    "rsid",
    "pPr",
    "rPr",
    "tblPr",
    "trPr",
    "tcPr",
    "tblStylePr",
]
PPR_ORDER = [
    "pStyle",
    "keepNext",
    "keepLines",
    "pageBreakBefore",
    "framePr",
    "widowControl",
    "numPr",
    "suppressLineNumbers",
    "pBdr",
    "shd",
    "tabs",
    "suppressAutoHyphens",
    "kinsoku",
    "wordWrap",
    "overflowPunct",
    "topLinePunct",
    "autoSpaceDE",
    "autoSpaceDN",
    "bidi",
    "adjustRightInd",
    "snapToGrid",
    "spacing",
    "ind",
    "contextualSpacing",
    "mirrorIndents",
    "suppressOverlap",
    "jc",
    "textDirection",
    "textAlignment",
    "textboxTightWrap",
    "outlineLvl",
    "divId",
    "cnfStyle",
    "rPr",
    "sectPr",
    "pPrChange",
]
RPR_ORDER = [
    "rStyle",
    "rFonts",
    "b",
    "bCs",
    "i",
    "iCs",
    "caps",
    "smallCaps",
    "strike",
    "dstrike",
    "outline",
    "shadow",
    "emboss",
    "imprint",
    "noProof",
    "snapToGrid",
    "vanish",
    "webHidden",
    "color",
    "spacing",
    "w",
    "kern",
    "position",
    "sz",
    "szCs",
    "highlight",
    "u",
    "effect",
    "bdr",
    "shd",
    "fitText",
    "vertAlign",
    "rtl",
    "cs",
    "em",
    "lang",
    "eastAsianLayout",
    "specVanish",
    "oMath",
]

RUN_KEYS = [
    "bold",
    "italic",
    "small_caps",
    "strike",
    "underline",
    "font",
    "size_pt",
    "color",
    "highlight",
    "vertical_align",
    "style",
]
PARA_KEYS = [
    "alignment",
    "indent",
    "spacing",
    "keep_next",
    "keep_lines",
    "page_break_before",
    "outline_level",
]
_RUN_TOGGLES = {"bold": "b", "italic": "i", "small_caps": "smallCaps", "strike": "strike"}
_PARA_TOGGLES = {
    "keep_next": "keepNext",
    "keep_lines": "keepLines",
    "page_break_before": "pageBreakBefore",
}
_VAL_RUN = {
    "underline": "u",
    "color": "color",
    "highlight": "highlight",
    "vertical_align": "vertAlign",
    "style": "rStyle",
}
_INDENT = {"left": "left", "right": "right", "first_line": "firstLine", "hanging": "hanging"}
_SPACING = {"before": "before", "after": "after", "line": "line", "line_rule": "lineRule"}
# Local names of the children the vocabulary above owns (cleared on a full replace).
_RUN_OWNED = {*_RUN_TOGGLES.values(), *_VAL_RUN.values(), "rFonts", "sz", "szCs"}
_PARA_OWNED = {*_PARA_TOGGLES.values(), "jc", "ind", "spacing", "outlineLvl"}


def invalid(message: str, **details: Any) -> DocControlError:
    return DocControlError(ErrorCode.SCHEMA_INVALID, message, **details)


def _local(elem: ET.Element) -> str:
    return elem.tag.rpartition("}")[2]


def _put(parent: ET.Element, local: str, order: list[str], attrs: dict[str, str] | None) -> None:
    """Replace the ``local`` child of ``parent`` (``attrs`` None: just remove it)."""
    for old in [c for c in parent if _local(c) == local]:
        parent.remove(old)
    if attrs is None:
        return
    new = ET.Element(W + local, {W + k: v for k, v in attrs.items()})
    rank = order.index(local)
    at = len(parent)
    for i, child in enumerate(parent):
        name = _local(child)
        if name in order and order.index(name) > rank:
            at = i
            break
    parent.insert(at, new)


def _check_bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise invalid(f"{key} must be true or false")
    return value


def _toggle(key: str, value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    return {} if _check_bool(key, value) else {"val": "0"}


def _check_str(key: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise invalid(f"{key} must be a non-empty string")
    return value


def _check_int(key: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid(f"{key} must be an integer (twips)")
    return value


def _sub(patch: dict[str, Any], key: str, allowed: dict[str, str]) -> dict[str, Any]:
    value = patch[key]
    if not isinstance(value, dict):
        raise invalid(f"{key} must be an object")
    extra = sorted(set(value) - set(allowed))
    if extra:
        raise invalid(f"unknown {key} key {extra[0]!r}", allowed=sorted(allowed))
    return value


def apply_run(rpr: ET.Element, patch: dict[str, Any]) -> None:
    """Set (or, for a ``None`` value, remove) the run properties in ``patch``."""
    unknown = sorted(set(patch) - set(RUN_KEYS))
    if unknown:
        raise invalid(f"unknown run key {unknown[0]!r}", allowed=RUN_KEYS)
    order = RPR_ORDER
    for key, value in patch.items():
        if key in _RUN_TOGGLES:
            local = _RUN_TOGGLES[key]
            _put(rpr, local, order, _toggle(key, value))
        elif key in _VAL_RUN:
            local = _VAL_RUN[key]
            _put(rpr, local, order, None if value is None else {"val": _check_str(key, value)})
        elif key == "font":
            _put(rpr, "rFonts", order, None)
            if value is not None:
                name = _check_str(key, value)
                _put(rpr, "rFonts", order, {"ascii": name, "hAnsi": name})
        elif key == "size_pt":
            for local in ("sz", "szCs"):
                if value is None:
                    _put(rpr, local, order, None)
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                    raise invalid("size_pt must be a positive number")
                half = value * 2
                if half != int(half):
                    raise invalid("size_pt must be a multiple of 0.5")
                _put(rpr, local, order, {"val": str(int(half))})


def apply_para(ppr: ET.Element, patch: dict[str, Any]) -> None:
    """Set (or remove, for ``None``) the paragraph properties in ``patch``."""
    unknown = sorted(set(patch) - set(PARA_KEYS))
    if unknown:
        raise invalid(f"unknown paragraph key {unknown[0]!r}", allowed=PARA_KEYS)
    order = PPR_ORDER
    for key, value in patch.items():
        if key in _PARA_TOGGLES:
            local = _PARA_TOGGLES[key]
            _put(ppr, local, order, _toggle(key, value))
        elif key == "alignment":
            _put(ppr, "jc", order, None if value is None else {"val": _check_str(key, value)})
        elif key == "outline_level":
            if value is not None and (_check_int(key, value) < 0 or value > 9):
                raise invalid("outline_level must be 0-9")
            _put(ppr, "outlineLvl", order, None if value is None else {"val": str(value)})
        else:  # indent, spacing
            names = _INDENT if key == "indent" else _SPACING
            local = "ind" if key == "indent" else "spacing"
            if value is None:
                _put(ppr, local, order, None)
                continue
            attrs: dict[str, str] = {}
            for sub, raw in _sub(patch, key, names).items():
                if raw is None:
                    continue
                attrs[names[sub]] = (
                    _check_str(sub, raw) if sub == "line_rule" else str(_check_int(sub, raw))
                )
            _put(ppr, local, order, attrs or None)


def _owned_clear(elem: ET.Element, owned: set[str]) -> None:
    for child in [c for c in elem if _local(c) in owned]:
        elem.remove(child)


def apply_properties(
    style: ET.Element, run: dict[str, Any] | None, para: dict[str, Any] | None, *, replace: bool
) -> None:
    """Edit the ``w:rPr`` / ``w:pPr`` of ``style``; ``replace`` drops the old values."""
    for local, patch, owned, apply in (
        ("pPr", para, _PARA_OWNED, apply_para),
        ("rPr", run, _RUN_OWNED, apply_run),
    ):
        if patch is None:
            continue
        if not isinstance(patch, dict):
            raise invalid(f"{'paragraph' if local == 'pPr' else 'run'} must be an object")
        props = style.find(W + local)
        if props is None:
            props = ET.Element(W + local)
            _put_elem(style, props)
        if replace:
            _owned_clear(props, owned)
        apply(props, patch)
        if len(props) == 0 and not props.attrib:
            style.remove(props)


def _put_elem(style: ET.Element, new: ET.Element) -> None:
    rank = STYLE_ORDER.index(_local(new))
    at = len(style)
    for i, child in enumerate(style):
        name = _local(child)
        if name in STYLE_ORDER and STYLE_ORDER.index(name) > rank:
            at = i
            break
    style.insert(at, new)


def set_child(style: ET.Element, local: str, val: str | None, *, flag: bool = False) -> None:
    """Set a ``w:name`` / ``basedOn`` / ``next`` / ``link`` / ``qFormat`` child (None removes)."""
    for old in [c for c in style if _local(c) == local]:
        style.remove(old)
    if val is None:
        return
    _put_elem(style, ET.Element(W + local, {} if flag else {W + "val": val}))


def new_style(
    style_id: str, name: str, kind: str, *, default: bool = False, custom: bool = False
) -> ET.Element:
    attrs = {W + "type": kind}
    if default:
        attrs[W + "default"] = "1"
    if custom:
        attrs[W + "customStyle"] = "1"
    attrs[W + "styleId"] = style_id
    style = ET.Element(W + "style", attrs)
    set_child(style, "name", name)
    return style


@dataclass(frozen=True)
class Rec:
    """The identity fields of one style, as the editor sees them."""

    id: str
    name: str
    type: str
    based_on: str | None
    next: str | None
    link: str | None
    default: bool


class StylesText:
    """The text of a ``styles.xml`` with block-level edits."""

    def __init__(self, data: bytes | None) -> None:
        self.created = data is None
        if data is None:
            self.text = f'{_XML_DECL}<w:styles xmlns:w="{W_NS}"></w:styles>'
        else:
            try:
                self.text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DocControlError(
                    ErrorCode.UNSUPPORTED_CONTENT, "styles.xml is not UTF-8 encoded"
                ) from exc
            if self.text.startswith("﻿"):
                self.text = self.text[1:]
        m = re.search(r"<([\w.-]+):styles\b([^>]*?)(/?)>", self.text)
        if m is None:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT, "styles.xml has no prefixed w:styles root"
            )
        self.prefix = m.group(1)
        self.decls = dict(re.findall(r'xmlns:([\w.-]+)="([^"]*)"', m.group(2)))
        if self.decls.get(self.prefix) != W_NS:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                "styles.xml root is not in the WordprocessingML namespace",
            )
        if m.group(3):  # <w:styles .../>
            open_tag = f"<{self.prefix}:styles{m.group(2)}>"
            self.text = (
                self.text[: m.start()]
                + open_tag
                + f"</{self.prefix}:styles>"
                + self.text[m.end() :]
            )
        self.open_tag = (
            self.text[self.text.index(f"<{self.prefix}:styles") :].split(">", 1)[0] + ">"
        )
        for prefix, uri in self.decls.items():
            with contextlib.suppress(ValueError):
                ET.register_namespace(prefix, uri)
        p = re.escape(self.prefix)
        self._block = re.compile(rf"<{p}:style\b([^>]*?)(?:/>|>(.*?)</{p}:style>)", re.DOTALL)
        self._id = re.compile(rf'\b{p}:styleId="([^"]*)"')

    # -- reading ---------------------------------------------------------

    def _spans(self) -> list[tuple[int, int, str]]:
        out = []
        for m in self._block.finditer(self.text):
            sid = self._id.search(m.group(1))
            if sid:
                out.append((m.start(), m.end(), unescape(sid.group(1), {"&quot;": '"'})))
        return out

    def records(self) -> dict[str, Rec]:
        root = ET.fromstring(self.text.encode("utf-8"))
        out: dict[str, Rec] = {}
        for e in root.findall(W + "style"):
            sid = e.get(W + "styleId")
            if not sid:
                continue

            def val(tag: str, e: ET.Element = e) -> str | None:
                c = e.find(W + tag)
                return c.get(W + "val") if c is not None else None

            out[sid] = Rec(
                sid,
                val("name") or sid,
                e.get(W + "type") or "paragraph",
                val("basedOn"),
                val("next"),
                val("link"),
                (e.get(W + "default") or "0").lower() in ("1", "true", "on"),
            )
        return out

    def element(self, style_id: str) -> ET.Element:
        for start, end, sid in self._spans():
            if sid == style_id:
                root = ET.fromstring(
                    (self.open_tag + self.text[start:end] + f"</{self.prefix}:styles>").encode(
                        "utf-8"
                    )
                )
                return root[0]
        raise DocControlError(ErrorCode.STYLE_NOT_FOUND, f"no style {style_id!r}", style=style_id)

    # -- writing ---------------------------------------------------------

    def serialize(self, elem: ET.Element) -> str:
        out = ET.tostring(elem, encoding="unicode")
        head, sep, rest = out.partition(">")
        head = re.sub(
            r' xmlns:([\w.-]+)="([^"]*)"',
            lambda m: "" if self.decls.get(m.group(1)) == m.group(2) else m.group(0),
            head,
        )
        return head + sep + rest

    def put(self, elem: ET.Element, replacing: str | None = None) -> None:
        """Replace the block of ``replacing`` (default: the element's own id), or append it."""
        sid = replacing or elem.get(W + "styleId") or ""
        block = self.serialize(elem)
        for start, end, found in self._spans():
            if found == sid:
                self.text = self.text[:start] + block + self.text[end:]
                return
        close = f"</{self.prefix}:styles>"
        at = self.text.rindex(close)
        self.text = self.text[:at] + block + self.text[at:]

    def remove(self, style_id: str) -> None:
        for start, end, sid in self._spans():
            if sid == style_id:
                self.text = self.text[:start] + self.text[end:]
                return

    def retarget(self, old: str, new: str | None, tags: str = "basedOn|next|link") -> None:
        """Point the ``tags`` references at ``new`` where they name ``old`` (None: drop them)."""
        p = re.escape(self.prefix)
        pat = re.compile(rf"<{p}:({tags})\b[^>]*?\b{p}:val={quoteattr(old)}[^>]*?/>")
        if new is None:
            self.text = pat.sub("", self.text)
        else:
            self.text = pat.sub(
                lambda m: f"<{self.prefix}:{m.group(1)} {self.prefix}:val={quoteattr(new)}/>",
                self.text,
            )

    def data(self) -> bytes:
        return self.text.encode("utf-8")
