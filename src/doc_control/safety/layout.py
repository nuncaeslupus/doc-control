"""The layout footprint family (LAYOUT spec §5, safety contracts).

A layout write may change only:

* the ``w:sectPr`` of the sections it touches: inside the ``w:pPr`` of the paragraph that
  closes a section (that paragraph is in ``Footprint.replaced``) or, for the last section,
  the body's own ``w:sectPr`` (``Footprint.body_section``);
* the paragraphs that gain or lose a section break: a new empty paragraph holding the
  ``w:sectPr`` (``inserted_after``), or the mark of a break paragraph turned into a tracked
  deletion;
* the flags of ``word/settings.xml`` (``Footprint.settings``).

:func:`layout_footprint` builds that footprint; the write verifier then also runs
:func:`layout_problems` on it, which looks inside the touched paragraphs: nothing but the
``w:sectPr`` and the tracked marks of the paragraph mark may differ, and an inserted
paragraph must be empty. :func:`untracked_section_changes` is the plan's
``layout_untracked_changes`` metric: every change to a section's page setup must carry the
``w:sectPrChange`` that restores the old one, and every inserted break a tracked mark.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from doc_control.ooxml.sections import section_closers
from doc_control.ooxml.settingsflags import FLAGS
from doc_control.ooxml.splice import DocumentSpans, scan
from doc_control.ooxml.zipio import Package
from doc_control.safety.verify import DOCUMENT_PART, Footprint

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PPR, _RPR, _SECT, _CHANGE = W + "pPr", W + "rPr", W + "sectPr", W + "sectPrChange"
_REFS = (W + "headerReference", W + "footerReference")
_MARKS = (W + "ins", W + "del")


def layout_footprint(
    *,
    replaced: Iterable[int] = (),
    inserted_after: Iterable[int] = (),
    body_section: bool = False,
    settings: Iterable[str] = (),
) -> Footprint:
    """The footprint of a layout write; ``replaced`` and ``inserted_after`` are paragraph
    indices in the original ``word/document.xml``, ``settings`` names of settings flags."""
    unknown = sorted(set(settings) - set(FLAGS))
    if unknown:
        raise ValueError(f"not a layout setting: {', '.join(unknown)}")
    return Footprint(
        replaced=frozenset(replaced),
        inserted_after=frozenset(inserted_after),
        body_section=body_section,
        settings=frozenset(settings),
        layout=True,
    )


def _new_index(old: int, inserted_after: frozenset[int]) -> int:
    return old + sum(1 for j in inserted_after if j < old)


def _stripped(p: ET.Element) -> ET.Element:
    """``p`` without what a layout write may change: the paragraph's ``w:sectPr`` and the
    ``w:ins``/``w:del`` marks of its mark properties."""
    out = copy.deepcopy(p)
    ppr = out.find(_PPR)
    if ppr is not None:
        for sect in ppr.findall(_SECT):
            ppr.remove(sect)
        rpr = ppr.find(_RPR)
        if rpr is not None:
            for mark in [c for c in rpr if c.tag in _MARKS]:
                rpr.remove(mark)
            if len(rpr) == 0:
                ppr.remove(rpr)
        if len(ppr) == 0 and not ppr.attrib:
            out.remove(ppr)
    return out


def _same(a: ET.Element, b: ET.Element) -> bool:
    return bool(ET.tostring(a) == ET.tostring(b))


def layout_problems(original: Package, result: Package, footprint: Footprint) -> list[str]:
    """What changed inside the touched paragraphs beyond a section break, empty when the
    document differs from the original as a layout write allows."""
    old = scan(original.read(DOCUMENT_PART))
    new = scan(result.read(DOCUMENT_PART))
    inserted = footprint.inserted_after
    if len(new.paragraphs) != len(old.paragraphs) + len(inserted):
        return ["a layout write changed the paragraph count"]
    problems: list[str] = []
    for i in sorted(footprint.replaced):
        before = old.parsed(i)
        after = new.parsed(_new_index(i, inserted))
        if not _same(_stripped(before), _stripped(after)):
            problems.append(f"paragraph {i}: more than its section break changed")
    for j in sorted(inserted):
        at = _new_index(j, inserted) + 1
        p = new.parsed(at)
        ppr = p.find(_PPR)
        kids = [c for c in p if isinstance(c.tag, str)]
        if (
            len(kids) != 1
            or ppr is None
            or ppr.find(_SECT) is None
            or any(c.tag not in (_RPR, _SECT) for c in ppr)
        ):
            problems.append(f"inserted paragraph {at}: not an empty section break")
    return problems


# -- the metric -----------------------------------------------------------------------------


def _canon(elem: ET.Element) -> str:
    return ET.tostring(elem, encoding="unicode")


def _setup(sect: ET.Element) -> list[str]:
    """A section's page setup as comparable text: all but references and a change record."""
    return [_canon(c) for c in sect if c.tag not in _REFS and c.tag != _CHANGE]


def _refs(sect: ET.Element) -> list[str]:
    return [_canon(c) for c in sect if c.tag in _REFS]


def _closers(spans: DocumentSpans) -> list[tuple[int | None, ET.Element]]:
    """Every section's ``w:sectPr`` with its paragraph index (None: the body's)."""
    out: list[tuple[int | None, ET.Element]] = []
    for i in section_closers(spans):
        if i is None:
            out.append((None, spans.parse_body_sect()))
        else:
            sect = spans.parsed(i).find(f"{_PPR}/{_SECT}")
            assert sect is not None
            out.append((i, sect))
    return out


def untracked_section_changes(
    original: bytes,
    result: bytes,
    inserted_after: Iterable[int] = (),
    *,
    allow_refs: bool = False,
) -> list[str]:
    """Section changes in ``result`` that Word would not show as tracked
    (``layout_untracked_changes``); both arguments are ``word/document.xml`` bytes.

    Each section of the original must be as it was, or carry a ``w:sectPrChange`` whose
    record is its old setup; each paragraph in ``inserted_after`` (indices in the original)
    must be followed by an empty section break whose mark is a ``w:ins``. Header and footer
    references cannot be recorded, so a change there counts unless ``allow_refs``.
    """
    old, new = scan(original), scan(result)
    inserted = frozenset(inserted_after)
    problems: list[str] = []
    new_by_index = dict(_closers(new))
    for i, before in _closers(old):
        label = "the last section" if i is None else f"the section closed by paragraph {i}"
        after = new_by_index.get(None if i is None else _new_index(i, inserted))
        if after is None:
            problems.append(f"{label} is gone: a break is removed by a tracked deletion")
            continue
        if _setup(before) != _setup(after):
            change = after.find(_CHANGE)
            record = change.find(_SECT) if change is not None else None
            if record is None or [_canon(c) for c in record] != _setup(before):
                problems.append(f"{label} changed without a w:sectPrChange that restores it")
        if not allow_refs and _refs(before) != _refs(after):
            problems.append(f"{label}: header or footer references changed untracked")
    for j in sorted(inserted):
        at = _new_index(j, inserted) + 1
        rpr = new.parsed(at).find(f"{_PPR}/{_RPR}")
        if rpr is None or rpr.find(W + "ins") is None:
            problems.append(f"the section break after paragraph {j} is not a tracked insertion")
    return problems
