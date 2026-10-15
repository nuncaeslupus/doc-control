"""Layout facts for `para.get` and `doc.outline` (D-18), registered through the
:data:`doc_control.ops.read.PARA_FACTS` and :data:`doc_control.ops.read.OUTLINE_FACTS` hooks.

The definitions live in :mod:`doc_control.model.layout`.
"""

from __future__ import annotations

import re
import tempfile
import xml.etree.ElementTree as ET
from functools import partial
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.layout import blank_before, centred, is_empty, page_break_before
from doc_control.model.parts import W, parse_part
from doc_control.model.stories import HF_KINDS, list_stories
from doc_control.ooxml.hftext import free_part_names, free_rids
from doc_control.ooxml.revisions import DEFAULT_AUTHOR, unsupported
from doc_control.ooxml.sections import BREAK_TYPES, check_props
from doc_control.ops.read import OUTLINE_FACTS, PARA_FACTS, ParagraphContext
from doc_control.ops.sections import SETTINGS_PART, flag, section_rows
from doc_control.ops.sectionwrite import LayoutWriteResult, apply_layout_edits
from doc_control.registry import operation


def para_facts(ctx: ParagraphContext) -> dict[str, Any]:
    doc, para = ctx.doc, ctx.paragraph
    return {
        "empty": is_empty(doc, para),
        "blank_before": blank_before(doc, para),
        "heading_level": doc.heading_level(para),
        "centred": centred(doc, para),
        "page_break_before": page_break_before(doc, para),
    }


def outline_facts(ctx: ParagraphContext) -> dict[str, Any]:
    """Outline entries already carry ``level`` and ``style``; add the rest."""
    doc, para = ctx.doc, ctx.paragraph
    style = doc.style_id(para)
    style_def = doc.styles.get(style) if style is not None else None
    return {
        "style_name": style_def.name if style_def is not None else None,
        "blank_before": blank_before(doc, para),
        "centred": centred(doc, para),
        "page_break_before": page_break_before(doc, para),
    }


PARA_FACTS.append(para_facts)
OUTLINE_FACTS.append(outline_facts)


# --- layout.get: the page plan (LAYOUT T2) -------------------------------------------------

_PLAN_VERSION = 1


def _local(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1]


def _placeholder(instr: str) -> str | None:
    """``{PAGE}``, ``{NUMPAGES}`` or ``{chapter}`` for a field code, else ``None``."""
    words = instr.split(None, 1)
    if not words:
        return None
    code = words[0].upper()
    if code in ("PAGE", "NUMPAGES"):
        return "{" + code + "}"
    if code == "STYLEREF" and len(words) > 1:
        arg = re.match(r'\s*(?:"([^"]*)"|(\S+))', words[1])
        name = ((arg.group(1) or arg.group(2)) if arg else "").strip().lower()
        if name in ("heading 1", "1"):
            return "{chapter}"
    return None


