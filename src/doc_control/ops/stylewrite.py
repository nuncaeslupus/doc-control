"""Style write operations: ``style.create``, ``style.update``, ``style.rename``, ``style.merge``.

They edit ``word/styles.xml`` (one verified write, backup first) and, for a rename of the
style id or a merge, the style references (``w:pStyle``, ``w:rStyle``, ``w:tblStyle``) in
every other part: the document's paragraphs, headers, footers, footnotes, endnotes,
comments and numbering. ``basedOn``, ``next`` and ``link`` inside the styles part follow.
Each operation is also a ``batch`` kind, but a style batch holds only style operations
and its definition changes are not tracked changes (they are covered by the dry run, the
backup and the undo log). Latent styles are left alone: they are defaults for built-in
styles that are not instantiated, keyed by name.

A style is named by its id or by its display name wherever a request says ``style``,
``from`` or ``into``; the stored references always use ids.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, unescape

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.index import build_index
from doc_control.model.parts import DOCUMENT_RELS, OFFICE_REL, W, related_part
from doc_control.ooxml.styledef import (
    STYLES_CONTENT_TYPE,
    Rec,
    StylesText,
    apply_properties,
    invalid,
    new_style,
    set_child,
)
from doc_control.ooxml.zipio import Package
from doc_control.registry import operation
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, Footprint, write_verified

STYLES_PART = "word/styles.xml"
STYLE_KINDS = ("style.create", "style.update", "style.rename", "style.merge")
TYPES = ("paragraph", "character", "table", "numbering")
_REF = re.compile(rb'(<(?:[\w.-]+:)?(?:pStyle|rStyle|tblStyle)\b[^>]*?\bval=")([^"]*)(")')

_DOC = {"type": "string", "minLength": 1, "description": "Path to the .docx."}
_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "string", "minLength": 1, "description": "Style id (default: from the name)."},
    "name": {"type": "string", "minLength": 1, "description": "Display name."},
    "type": {"enum": list(TYPES), "description": "Kind of style (default paragraph)."},
    "based_on": {"type": "string", "description": "Parent style id or name; '' for none."},
    "next": {"type": "string", "description": "Style of the next paragraph; '' for none."},
    "link": {"type": "string", "description": "Linked paragraph/character style; '' for none."},
    "quick": {"type": "boolean", "description": "Show in the quick-style gallery."},
    "default": {"type": "boolean", "description": "The default style of its type."},
    "custom": {"type": "boolean", "description": "A user-defined (not built-in) style."},
    "run": {
        "type": "object",
        "description": "Character formatting: bold, italic, small_caps, strike, underline, "
        "font, size_pt, color, highlight, vertical_align; null removes a key.",
    },
    "paragraph": {
        "type": "object",
        "description": "Paragraph formatting (twips): alignment, indent{left,right,first_line,"
        "hanging}, spacing{before,after,line,line_rule}, keep_next, keep_lines, "
        "page_break_before, outline_level; null removes a key.",
    },
}
_STYLE_REF = {"type": "string", "minLength": 1, "description": "Style id or name."}
_OPS: dict[str, tuple[dict[str, dict[str, Any]], list[str], str]] = {
    "style.create": (_FIELDS, ["name"], "Add a style."),
    "style.update": (
        {
            "style": _STYLE_REF,
            **{k: v for k, v in _FIELDS.items() if k in ("based_on", "next", "link", "quick")},
            "run": _FIELDS["run"],
            "paragraph": _FIELDS["paragraph"],
        },
        ["style"],
        "Change a style's parent, next/link styles, quick flag or formatting.",
    ),
    "style.rename": (
        {
            "style": _STYLE_REF,
            "name": _FIELDS["name"],
            "id": _FIELDS["id"],
        },
        ["style"],
        "Change a style's display name and/or id; references follow.",
    ),
    "style.merge": (
        {
            "from": {
                "description": "Style id or name, or a list of them (they are removed).",
            },
            "into": _STYLE_REF,
        },
        ["from", "into"],
        "Re-point every use of the 'from' styles to 'into' and delete them.",
    ),
}


def op_schema(kind: str) -> dict[str, Any]:
    """The batch-item schema of one style operation."""
    fields, required, _ = _OPS[kind]
    return {
        "type": "object",
        "properties": {"op": {"enum": [kind]}, **fields},
        "required": ["op", *required],
        "additionalProperties": False,
    }


# -- the editing session ------------------------------------------------------


class StyleSession:
    def __init__(self, pkg: Package) -> None:
        self.pkg = pkg
        self.part = related_part(pkg, OFFICE_REL + "styles", STYLES_PART)
        self.styles = StylesText(pkg.read(self.part) if self.part else None)
        self.original = self.styles.text
        self.remap: dict[str, str] = {}
        self.touched: set[str] = set()

    def records(self) -> dict[str, Rec]:
        return self.styles.records()

    def resolve(self, key: str) -> Rec:
        recs = self.records()
        found = recs.get(key) or next(
            (r for r in recs.values() if r.name.lower() == key.lower()), None
        )
        if found is None:
            raise DocControlError(ErrorCode.STYLE_NOT_FOUND, f"no style {key!r}", style=key)
        return found

    def ref_id(self, key: str) -> str:
        """A style reference as an id: by id, else by name, else as written (checked later)."""
        recs = self.records()
        if key in recs:
            return key
        return next((r.id for r in recs.values() if r.name.lower() == key.lower()), key)

    def remap_ref(self, old: str, new: str) -> None:
        for k, v in self.remap.items():
            if v == old:
                self.remap[k] = new
        self.remap[old] = new

    def check(self) -> None:
        """Every reference of a touched style resolves and nothing is based on itself."""
        recs = self.records()
        for sid in self.touched:
            rec = recs.get(sid)
            if rec is None:
                continue
            for label, target in (
                ("based_on", rec.based_on),
                ("next", rec.next),
                ("link", rec.link),
            ):
                if target is not None and target not in recs:
                    raise DocControlError(
                        ErrorCode.STYLE_NOT_FOUND,
                        f"style {sid!r} {label} names {target!r}, which does not exist",
                        style=target,
                    )
            seen = {sid}
            parent = rec.based_on
            while parent is not None:
                if parent in seen:
                    raise DocControlError(
                        ErrorCode.STYLE_CONFLICT,
                        f"style {sid!r} would be based on itself through {parent!r}",
                        style=sid,
                    )
                seen.add(parent)
                parent = recs[parent].based_on if parent in recs else None


def _conflict(message: str, **details: Any) -> DocControlError:
    return DocControlError(ErrorCode.STYLE_CONFLICT, message, **details)


def apply_refs(el: Any, spec: dict[str, Any], session: StyleSession) -> None:
    for key, tag in (("based_on", "basedOn"), ("next", "next"), ("link", "link")):
        if key in spec:
            value = spec[key]
            if not isinstance(value, str):
                raise invalid(f"{key} must be a string")
            set_child(el, tag, session.ref_id(value) if value else None)
    if "quick" in spec:
        flag = spec["quick"]
        if not isinstance(flag, bool):
            raise invalid("quick must be true or false")
        set_child(el, "qFormat", "1" if flag else None, flag=True)


def create_style(s: StyleSession, spec: dict[str, Any]) -> dict[str, Any]:
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise invalid("name must be a non-empty string")
    kind = spec.get("type", "paragraph")
    if kind not in TYPES:
        raise invalid(f"type must be one of {', '.join(TYPES)}")
    recs = s.records()
    sid = spec.get("id")
    if sid is None:
        base = re.sub(r"[^A-Za-z0-9]", "", name) or "Style"
        sid, n = base, 1
        while sid in recs:
            n += 1
            sid = f"{base}{n}"
    elif not isinstance(sid, str) or not sid:
        raise invalid("id must be a non-empty string")
    if sid in recs:
        raise _conflict(f"a style with id {sid!r} already exists", style=sid)
    if any(r.name.lower() == name.lower() for r in recs.values()):
        raise _conflict(f"a style named {name!r} already exists", name=name)
    default = bool(spec.get("default", False))
    if default and any(r.default and r.type == kind for r in recs.values()):
        raise _conflict(f"a default {kind} style already exists")
    el = new_style(sid, name, kind, default=default, custom=bool(spec.get("custom", not default)))
    apply_refs(el, spec, s)
    apply_properties(el, spec.get("run"), spec.get("paragraph"), replace=False)
    s.styles.put(el)
    s.touched.add(sid)
    return {"op": "style.create", "style": sid, "name": name, "type": kind}


def _update(s: StyleSession, spec: dict[str, Any], *, replace: bool = False) -> dict[str, Any]:
    rec = s.resolve(_need(spec, "style"))
    el = s.styles.element(rec.id)
    apply_refs(el, spec, s)
    apply_properties(el, spec.get("run"), spec.get("paragraph"), replace=replace)
    s.styles.put(el)
    s.touched.add(rec.id)
    return {"op": "style.update", "style": rec.id, "name": rec.name}


def _need(spec: dict[str, Any], key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        raise invalid(f"{key} must be a non-empty string")
    return value


def _rename(s: StyleSession, spec: dict[str, Any]) -> dict[str, Any]:
    rec = s.resolve(_need(spec, "style"))
    if "name" not in spec and "id" not in spec:
        raise invalid("give a new name and/or a new id")
    el = s.styles.element(rec.id)
    recs = s.records()
    new_name = spec.get("name", rec.name)
    new_id = spec.get("id", rec.id)
    for key, value in (("name", new_name), ("id", new_id)):
        if not isinstance(value, str) or not value:
            raise invalid(f"{key} must be a non-empty string")
    if any(r.id != rec.id and r.name.lower() == new_name.lower() for r in recs.values()):
        raise _conflict(f"a style named {new_name!r} already exists", name=new_name)
    if new_id != rec.id and new_id in recs:
        raise _conflict(f"a style with id {new_id!r} already exists", style=new_id)
    set_child(el, "name", new_name)
    if new_id != rec.id:
        el.set(W + "styleId", new_id)
    s.styles.put(el, replacing=rec.id)
    if new_id != rec.id:
        s.styles.retarget(rec.id, new_id)
        s.remap_ref(rec.id, new_id)
        s.touched.discard(rec.id)
    s.touched.add(new_id)
    return {
        "op": "style.rename",
        "style": new_id,
        "name": new_name,
        "was": {"id": rec.id, "name": rec.name},
    }


def _merge(s: StyleSession, spec: dict[str, Any]) -> dict[str, Any]:
    sources = spec.get("from")
    if isinstance(sources, str):
        sources = [sources]
    if (
        not isinstance(sources, list)
        or not sources
        or not all(isinstance(x, str) and x for x in sources)
    ):
        raise invalid("from must be a style id/name or a non-empty list of them")
    target = s.resolve(_need(spec, "into"))
    merged: list[str] = []
    before = s.records()
    for key in sources:
        src = s.resolve(key)
        if src.id == target.id:
            raise _conflict(f"cannot merge {src.id!r} into itself", style=src.id)
        if src.type != target.type:
            raise _conflict(
                f"cannot merge {src.type} style {src.id!r} into {target.type} style {target.id!r}"
            )
        if src.type in ("table", "numbering"):
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT, f"merging {src.type} styles is not supported"
            )
        if src.default:
            raise _conflict(f"{src.id!r} is the default {src.type} style and cannot be merged away")
        s.styles.retarget(src.id, target.id, "basedOn|next")
        s.styles.retarget(src.id, None, "link")
        s.styles.remove(src.id)
        s.remap_ref(src.id, target.id)
        s.touched.discard(src.id)
        merged.append(src.id)
    base = before[target.id].based_on
    if base in merged:
        # Based on a merged-away style: inherit from that style's own parent instead.
        while base in merged:
            base = before[base].based_on
        el = s.styles.element(target.id)
        set_child(el, "basedOn", None if base == target.id else base)
        s.styles.put(el)
    s.touched.add(target.id)
    return {"op": "style.merge", "style": target.id, "merged": merged}


_HANDLERS: dict[str, Callable[[StyleSession, dict[str, Any]], dict[str, Any]]] = {
    "style.create": create_style,
    "style.update": _update,
    "style.rename": _rename,
    "style.merge": _merge,
}


# -- references in the other parts --------------------------------------------


def _rewriter(remap: Mapping[str, str]) -> Callable[[bytes], bytes]:
    def sub(m: re.Match[bytes]) -> bytes:
        old = unescape(m.group(2).decode("utf-8"), {"&quot;": '"'})
        if old not in remap:
            return m.group(0)
        new = escape(remap[old], {'"': "&quot;"}).encode("utf-8")
        return m.group(1) + new + m.group(3)

    return lambda data: _REF.sub(sub, data)


def _rewrite_parts(
    pkg: Package, styles_part: str | None, remap: dict[str, str]
) -> tuple[dict[str, bytes], frozenset[int], int]:
    """New bytes for every part that references a remapped style, and the paragraphs changed."""
    rewrite = _rewriter(remap)
    replacements: dict[str, bytes] = {}
    count = 0
    for name in pkg.names:
        if name in (DOCUMENT_PART, styles_part) or not (
            name.startswith("word/") and name.endswith(".xml")
        ):
            continue
        data = pkg.read(name)
        if b"Style" not in data:
            continue
        new = rewrite(data)
        if new != data:
            replacements[name] = new
            count += _hits(data, remap)
    data = pkg.read(DOCUMENT_PART)
    if b"Style" not in data:
        return replacements, frozenset(), count
    wanted = _hits(data, remap)
    if not wanted:
        return replacements, frozenset(), count
    spans = build_index(data).spans.paragraphs
    changed: set[int] = set()
    pieces: list[bytes] = []
    pos = 0
    done = 0
    for i, span in enumerate(spans):
        if span.start < pos:
            continue  # nested in the previous paragraph: rewritten with it
        chunk = data[span.start : span.end]
        new = rewrite(chunk)
        if new != chunk:
            changed.add(i)
            done += _hits(chunk, remap)
            pieces.append(data[pos : span.start])
            pieces.append(new)
            pos = span.end
    if done < wanted:
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            "a style being renamed or merged is referenced outside any paragraph "
            "(a table style?), which a verified write cannot rewrite",
        )
    pieces.append(data[pos:])
    replacements[DOCUMENT_PART] = b"".join(pieces)
    return replacements, frozenset(changed), count + done


def _hits(data: bytes, remap: Mapping[str, str]) -> int:
    return sum(
        1
        for m in _REF.finditer(data)
        if unescape(m.group(2).decode("utf-8"), {"&quot;": '"'}) in remap
    )


def _register_part(pkg: Package, replacements: dict[str, bytes]) -> None:
    """Content type and document relationship for a styles part that did not exist."""
    types = pkg.read("[Content_Types].xml").decode("utf-8")
    entry = f'<Override PartName="/{STYLES_PART}" ContentType="{STYLES_CONTENT_TYPE}"/>'
    replacements["[Content_Types].xml"] = types.replace("</Types>", entry + "</Types>").encode()
    if DOCUMENT_RELS in pkg:
        rels = pkg.read(DOCUMENT_RELS).decode("utf-8")
        used = {int(n) for n in re.findall(r'Id="rId(\d+)"', rels)}
        rid = f"rId{max(used, default=0) + 1}"
        rel = f'<Relationship Id="{rid}" Type="{OFFICE_REL}styles" Target="styles.xml"/>'
        replacements[DOCUMENT_RELS] = rels.replace(
            "</Relationships>", rel + "</Relationships>"
        ).encode()
    else:
        replacements[DOCUMENT_RELS] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{OFFICE_REL}styles" Target="styles.xml"/>'
            "</Relationships>"
        ).encode()


# -- the write ------------------------------------------------------------------


def run_style_ops(
    session: StyleSession, ops: list[dict[str, Any]], handlers: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    out = []
    for i, raw in enumerate(ops):
        kind = raw.get("op")
        handler = (handlers or _HANDLERS).get(kind) if isinstance(kind, str) else None
        if handler is None:
            raise invalid(f"ops[{i}]: not a style operation: {kind!r}", op_index=i)
        spec = {k: v for k, v in raw.items() if k != "op"}
        try:
            out.append(handler(session, spec))
        except DocControlError as err:
            raise DocControlError(
                err.code, f"ops[{i}]: {err.message}", op_index=i, **err.details
            ) from err
    session.check()
    return out


def commit_styles(
    doc_path: Path,
    prepare: Callable[[StyleSession], list[dict[str, Any]]],
    *,
    expect_sha256: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    stage: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run ``prepare`` on a session over ``doc_path`` and write the result in one verified write."""
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    session = StyleSession(pkg)
    applied = prepare(session)
    session.check()
    replacements: dict[str, bytes] = {}
    changed_paras: frozenset[int] = frozenset()
    references = 0
    if session.remap:
        replacements, changed_paras, references = _rewrite_parts(
            pkg, session.part, {k: v for k, v in session.remap.items() if k != v}
        )
    if session.styles.text != session.original or session.part is None:
        target = session.part or STYLES_PART
        replacements[target] = session.styles.data()
        if session.part is None:
            _register_part(pkg, replacements)
    result: dict[str, Any] = {
        "applied": applied,
        "dry_run": dry_run,
        "tracked": False,
        "changed_parts": sorted(replacements),
        "paragraphs_changed": len(changed_paras),
        "references_rewritten": references,
    }
    if dry_run or not replacements:
        if not dry_run:
            result["sha256"] = pkg.sha256
        return result
    written = write_verified(
        doc_path,
        replacements,
        Footprint(replaced=changed_paras, parts=frozenset(replacements)),
        pkg.sha256,
        now=now,
        env=env,
        stage=stage,
    )
    result["sha256"] = written.after_sha256
    result["backup"] = str(written.backup)
    return result


