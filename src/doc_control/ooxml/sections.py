"""Section properties as tracked changes (LAYOUT T3): the ``w:sectPr`` writer.

A section's page setup lives in the ``w:sectPr`` that closes it: inside the ``w:pPr`` of the
section's last paragraph, or, for the last section, as the body's own last child.
:func:`change_section` edits one such element and records the previous properties in a
``w:sectPrChange`` (ECMA-376 17.13.5.32: the last child, holding a ``w:sectPr`` with the old
page setup), so rejecting the revision (:func:`restore_section`) puts the old setup back and
accepting it (:func:`drop_change`) keeps the new one.

Properties (``props``)
    ``break``                 ``w:type``: nextPage, continuous, evenPage, oddPage, nextColumn;
    ``page_number_start``     ``w:pgNumType/@w:start`` (None: continue the numbering);
    ``page_number_format``    ``w:pgNumType/@w:fmt`` (None: back to the default, decimal);
    ``title_page``            ``w:titlePg`` (a different first page);
    ``margins``               ``w:pgMar`` attributes in twips (top, right, bottom, left,
                              header, footer, gutter);
    ``header_refs`` / ``footer_refs``
                              ``{kind: relationship id or None}`` for ``w:headerReference`` /
                              ``w:footerReference``; the schema has no place for them in
                              ``w:sectPrChange``, so Word cannot track them and they change
                              only when the caller allows an untracked change (D-L2).

A section break is a paragraph whose ``w:pPr`` holds the ``w:sectPr``. :func:`new_break`
builds one as a tracked inserted paragraph (its mark is a ``w:ins``) carrying a copy of the
setup of the section it splits, and :func:`mark_break_removed` tracks the deletion of an
existing one (its mark becomes a ``w:del``). Accepting a deleted break, or rejecting an
inserted one, removes the paragraph, so the text before it joins the section after it
(:mod:`doc_control.ooxml.resolve`).
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.revisions import DEL, INS, PPR, RPR, P, RevisionContext, W, unsupported
from doc_control.ooxml.splice import DocumentSpans

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
R_ID = f"{{{R_NS}}}id"
SECT_PR = W + "sectPr"
SECT_CHANGE = W + "sectPrChange"
HEADER_REF, FOOTER_REF = W + "headerReference", W + "footerReference"
REFS = (HEADER_REF, FOOTER_REF)
VAL = W + "val"

BREAK_TYPES = ("nextPage", "continuous", "evenPage", "oddPage", "nextColumn")
NUMBER_FORMATS = frozenset(
    {
        "decimal",
        "upperRoman",
        "lowerRoman",
        "upperLetter",
        "lowerLetter",
        "ordinal",
        "cardinalText",
        "ordinalText",
        "decimalZero",
        "none",
    }
)
HF_KINDS = ("default", "first", "even")
MARGIN_KEYS = ("top", "right", "bottom", "left", "header", "footer", "gutter")
PROP_KEYS = frozenset(
    {
        "break",
        "page_number_start",
        "page_number_format",
        "title_page",
        "margins",
        "header_refs",
        "footer_refs",
    }
)

_NAMES = """
headerReference footerReference footnotePr endnotePr type pgSz pgMar paperSrc pgBorders
lnNumType pgNumType cols formProt vAlign noEndnote titlePg textDirection bidi rtlGutter
docGrid printerSettings sectPrChange
"""
_ORDER = tuple(W + n for n in _NAMES.split())
_OFF = frozenset({"0", "false", "off"})


def _bad(message: str, **details: object) -> DocControlError:
    return DocControlError(ErrorCode.SCHEMA_INVALID, message, **details)


def check_props(props: Mapping[str, Any]) -> None:
    """Validate ``props``; raises ``SCHEMA_INVALID`` naming the first problem."""
    unknown = sorted(set(props) - PROP_KEYS)
    if unknown:
        raise _bad(f"unknown section properties: {', '.join(unknown)}", allowed=sorted(PROP_KEYS))
    if not props:
        raise _bad("no section properties given")
    value = props.get("break")
    if "break" in props and value not in BREAK_TYPES:
        raise _bad(f"break must be one of {', '.join(BREAK_TYPES)}", got=value)
    start = props.get("page_number_start")
    if (
        "page_number_start" in props
        and start is not None
        and (isinstance(start, bool) or not isinstance(start, int) or start < 0)
    ):
        raise _bad("page_number_start must be a non-negative integer or null", got=start)
    fmt = props.get("page_number_format")
    if "page_number_format" in props and fmt is not None and fmt not in NUMBER_FORMATS:
        raise _bad(f"page_number_format must be one of {sorted(NUMBER_FORMATS)}", got=fmt)
    if "title_page" in props and not isinstance(props["title_page"], bool):
        raise _bad("title_page must be true or false")
    margins = props.get("margins")
    if "margins" in props:
        if not isinstance(margins, Mapping) or not margins:
            raise _bad("margins must be an object of twip values")
        for key, twips in margins.items():
            if key not in MARGIN_KEYS or isinstance(twips, bool) or not isinstance(twips, int):
                raise _bad(f"margins.{key}: expected an integer of {', '.join(MARGIN_KEYS)}")
            if twips < 0:
                raise _bad(f"margins.{key} must not be negative")
    for group in ("header_refs", "footer_refs"):
        refs = props.get(group)
        if group in props:
            if not isinstance(refs, Mapping) or not refs:
                raise _bad(f"{group} must be an object of kind to relationship id")
            for kind, rid in refs.items():
                if kind not in HF_KINDS or not (rid is None or (isinstance(rid, str) and rid)):
                    raise _bad(f"{group}.{kind}: a kind of {HF_KINDS} and a relationship id")


def has_refs(props: Mapping[str, Any]) -> bool:
    return "header_refs" in props or "footer_refs" in props


# -- reading ---------------------------------------------------------------------------


def section_closers(spans: DocumentSpans) -> list[int | None]:
    """What closes each section, in document order: the index (in ``spans.paragraphs``) of
    the paragraph whose ``w:pPr`` holds the ``w:sectPr``, and ``None`` for the body's own."""
    out: list[int | None] = []
    for i, span in enumerate(spans.paragraphs):
        if span.nested or b"sectPr" not in spans.paragraph_bytes(i):
            continue
        if spans.parsed(i).find(f"{PPR}/{SECT_PR}") is not None:
            out.append(i)
    if spans.body_sect is not None:
        out.append(None)
    return out