class _Text:
    """The text of one header or footer paragraph, its recognised fields as placeholders."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.fields: list[dict[str, Any]] = []

    def emit(self, text: str) -> None:
        if not self.fields:
            self.out.append(text)
        elif self.fields[-1]["result"]:
            self.fields[-1]["buf"].append(text)

    def field_end(self) -> None:
        if self.fields:
            top = self.fields.pop()
            found = _placeholder("".join(top["instr"]))
            self.emit(found if found is not None else "".join(top["buf"]))

    def walk(self, parent: ET.Element) -> None:
        for child in parent:
            name = _local(child)
            if name in (
                "del",
                "delText",
                "pPr",
                "rPr",
                "moveFrom",
                "txbxContent",
                "sdtPr",
                "sdtEndPr",
            ):
                continue
            if name == "AlternateContent":
                branches = [c for c in child if _local(c) in ("Choice", "Fallback")]
                branches.sort(key=lambda c: _local(c) != "Choice")  # Choice first
                if branches:
                    self.walk(branches[0])
                continue
            if name == "fldSimple":
                found = _placeholder(child.get(W + "instr", ""))
                if found is not None:
                    self.emit(found)
                else:
                    self.walk(child)
            elif name == "fldChar":
                kind = child.get(W + "fldCharType")
                if kind == "begin":
                    self.fields.append({"instr": [], "buf": [], "result": False})
                elif kind == "separate" and self.fields:
                    self.fields[-1]["result"] = True
                elif kind == "end":
                    self.field_end()
            elif name == "instrText":
                if self.fields and not self.fields[-1]["result"]:
                    self.fields[-1]["instr"].append(child.text or "")
            elif name == "t":
                self.emit(child.text or "")
            elif name == "tab":
                self.emit("\t")
            elif name == "cr" or (name == "br" and child.get(W + "type") not in ("page", "column")):
                self.emit("\n")  # as the body index reads them
            else:
                self.walk(child)


def _paragraphs(parent: ET.Element) -> list[ET.Element]:
    """The story's paragraphs in order: also those in content controls (Word's page-number
    footers) and table cells, never text-box content."""
    out: list[ET.Element] = []
    for child in parent:
        name = _local(child)
        if name == "p":
            out.append(child)
        elif name in ("sdt", "sdtContent", "tbl", "tr", "tc"):
            out += _paragraphs(child)
    return out


def _story_text(root: ET.Element) -> str:
    paragraphs = []
    for para in _paragraphs(root):
        text = _Text()
        text.walk(para)
        while text.fields:  # a field left open by a truncated part
            text.field_end()
        paragraphs.append("".join(text.out))
    return "\n".join(paragraphs)


def _paragraph_starts(doc: Document, count: int) -> list[int]:
    """The 1-based paragraph number each section starts at (the first at 1)."""
    total = len(doc.index)
    starts = [1]
    for number in range(1, total + 1):
        if len(starts) >= count:
            break
        elem = doc.paragraph_element(doc.index.paragraph(number))
        if elem.find(f"{W}pPr/{W}sectPr") is not None:
            starts.append(min(number + 1, total))
    while len(starts) < count:
        starts.append(total)
    return starts


@operation(
    "layout.get",
    "read",
    "The document's page plan: per section where it starts, its break, page numbering, "
    "title page, and the header and footer text per kind (default, first, even), with PAGE, "
    "NUMPAGES and chapter (STYLEREF) fields as {PAGE}, {NUMPAGES} and {chapter}. A kind the "
    'section does not define is null (inherited). The plan carries "version": 1.',
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def layout_get(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    return Reply(page_plan(doc), doc=doc.envelope_doc())


def page_plan(doc: Document) -> dict[str, Any]:
    """The document's page plan, as `layout.get` returns it."""
    pkg = doc.pkg
    rows = section_rows(doc)
    mirror = False
    if SETTINGS_PART in pkg:
        mirror = flag(parse_part(pkg, SETTINGS_PART), "mirrorMargins")
    stories = {s.key: s for s in list_stories(pkg, ("headers",))}
    texts: dict[str, str] = {}

    def text_of(key: str) -> str:
        if key not in texts:
            texts[key] = _story_text(parse_part(pkg, stories[key].part))
        return texts[key]

    sections: list[dict[str, Any]] = []
    for row, start in zip(rows, _paragraph_starts(doc, len(rows)), strict=True):
        number = row["section"]
        active = {"default"}
        if row["title_page"]:
            active.add("first")
        if row["even_and_odd"]:
            active.add("even")
        shown = False
        plan: dict[str, dict[str, str | None]] = {}
        for group, which in (("header", "headers"), ("footer", "footers")):
            values: dict[str, str | None] = {}
            for kind in HF_KINDS:
                key = row[which][kind]
                values[kind] = None
                if key is None:
                    continue
                if stories[key].section == number:
                    values[kind] = text_of(key)
                if kind in active and "{PAGE}" in text_of(key):
                    shown = True
            plan[group] = values
        sections.append(
            {
                "from": str(doc.index.paragraph(start).address),
                "break": row["break"],
                "numbering": {
                    "shown": shown,
                    "start": row["page_numbers"]["start"],
                    "format": row["page_numbers"]["format"],
                },
                "header": plan["header"],
                "footer": plan["footer"],
                "first_page_different": row["title_page"],
            }
        )
    return {
        "version": _PLAN_VERSION,
        "two_sided": bool(rows and rows[0]["even_and_odd"]),
        "mirror_margins": mirror,
        "sections": sections,
    }


