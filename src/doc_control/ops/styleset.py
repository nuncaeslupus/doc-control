"""JSON style sets: ``styleset.export`` (read) and ``styleset.install`` (write).

A style set is ``{"styleset": name, "version": 1, "styles": [...]}``; each style carries
``id``, ``name``, ``type``, ``based_on``, ``next``, ``link``, ``default``, ``custom``,
``quick`` and the ``run`` / ``paragraph`` formatting in the vocabulary of ``style.get``
(lengths in twips). Installing an exported set into a document leaves the definitions
of those styles equal to the exported ones. What a set does not carry (table style
conditional formatting, numbering links, theme fonts, latent styles) is left as it is
in the document on a replace and absent on a create.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.styles import Style
from doc_control.ooxml.styledef import apply_properties, invalid, set_child
from doc_control.ops.stylewrite import (
    TYPES,
    StyleSession,
    apply_refs,
    commit_styles,
    create_style,
)
from doc_control.registry import operation

VERSION = 1
_DOC = {"type": "string", "minLength": 1, "description": "Path to the .docx."}


def style_entry(style: Style) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": style.id, "name": style.name, "type": style.type}
    for key in ("based_on", "next", "link"):
        if getattr(style, key):
            entry[key] = getattr(style, key)
    for key in ("default", "custom", "quick"):
        if getattr(style, key):
            entry[key] = True
    if style.rpr:
        entry["run"] = style.rpr
    if style.ppr:
        entry["paragraph"] = style.ppr
    return entry


@operation(
    "styleset.export",
    "read",
    (
        "Export the document's styles (all, the ones named, or custom ones only) as a "
        "JSON style set, returned and optionally also written to `out`. Feed it to "
        "styleset.install on another document; styleset.export-dotx writes a Word "
        "template instead. The document is not changed."
    ),
    {
        "type": "object",
        "properties": {
            "doc": _DOC,
            "name": {"type": "string", "minLength": 1, "description": "Set name."},
            "styles": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Only these styles (ids or names).",
            },
            "custom_only": {"type": "boolean", "description": "Only user-defined styles."},
            "out": {"type": "string", "minLength": 1, "description": "Also write the JSON here."},
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def styleset_export(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    styleset = select_styleset(doc, request)
    entries = styleset["styles"]
    result: dict[str, Any] = {"styleset": styleset, "count": len(entries)}
    if "out" in request:
        write_new_file(Path(request["out"]), Path(request["doc"]), _json_bytes(styleset))
        result["path"] = request["out"]
    return Reply(result, doc=doc.envelope_doc())


def _json_bytes(styleset: dict[str, Any]) -> bytes:
    return (json.dumps(styleset, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def select_styleset(doc: Document, request: dict[str, Any]) -> dict[str, Any]:
    """The document's styles (``styles`` / ``custom_only`` applied) as a style set."""
    chosen = request.get("styles")
    wanted: set[str] | None = None
    if chosen is not None:
        wanted = set()
        for key in chosen:
            found = doc.styles.find(key)
            if found is None:
                raise DocControlError(ErrorCode.STYLE_NOT_FOUND, f"no style {key!r}", style=key)
            wanted.add(found.id)
    entries = [
        style_entry(s)
        for s in doc.styles.styles
        if (wanted is None or s.id in wanted) and not (request.get("custom_only") and not s.custom)
    ]
    return {
        "styleset": request.get("name") or Path(request["doc"]).stem,
        "version": VERSION,
        "styles": entries,
    }


def write_new_file(out: Path, doc: Path, payload: bytes) -> None:
    """Write the set to a file that does not exist yet; never the document itself."""
    same = out.exists() and doc.exists() and out.samefile(doc)
    if same or out.resolve() == doc.resolve():
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f"out is the document: {out}", path=str(out)
        )
    if out.exists():
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"out already exists, nothing was written: {out}",
            path=str(out),
        )
    try:
        with out.open("xb") as fh:
            fh.write(payload)
    except FileExistsError as exc:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f"out already exists: {out}", path=str(out)
        ) from exc
    except OSError as exc:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"cannot write out: {type(exc).__name__}",
            path=str(out),
        ) from exc