def _flag_on(elem: ET.Element | None) -> bool:
    return elem is not None and (elem.get(VAL) or "1").lower() not in _OFF


def effective(sect: ET.Element) -> dict[str, Any]:
    """The properties :func:`change_section` can set, as they are in ``sect``."""
    kind = sect.find(W + "type")
    num = sect.find(W + "pgNumType")
    start = num.get(W + "start") if num is not None else None
    mar = sect.find(W + "pgMar")
    return {
        "break": (kind.get(VAL) if kind is not None else None) or "nextPage",
        "page_number_start": int(start) if start is not None and start.isdigit() else None,
        "page_number_format": (num.get(W + "fmt") if num is not None else None) or "decimal",
        "title_page": _flag_on(sect.find(W + "titlePg")),
        "margins": {
            k: int(v)
            for k in MARGIN_KEYS
            if mar is not None and (v := mar.get(W + k, "")).isdigit()
        },
    }


# -- writing ---------------------------------------------------------------------------


def _child(parent: ET.Element, tag: str) -> tuple[ET.Element, bool]:
    """The child ``tag`` of ``parent``, created in schema position if missing."""
    found = parent.find(tag)
    if found is not None:
        return found, False
    rank = _ORDER.index(tag)
    at = len(parent)
    for i, existing in enumerate(parent):
        if (
            isinstance(existing.tag, str)
            and existing.tag in _ORDER
            and _ORDER.index(existing.tag) > rank
        ):
            at = i
            break
    elem = ET.Element(tag)
    parent.insert(at, elem)
    return elem, True