def apply_style_ops(
    doc_path: Path,
    ops: list[dict[str, Any]],
    *,
    expect_sha256: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    stage: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply style operations (``ops`` as in a batch) in one verified write."""
    if not ops:
        raise invalid("no style operations given")
    return commit_styles(
        doc_path,
        lambda session: run_style_ops(session, ops),
        expect_sha256=expect_sha256,
        dry_run=dry_run,
        now=now,
        env=env,
        stage=stage,
    )


def _register(kind: str) -> None:
    fields, required, summary = _OPS[kind]
    schema = {
        "type": "object",
        "properties": {
            "doc": _DOC,
            **fields,
            "expect": {
                "type": "object",
                "properties": {"sha256": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
        },
        "required": ["doc", *required],
        "additionalProperties": False,
    }

    def handler(request: dict[str, Any]) -> Reply:
        spec = {k: v for k, v in request.items() if k not in ("doc", "expect", "dry_run")}
        result = apply_style_ops(
            Path(request["doc"]),
            [{"op": kind, **spec}],
            expect_sha256=request.get("expect", {}).get("sha256"),
            dry_run=request.get("dry_run", False),
        )
        return Reply(result, doc=Document(request["doc"]).envelope_doc())

    operation(kind, "write", summary, schema)(handler)


for _kind in STYLE_KINDS:
    _register(_kind)