# --- layout.apply: breaks and numbering (LAYOUT T4) ----------------------------------------

_BREAKS = tuple(b for b in BREAK_TYPES if b != "nextColumn")  # the plan has no column breaks
_PLAN_KEYS = {"version", "two_sided", "mirror_margins", "sections"}
_SECTION_KEYS = {"from", "break", "numbering", "header", "footer", "first_page_different"}
#: What a part split off a section does not take from it: the break that now starts it is a
#: page break, and a page-number start or a different first page would repeat at the break
#: (the writer drops them there). The rest of the source section's setup (the number format)
#: carries over, so a key the plan leaves out stays as the pages had it.
_SPLIT_RESETS = {
    "break": "nextPage",
    "page_number_start": None,
    "title_page": False,
}
_OMITTED: Any = object()


def _bad_plan(message: str, **details: object) -> DocControlError:
    return DocControlError(ErrorCode.SCHEMA_INVALID, message, **details)


def _check_plan(plan: dict[str, Any]) -> None:
    """Refuse what is not a version-1 page plan. A key left out means "leave it as it is"."""
    if plan.get("version") != _PLAN_VERSION:
        raise _bad_plan(f"plan.version must be {_PLAN_VERSION}", got=plan.get("version"))
    unknown = sorted(set(plan) - _PLAN_KEYS)
    if unknown:
        raise _bad_plan(f"plan: unknown keys {', '.join(unknown)}")
    for key in ("two_sided", "mirror_margins"):
        if key in plan and not isinstance(plan[key], bool):
            raise _bad_plan(f"plan.{key} must be true or false")
    sections = plan.get("sections")
    if not isinstance(sections, list) or not sections:
        raise _bad_plan("plan.sections must be a non-empty list")
    for n, sect in enumerate(sections):
        where = f"plan.sections[{n}]"
        if not isinstance(sect, dict) or not isinstance(sect.get("from"), str):
            raise _bad_plan(f"{where} needs a 'from' address")
        unknown = sorted(set(sect) - _SECTION_KEYS)
        if unknown:
            raise _bad_plan(f"{where}: unknown keys {', '.join(unknown)}")
        if "break" in sect and sect["break"] not in _BREAKS:
            raise _bad_plan(f"{where}.break must be one of {', '.join(_BREAKS)}")
        if "first_page_different" in sect and not isinstance(sect["first_page_different"], bool):
            raise _bad_plan(f"{where}.first_page_different must be true or false")
        num = sect.get("numbering", {})
        if not isinstance(num, dict) or set(num) - {"shown", "start", "format"}:
            raise _bad_plan(f"{where}.numbering takes shown, start and format")
        if "shown" in num and not isinstance(num["shown"], bool):
            raise _bad_plan(f"{where}.numbering.shown must be true or false")
        numbering = {"page_number_" + k: num[k] for k in ("start", "format") if k in num}
        if numbering:
            check_props(numbering)
        for group in ("header", "footer"):
            texts = sect.get(group, {})
            if not isinstance(texts, dict) or set(texts) - set(HF_KINDS):
                raise _bad_plan(f"{where}.{group} takes {', '.join(HF_KINDS)}")
            if any(v is not None and not isinstance(v, str) for v in texts.values()):
                raise _bad_plan(f"{where}.{group} values are text or null")


def _row_props(row: dict[str, Any]) -> dict[str, Any]:
    """A `section.list` row as the section properties the writer takes."""
    return {
        "break": row["break"],
        "page_number_start": row["page_numbers"]["start"],
        "page_number_format": row["page_numbers"]["format"],
        "title_page": row["title_page"],
    }