def _set_break(work: ET.Element, value: str) -> bool:
    if effective(work)["break"] == value:
        return False
    elem, _ = _child(work, W + "type")
    elem.set(VAL, value)
    return True


def _set_numbering(work: ET.Element, attr: str, value: str | None, default: str | None) -> bool:
    num = work.find(W + "pgNumType")
    current = num.get(W + attr) if num is not None else None
    if (
        current == value
        or (value is None and current is None)
        or (value == default and current is None)
    ):
        return False
    if value is None:
        assert num is not None
        del num.attrib[W + attr]
        if not num.attrib:
            work.remove(num)
        return True
    num, _ = _child(work, W + "pgNumType")
    num.set(W + attr, value)
    return True


def _set_title_page(work: ET.Element, on: bool) -> bool:
    existing = work.find(W + "titlePg")
    if _flag_on(existing) == on:
        return False
    if on:
        elem, _ = _child(work, W + "titlePg")
        elem.attrib.pop(VAL, None)
    else:
        assert existing is not None
        work.remove(existing)
    return True


def _set_margins(work: ET.Element, margins: Mapping[str, int]) -> bool:
    mar = work.find(W + "pgMar")
    if mar is None:
        missing = [k for k in MARGIN_KEYS if k not in margins]
        if missing:
            raise unsupported(
                "the section has no page margins: give every margin to set them",
                missing=missing,
            )
        mar, _ = _child(work, W + "pgMar")
    changed = False
    for key, twips in margins.items():
        if mar.get(W + key) != str(twips):
            mar.set(W + key, str(twips))
            changed = True
    return changed


def _set_refs(work: ET.Element, tag: str, refs: Mapping[str, str | None]) -> bool:
    changed = False
    for kind, rid in refs.items():
        found = next(
            (r for r in work.findall(tag) if (r.get(W + "type") or "default") == kind), None
        )
        if rid is None:
            if found is not None:
                work.remove(found)
                changed = True
        elif found is None:
            at = max((i + 1 for i, c in enumerate(work) if c.tag in REFS), default=0)
            work.insert(at, ET.Element(tag, {W + "type": kind, R_ID: rid}))
            changed = True
        elif found.get(R_ID) != rid:
            found.set(R_ID, rid)
            changed = True
    return changed


def _apply(work: ET.Element, props: Mapping[str, Any], *, refs: bool) -> bool:
    changed = False
    if "break" in props:
        changed |= _set_break(work, props["break"])
    if "page_number_start" in props:
        start = props["page_number_start"]
        changed |= _set_numbering(work, "start", None if start is None else str(start), None)
    if "page_number_format" in props:
        changed |= _set_numbering(work, "fmt", props["page_number_format"], "decimal")
    if "title_page" in props:
        changed |= _set_title_page(work, props["title_page"])
    if "margins" in props:
        changed |= _set_margins(work, props["margins"])
    if refs:
        if "header_refs" in props:
            changed |= _set_refs(work, HEADER_REF, props["header_refs"])
        if "footer_refs" in props:
            changed |= _set_refs(work, FOOTER_REF, props["footer_refs"])
    return changed


def previous_setup(sect: ET.Element) -> ET.Element:
    """A ``w:sectPr`` holding ``sect``'s properties as ``w:sectPrChange`` records them:
    everything but the header/footer references and an earlier change."""
    previous = ET.Element(SECT_PR)
    previous.extend(copy.deepcopy(c) for c in sect if c.tag not in REFS and c.tag != SECT_CHANGE)
    return previous