def _validated(styleset: Any) -> list[dict[str, Any]]:
    if not isinstance(styleset, dict) or not isinstance(styleset.get("styles"), list):
        raise invalid("a style set is an object with a 'styles' list")
    if styleset.get("version") != VERSION:
        raise invalid(
            f"unsupported style set version {styleset.get('version')!r}", supported=VERSION
        )
    seen: set[str] = set()
    for i, entry in enumerate(styleset["styles"]):
        if not isinstance(entry, dict):
            raise invalid(f"styles[{i}] must be an object")
        for key in ("id", "name"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise invalid(f"styles[{i}].{key} must be a non-empty string")
        if entry.get("type", "paragraph") not in TYPES:
            raise invalid(f"styles[{i}].type must be one of {', '.join(TYPES)}")
        if entry["id"] in seen:
            raise invalid(f"styles[{i}]: id {entry['id']!r} appears twice")
        seen.add(entry["id"])
    entries: list[dict[str, Any]] = styleset["styles"]
    return entries


def _install(
    session: StyleSession, entries: list[dict[str, Any]], mode: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in entries:
        sid = entry["id"]
        existing = session.records().get(sid)
        if existing is None:
            spec = dict(entry)
            spec["quick"] = bool(entry.get("quick", False))
            spec["custom"] = bool(entry.get("custom", False))
            kind = spec.get("type", "paragraph")
            if spec.get("default") and any(
                r.default and r.type == kind for r in session.records().values()
            ):
                spec["default"] = False  # the document keeps its own default
            create_style(session, spec)
            out.append({"style": sid, "action": "created"})
            continue
        if mode == "skip":
            out.append({"style": sid, "action": "skipped"})
            continue
        if mode == "error":
            raise DocControlError(
                ErrorCode.STYLE_CONFLICT, f"style {sid!r} already exists", style=sid
            )
        if existing.type != entry.get("type", "paragraph"):
            raise DocControlError(
                ErrorCode.STYLE_CONFLICT,
                f"style {sid!r} is a {existing.type} style in the document, "
                f"the set has a {entry.get('type', 'paragraph')} style",
                style=sid,
            )
        others = [r for r in session.records().values() if r.id != sid]
        if any(r.name.lower() == entry["name"].lower() for r in others):
            raise DocControlError(
                ErrorCode.STYLE_CONFLICT, f"a style named {entry['name']!r} already exists"
            )
        el = session.styles.element(sid)
        set_child(el, "name", entry["name"])
        apply_refs(
            el,
            {
                "based_on": entry.get("based_on", ""),
                "next": entry.get("next", ""),
                "link": entry.get("link", ""),
                "quick": bool(entry.get("quick", False)),
            },
            session,
        )
        apply_properties(el, entry.get("run", {}), entry.get("paragraph", {}), replace=True)
        session.styles.put(el)
        session.touched.add(sid)
        out.append({"style": sid, "action": "replaced"})
    return out


@operation(
    "styleset.install",
    "write",
    "Install a JSON style set into the document: new styles are added, existing ones "
    "replaced (or skipped); dry_run previews.",
    {
        "type": "object",
        "properties": {
            "doc": _DOC,
            "styleset": {"type": "object", "description": "The style set (inline)."},
            "path": {"type": "string", "minLength": 1, "description": "Or a style set file."},
            "on_conflict": {
                "enum": ["replace", "skip", "error"],
                "description": "A style id already in the document (default replace).",
            },
            "expect": {
                "type": "object",
                "properties": {"sha256": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def styleset_install(request: dict[str, Any]) -> Reply:
    if ("styleset" in request) == ("path" in request):
        raise invalid("give exactly one of 'styleset' and 'path'")
    if "path" in request:
        try:
            data = json.loads(Path(request["path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise invalid(f"cannot read the style set: {type(exc).__name__}") from exc
    else:
        data = request["styleset"]
    entries = _validated(data)
    mode = request.get("on_conflict", "replace")
    result = commit_styles(
        Path(request["doc"]),
        lambda session: _install(session, entries, mode),
        expect_sha256=request.get("expect", {}).get("sha256"),
        dry_run=request.get("dry_run", False),
    )
    return Reply(result, doc=Document(request["doc"]).envelope_doc())