def _wanted_props(sect: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """The properties the plan section asks for; what it leaves out stays as in ``base``."""
    num = sect.get("numbering", {})
    asked = {
        "break": sect.get("break", _OMITTED),
        "page_number_start": num.get("start", _OMITTED),
        "page_number_format": num.get("format", _OMITTED),
        "title_page": sect.get("first_page_different", _OMITTED),
    }
    return {k: base[k] if v is _OMITTED else v for k, v in asked.items()}


def _plan_edits(
    doc: Document, plan: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[tuple[str, int]]]:
    """The section edits (as :func:`apply_layout_edits` takes them) that bring the
    document's breaks and numbering to ``plan``, and per plan section where its properties
    go: ``("section", n)`` for section ``n`` of the document as it is, ``("break", k)`` for
    the break ``edits[k]`` ends it with."""
    rows = section_rows(doc)
    old_starts = _paragraph_starts(doc, len(rows))
    wanted = [(doc.index.resolve_address(s["from"]).number, s) for s in plan["sections"]]
    starts = [n for n, _ in wanted]
    if starts[0] != 1 or starts != sorted(set(starts)):
        raise _bad_plan(
            "plan.sections must start at paragraph 1, then at increasing, distinct paragraphs"
        )
    gone = [n for n in old_starts if n not in starts]
    if gone:
        raise unsupported(
            "layout.apply does not remove section breaks: keep the section that starts at each of "
            + ", ".join(str(doc.index.paragraph(n).address) for n in gone)
        )
    edits: list[dict[str, Any]] = []
    targets: list[tuple[str, int]] = []
    ends = [*old_starts[1:], len(doc.index) + 1]
    for number, (row, start, end) in enumerate(zip(rows, old_starts, ends, strict=True), 1):
        # The section's first part keeps its place; later parts are new breaks, and the
        # last part keeps the section's own closing sectPr.
        parts = [s for n, s in wanted if start <= n < end]
        split = {**_row_props(row), **_SPLIT_RESETS}
        for k, sect in enumerate(parts):
            props = _wanted_props(sect, _row_props(row) if k == 0 else split)
            if len(parts) == 1:
                targets.append(("section", number))
                props = {key: v for key, v in props.items() if v != _row_props(row)[key]}
                if props:
                    edits.append({"kind": "section.props", "section": number, "props": props})
            elif k + 1 == len(parts):
                targets.append(("section", number))
                edits.append({"kind": "section.props", "section": number, "props": props})
            else:
                targets.append(("break", len(edits)))
                following = parts[k + 1]
                at = doc.index.resolve_address(following["from"]).number - 1
                edits.append(
                    {
                        "kind": "section.break",
                        "after": str(doc.index.paragraph(at).address),
                        "type": _wanted_props(following, split)["break"],
                        "props": props,
                    }
                )
    return edits, targets


def _differences(want: Any, got: Any, path: str = "plan") -> list[str]:
    """Where ``got`` differs from ``want``, looking only at the keys ``want`` has."""
    if isinstance(want, dict) and isinstance(got, dict):
        return [
            d
            for key, value in want.items()
            for d in _differences(value, got.get(key, _OMITTED), f"{path}.{key}")
        ]
    if isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            return [f"{path}: {len(got)} entries, not {len(want)}"]
        return [
            d
            for i, (w, g) in enumerate(zip(want, got, strict=True))
            for d in _differences(w, g, f"{path}[{i}]")
        ]
    return [] if want == got else [f"{path}: {got!r}, not {want!r}"]


# --- layout.apply: headers, footers, two-sided (LAYOUT T5) --------------------------------

_Refs = dict[str, dict[str, str | None]]  # "header_refs"/"footer_refs" -> kind -> rId or None
_GROUPS = (("header", "headers"), ("footer", "footers"))
_Printed = list[dict[str, dict[str, str | None]]]  # per section: group -> kind -> text shown


def _printed(doc: Document) -> _Printed:
    """The header and footer text each section prints per kind, inheritance resolved
    (None where it prints none)."""
    pkg = doc.pkg
    stories = {s.key: s for s in list_stories(pkg, ("headers",))}
    texts: dict[str, str] = {}
    out: _Printed = []
    for row in section_rows(doc):
        shown: dict[str, dict[str, str | None]] = {}
        for group, which in _GROUPS:
            shown[group] = {}
            for kind in HF_KINDS:
                key = row[which][kind]
                if key is not None and key not in texts:
                    texts[key] = _story_text(parse_part(pkg, stories[key].part))
                shown[group][kind] = None if key is None else texts[key]
        out.append(shown)
    return out


def _shown(plan: dict[str, Any], view: dict[str, Any]) -> list[set[tuple[str, str]]]:
    """Per plan section, the (group, kind) it prints once ``plan`` is made (``view``, the
    plan view now, for what the plan leaves out): default always, first with a different
    first page, even when two-sided."""
    two_sided = plan.get("two_sided", view["two_sided"])
    out = []
    for i, sect in enumerate(plan["sections"]):
        now = view["sections"][i] if i < len(view["sections"]) else {}
        first = sect.get("first_page_different", now.get("first_page_different", False))
        kinds = {"default"} | ({"first"} if first else set()) | ({"even"} if two_sided else set())
        out.append({(group, kind) for group, _ in _GROUPS for kind in kinds})
    return out


def _owned_view(
    sect: dict[str, Any], i: int, owned: dict[str, Any], before: _Printed
) -> dict[str, Any]:
    """Plan section ``sect`` as the plan view must show it after the write: without from,
    and with null for a kind asked the text the section already inherits (a no-op, so it
    keeps inheriting)."""
    out = {k: v for k, v in sect.items() if k != "from"}
    if i >= len(owned["sections"]) or i >= len(before):
        return out
    for group, _ in _GROUPS:
        if group not in sect:
            continue
        mine = owned["sections"][i][group]
        out[group] = {
            kind: None
            if isinstance(value, str) and mine[kind] is None and before[i][group][kind] == value
            else value
            for kind, value in sect[group].items()
        }
    return out


def _printed_differences(
    plan: dict[str, Any], owned: dict[str, Any], before: _Printed, after: _Printed
) -> list[str]:
    """Where a section prints other header or footer text than ``plan`` asks: the text
    given, else (key left out, or null on a kind it inherits) what it printed ``before``;
    null on a kind it owned (``owned``, the plan view before) means the previous section's.
    A kind the section does not print (first without a different first page, even when
    not two-sided) is left out."""
    sections = plan["sections"]
    if not len(sections) == len(before) == len(after):
        return []  # the section count differs; the plan comparison says so
    shown = _shown(plan, owned)
    out = []
    for i, sect in enumerate(sections):
        for group, _ in _GROUPS:
            for kind in HF_KINDS:
                value = sect.get(group, {}).get(kind, _OMITTED)
                if (group, kind) not in shown[i] and not isinstance(value, str):
                    continue
                want: str | None
                if isinstance(value, str):
                    want = value
                elif value is None and owned["sections"][i][group][kind] is not None:
                    want = after[i - 1][group][kind] if i else None
                else:
                    want = before[i][group][kind]
                got = after[i][group][kind]
                if (got or None) != (want or None):  # an empty part prints nothing too
                    out.append(f"sections[{i}].{group}.{kind} prints {got!r}, not {want!r}")
    return out


def _refuse_inherited_changes(
    slots: list[tuple[int, str, str, Any, str | None, str | None]],
    changed: dict[tuple[int, str, str], str | None],
    keep: dict[str, str],
    text_of: Any,
    shown: list[set[tuple[str, str]]],
) -> None:
    """Refuse when a changed reference would change what a later section prints that
    inherits the kind and was not asked to change (pinning it is not supported yet). A kind
    the section does not print (see :func:`_shown`) is free to change."""
    printed: dict[tuple[int, str, str], str | None] = {}  # slot -> text shown after
    source: dict[tuple[str, str], int] = {}  # (group, kind) -> last section changed
    for i, group, kind, _, own, part in slots:
        slot, prev = (i, group, kind), printed.get((i - 1, group, kind))
        before = text_of(part) if part is not None else None
        if slot in changed:
            text = changed[slot]
            printed[slot] = prev if text is None else text
            source[(group, kind)] = i
        elif own is not None:
            printed[slot] = keep[own]
        else:
            printed[slot] = prev
            if (
                (prev or None) != (before or None)
                and (group, kind) in source
                and (group, kind) in shown[i]
            ):
                edited = source[(group, kind)]
                raise unsupported(
                    f"changing sections[{edited}].{group}.{kind} would alter section "
                    f"{i + 1} (sections[{i}]), which inherits this {group}: changing a "
                    f"{group} that later sections inherit is not supported yet; nothing "
                    "is written",
                    section=i + 1,
                    edited=edited + 1,
                )


def _header_edits(
    after: Document, plan: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[int, _Refs], list[str]]:
    """What brings the header and footer text of ``after`` (the document with the plan's
    breaks made) to ``plan``: part edits, the references per plan section, and why each
    change that Word cannot track is needed.

    A part is rewritten in place (tracked) only when no section that keeps using it would
    change with it: every section referencing it asks the new text. A section asking other
    text than its part keeps gets a new part of its own; one asking the text it already
    shows (own or inherited) is left alone.

    A section whose reference of a kind changes (a new part, or null dropping its own) would
    change what a later section inheriting that kind prints; such a plan is refused
    (UNSUPPORTED_CONTENT) before anything is written: changing a header or footer that
    later sections inherit is not supported yet."""
    pkg = after.pkg
    rows = section_rows(after)
    if len(rows) != len(plan["sections"]):
        return [], {}, []  # the breaks went wrong; the comparison says how
    stories = {s.key: s for s in list_stories(pkg, ("headers",))}
    texts: dict[str, str] = {}

    def text_of(part: str) -> str:
        if part not in texts:
            texts[part] = _story_text(parse_part(pkg, part))
        return texts[part]

    users: dict[str, list[Any]] = {}  # part -> what each section using it asks
    slots: list[tuple[int, str, str, Any, str | None, str | None]] = []
    for i, (row, sect) in enumerate(zip(rows, plan["sections"], strict=True)):
        for group, which in _GROUPS:
            asked = sect.get(group, {})
            for kind in HF_KINDS:
                key = row[which][kind]
                part = stories[key].part if key is not None else None
                own = key is not None and stories[key].section == row["section"]
                value = asked.get(kind, _OMITTED)
                if part is not None:
                    # A section inheriting the part shows its text, whatever its plan says.
                    users.setdefault(part, []).append(value if own else _OMITTED)
                slots.append((i, group, kind, value, part if own else None, part))
    keep: dict[str, str] = {}
    for part, values in users.items():
        current = text_of(part)
        strings = [v for v in values if isinstance(v, str)]
        keep[part] = (
            current if _OMITTED in values or current in strings or not strings else strings[0]
        )
    edits: list[dict[str, Any]] = [
        {"kind": "header.text", "part": part, "text": text}
        for part, text in keep.items()
        if text != text_of(part)
    ]
    names = {group: free_part_names(pkg, group) for group, _ in _GROUPS}
    rids = free_rids(pkg)
    refs: dict[int, _Refs] = {}
    needs: list[str] = []
    changed: dict[tuple[int, str, str], str | None] = {}  # slot -> new text, None if dropped
    for i, group, kind, value, own, part in slots:
        where = f"sections[{i}].{group}.{kind}"
        if value is _OMITTED or (value is not None and part is not None and keep[part] == value):
            continue
        if value is None:
            if own is not None:
                refs.setdefault(i, {}).setdefault(group + "_refs", {})[kind] = None
                changed[(i, group, kind)] = None
                needs.append(f"{where}: dropping the section's reference")
            continue
        default = rows[i][group + "s"]["default"]
        like = part or (stories[default].part if default is not None else None)
        like = like or next((s.part for s in stories.values() if s.kind == group), None)
        name, rid = next(names[group]), next(rids)
        edit = {"kind": "header.new", "part": name, "rid": rid, "group": group, "text": value}
        edits.append({**edit, "like": like} if like is not None else edit)
        refs.setdefault(i, {}).setdefault(group + "_refs", {})[kind] = rid
        changed[(i, group, kind)] = value
        needs.append(f"{where}: a new {group} part")
    _refuse_inherited_changes(slots, changed, keep, text_of, _shown(plan, page_plan(after)))
    return edits, refs, needs


def _attach_refs(edits: list[dict[str, Any]], target: tuple[str, int], refs: _Refs) -> None:
    """Add ``refs`` to the props of the edit that sets up ``target`` (see :func:`_plan_edits`)."""
    where, n = target
    if where == "break":
        props = edits[n].setdefault("props", {})
    else:
        found = next((e for e in edits if e["kind"] == "section.props" and e["section"] == n), None)
        if found is None:
            found = {"kind": "section.props", "section": n, "props": {}}
            edits.append(found)
        props = found["props"]
    for key, values in refs.items():
        props[key] = {**props.get(key, {}), **values}


def _settings_edit(plan: dict[str, Any], now: dict[str, Any]) -> dict[str, Any] | None:
    flags = {
        setting: plan[key]
        for key, setting in (
            ("two_sided", "even_and_odd_headers"),
            ("mirror_margins", "mirror_margins"),
        )
        if key in plan and plan[key] != now[key]
    }
    return {"kind": "settings", **flags} if flags else None


def _read_back(package: bytes | None, scratch: str, name: str) -> Document:
    assert package is not None
    made = Path(scratch) / name
    made.write_bytes(package)
    return Document(str(made))


def apply_plan(
    path: Path,
    plan: dict[str, Any],
    *,
    dry_run: bool = False,
    untracked: bool = False,
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Bring the document to ``plan`` in one write; the result and its warnings. The write
    is checked before it is made: the plan read back from the resulting file must equal
    ``plan`` (a header or footer asked the text the section already inherits reads back as
    null: it keeps inheriting), else nothing is written."""
    _check_plan(plan)
    doc = Document(str(path))
    # Every write, the dry runs that check it and the real one, is pinned to the bytes read
    # here: a file that changes in between is refused (DOC_CHANGED_ON_DISK), not rewritten
    # from what the check never saw. expect.sha256 is checked even when nothing is written.
    pinned = doc.sha256
    if expect_sha256 is not None and pinned != expect_sha256.strip().lower():
        raise DocControlError(
            ErrorCode.DOC_CHANGED_ON_DISK,
            f"{path.name} changed on disk since it was read",
            path=str(path),
            expected_sha256=expect_sha256,
            actual_sha256=pinned,
        )
    edits, targets = _plan_edits(doc, plan)
    write = partial(
        apply_layout_edits,
        path,
        untracked=untracked,
        author=author,
        tag=tag,
        expect_sha256=pinned,
    )
    now = page_plan(doc)
    after = now
    probe: LayoutWriteResult | None = None
    blocked: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        made = doc
        if edits:
            probe = write(edits, dry_run=True)
            made = _read_back(probe.package, scratch, "breaks.docx")
        owned, before = page_plan(made), _printed(made)
        more, refs, needs = _header_edits(made, plan)
        settings = _settings_edit(plan, now)
        if settings is not None:
            needs.append("two_sided, mirror_margins: a settings change")
        if needs and not untracked:
            blocked = needs
        elif more or refs or settings is not None:
            for i, section_refs in refs.items():
                _attach_refs(edits, targets[i], section_refs)
            edits += more
            if settings is not None:
                edits.append(settings)
            probe = write(edits, dry_run=True)
            made = _read_back(probe.package, scratch, "after.docx")
        after = page_plan(made)
        printed = _printed_differences(plan, owned, before, _printed(made))
    # The header and footer text is checked twice: in the plan view (what each section owns)
    # and as printed (inheritance resolved).
    want = {
        **plan,
        "sections": [_owned_view(s, i, owned, before) for i, s in enumerate(plan["sections"])],
    }
    differences = _differences(want, after) + printed
    if blocked:
        raise unsupported(
            "Word cannot track what the plan needs (D-L2): "
            + "; ".join(blocked)
            + "; pass untracked: true",
            differences=differences,
            needs=blocked,
        )
    if differences:
        raise unsupported(
            "the plan asks for more than layout.apply can reach; nothing is written",
            differences=differences,
        )
    if not edits:
        return {"dry_run": dry_run, "changes": [], "plan": after}, []
    if dry_run:
        assert probe is not None
        return {"dry_run": True, "changes": probe.changes, "plan": after}, []
    done = write(edits)
    result = {
        "dry_run": False,
        "changes": done.changes,
        "plan": page_plan(Document(str(path))),
        "revision_ids": done.revision_ids,
        "sha256": done.after_sha256,
        "backup": str(done.backup) if done.backup else None,
        "op_id": done.op_id,
    }
    return result, done.warnings


@operation(
    "layout.apply",
    "write",
    "Bring the document to a page plan (as layout.get returns it): where each section "
    "starts (from), its break (nextPage, oddPage for a right-hand start, evenPage, "
    "continuous), the page-number start and format, a different first page, the header and "
    "footer text per kind (default, first, even; {PAGE}, {NUMPAGES} and {chapter} become "
    "fields; null drops the section's own, so it inherits the previous section's; asking "
    "the text a section already prints changes nothing; a new part or a dropped reference "
    "that would change what a later section inheriting it prints is refused, as changing a "
    "header or footer that later sections inherit is not supported yet), two_sided (even "
    "and odd headers) and "
    "mirror_margins. Keys left out stay as they are; shown is checked, not set. Breaks, "
    "numbering and the text of a header or footer part are tracked changes; a part is "
    "rewritten only when every section using it asks the same text, else the section gets "
    "a new part. A new part, a dropped reference and two_sided or mirror_margins cannot be "
    "tracked and need untracked: true (LAYOUT D-L2). Sections already in the document must "
    "all be kept. A section that still holds an unaccepted tracked change of its setup (say "
    "from an earlier layout.apply) cannot take another, so a second apply that changes it "
    "is refused (UNSUPPORTED_CONTENT) until that change is accepted or rejected. A "
    "split-off section keeps the number format of the section it came from unless the plan "
    "states one. All or nothing: the plan read back from the result must equal the "
    "request, else nothing is written (UNSUPPORTED_CONTENT). dry_run lists the changes and "
    "the plan that would result.",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            "plan": {
                "type": "object",
                "description": "The page plan: version 1 and sections, each with a from address.",
            },
            "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
            "untracked": {
                "type": "boolean",
                "description": "Allow a change Word cannot track (LAYOUT D-L2).",
            },
            "expect": {
                "type": "object",
                "properties": {"sha256": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            "author": {"type": "string", "minLength": 1, "description": "Revision author."},
            "tag": {"type": "string", "minLength": 1, "description": "Appended to the author."},
        },
        "required": ["doc", "plan"],
        "additionalProperties": False,
    },
)
def layout_apply(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    result, warnings = apply_plan(
        path,
        request["plan"],
        dry_run=request.get("dry_run", False),
        untracked=request.get("untracked", False),
        expect_sha256=request.get("expect", {}).get("sha256"),
        author=request.get("author", DEFAULT_AUTHOR),
        tag=request.get("tag"),
    )
    return Reply(result, doc=Document(str(path)).envelope_doc(), warnings=warnings)