def change_section(
    sect: ET.Element,
    props: Mapping[str, Any],
    ctx: RevisionContext | None,
    *,
    untracked: bool = False,
    where: Mapping[str, object] | None = None,
) -> bool:
    """Set ``props`` on ``sect`` in place; False if it already has them.

    With ``ctx`` the new properties come with a ``w:sectPrChange`` holding the old ones.
    Header and footer references cannot be recorded there, so they need ``untracked``
    (``UNSUPPORTED_CONTENT`` otherwise); with ``ctx=None`` nothing is recorded at all.
    A section with an unresolved ``w:sectPrChange`` is refused.
    """
    check_props(props)
    details = dict(where or {})
    if ctx is not None and has_refs(props) and not untracked:
        raise unsupported(
            "header and footer references cannot be tracked in a section property change; "
            "pass untracked: true",
            **details,
        )
    if ctx is not None and sect.find(SECT_CHANGE) is not None:
        raise unsupported("the section has an unresolved tracked property change", **details)
    work = copy.deepcopy(sect)
    if not _apply(work, props, refs=True):
        return False
    if ctx is not None and _apply(copy.deepcopy(sect), props, refs=False):
        change = ctx.revision("sectPrChange")
        change.append(previous_setup(sect))
        work.append(change)
    sect[:] = list(work)
    return True


def set_props(sect: ET.Element, props: Mapping[str, Any]) -> bool:
    """Set ``props`` on ``sect`` in place, without recording a change: for a ``w:sectPr``
    that is itself part of a tracked insertion. References are not touched."""
    check_props(props)
    return _apply(sect, props, refs=False)


def restore_section(sect: ET.Element, change: ET.Element) -> None:
    """Reject ``change`` (a ``w:sectPrChange`` of ``sect``): the recorded setup comes back.
    The header and footer references stay as they are, since the change cannot hold them."""
    old = change.find(SECT_PR)
    kept = [c for c in sect if c.tag in REFS]
    sect[:] = kept + ([copy.deepcopy(c) for c in old] if old is not None else [])


def drop_change(sect: ET.Element, change: ET.Element) -> None:
    """Accept ``change``: the current setup stays and the record goes."""
    sect.remove(change)


# -- section breaks --------------------------------------------------------------------


def new_break(source: ET.Element, ctx: RevisionContext) -> ET.Element:
    """A paragraph that ends a section, as a tracked insertion.

    Its ``w:sectPr`` is a copy of ``source``, the setup of the section being split, so the
    section before the break looks as the whole did. The paragraph is empty."""
    sect = ET.Element(SECT_PR)
    sect.extend(copy.deepcopy(c) for c in source if c.tag != SECT_CHANGE)
    p = ET.Element(P)
    ppr = ET.SubElement(p, PPR)
    ET.SubElement(ppr, RPR).append(ctx.revision("ins"))
    ppr.append(sect)
    return p


def middle_setup(source: ET.Element, break_type: str) -> ET.Element:
    """A copy of ``source`` for a section made by an earlier break of type ``break_type``:
    it starts as that break says, without the page-number restart or the title page."""
    work = copy.deepcopy(source)
    _apply(work, {"break": break_type, "page_number_start": None, "title_page": False}, refs=False)
    return work


def mark_break_removed(p: ET.Element, ctx: RevisionContext) -> None:
    """Track the removal of the section break that ends paragraph ``p``: its mark is
    deleted. (Accepting joins ``p`` with the next paragraph and the break goes.)"""
    ppr = p.find(PPR)
    if ppr is None or ppr.find(SECT_PR) is None:
        raise unsupported("the paragraph does not end a section")
    rpr = ppr.find(RPR)
    if rpr is None:
        rpr = ET.Element(RPR)
        ppr.insert([c.tag for c in ppr].index(SECT_PR), rpr)
    if any(c.tag in (INS, DEL) for c in rpr):
        raise unsupported("the section break already has a tracked insertion or deletion")
    rpr.insert(0, ctx.revision("del"))


__all__ = [
    "BREAK_TYPES",
    "SECT_CHANGE",
    "SECT_PR",
    "change_section",
    "check_props",
    "drop_change",
    "effective",
    "mark_break_removed",
    "middle_setup",
    "new_break",
    "previous_setup",
    "restore_section",
    "section_closers",
    "set_props",
]
