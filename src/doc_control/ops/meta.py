"""Self-description: capabilities and help."""

from __future__ import annotations

import sys
from typing import Any

from doc_control import API_VERSION, __version__, outputs
from doc_control.errors import HINTS, DocControlError, ErrorCode
from doc_control.live.dispatch import apps
from doc_control.registry import REGISTRY, canonical, load_operations, operation
from doc_control.update_check import attach

_EMPTY: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


_CAPABILITIES_OUTPUT = outputs.result(
    ("api", "version", "engines", "live", "features", "operations", "errors", "build"),
    api=outputs.string("API version of the envelope."),
    version=outputs.string("doc-control version."),
    engines=outputs.strings("Engines: file, live."),
    live=outputs.obj("The live engine.", apps=outputs.arr("Every live app.", outputs.LIVE_APP)),
    features=outputs.obj(
        "Feature support per engine.",
        plain_writes=outputs.obj(
            "tracked: false writes.",
            file=outputs.boolean("File engine."),
            live=outputs.boolean("Live."),
        ),
    ),
    operations=outputs.arr(
        "Every operation.",
        outputs.obj(
            "An operation.",
            name=outputs.string("Its name (MCP tool: dots as underscores)."),
            kind=outputs.string("read or write."),
            summary=outputs.string("What it does."),
        ),
    ),
    errors=outputs.strings("Every error code."),
    build=outputs.obj(
        "How this copy was built.",
        frozen=outputs.boolean("A frozen binary."),
        freezer=outputs.string("Its freezer (pyinstaller)."),
    ),
    update=outputs.UPDATE,
)


@operation(
    "server.capabilities",
    "read",
    (
        "Describe this doc-control server: API version, engines (file, live), whether it "
        "is a frozen build, and every operation it offers. Call it once at the start; "
        "server.help gives one operation's full schema. Read-only; needs no document."
    ),
    _EMPTY,
    output=_CAPABILITIES_OUTPUT,
)
def capabilities(_: dict[str, Any]) -> dict[str, Any]:
    load_operations()
    out: dict[str, Any] = {
        "api": API_VERSION,
        "version": __version__,
        "engines": ["file", "live"],
        "live": {"apps": apps()},
        # Per engine; `tracked: false` on batch, term.rename and restore (D-21). Live: later.
        "features": {"plain_writes": {"file": True, "live": False}},
        "operations": [
            {"name": op.name, "kind": op.kind, "summary": op.summary}
            for op in sorted(REGISTRY.values(), key=lambda o: o.name)
        ],
        "errors": sorted(code.value for code in HINTS),
        "build": _build(),
    }
    return attach(out)


def _build() -> dict[str, Any]:
    """``{"frozen": false}`` for a pip install; a frozen binary also names its freezer."""
    if not getattr(sys, "frozen", False):
        return {"frozen": False}
    return {"frozen": True, "freezer": "pyinstaller" if hasattr(sys, "_MEIPASS") else "unknown"}


_DESCRIBED = outputs.mapping(
    "Name -> {kind, summary, schema (JSON Schema of the request)"
    ", and output_schema (of the result) when one operation is asked}."
)
_HELP_OUTPUT = outputs.result(
    operations=_DESCRIBED,
    batch_operations={**_DESCRIBED, "description": "doc.batch kind -> {kind, summary, schema}."},
)


@operation(
    "server.help",
    "read",
    "JSON Schema of one operation, or of all operations; also the kinds doc.batch takes in "
    "`ops` (text.replace, comment.add…), listed under batch_operations; a name that is "
    "both gets both. An operation's old name (before 0.7.0) gives the operation it was "
    "renamed to.",
    {
        "type": "object",
        "properties": {
            "op": {"type": "string", "description": "Operation or batch operation kind."}
        },
        "additionalProperties": False,
    },
    output=_HELP_OUTPUT,
)
def help_(request: dict[str, Any]) -> dict[str, Any]:
    load_operations()
    name = request.get("op")
    name = canonical(name) if name is not None else None
    kinds = _batch_kinds()
    if name is None:
        return {
            "operations": {n: _describe(n) for n in sorted(REGISTRY)},
            "batch_operations": {k: _describe_kind(k, s) for k, s in sorted(kinds.items())},
        }
    if name not in REGISTRY and name not in kinds:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown operation: {name}", op=name)
    # A name can be both (text.replace is a direct write tool and a doc.batch kind): both.
    out: dict[str, Any] = {}
    if name in REGISTRY:
        out["operations"] = {name: {**_describe(name), "output_schema": REGISTRY[name].output}}
    if name in kinds:
        out["batch_operations"] = {name: _describe_kind(name, kinds[name])}
    return out


def _batch_kinds() -> dict[str, dict[str, Any]]:
    """The operation kinds ``doc.batch`` takes in ``ops`` (some are not operations of their own)."""
    from doc_control.ops.batch import ALL_SCHEMAS

    return dict(ALL_SCHEMAS)


def _describe_kind(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "batch",
        "summary": schema.get("description", f"`{name}` inside doc.batch ops; see its schema."),
        "schema": schema,
    }


def _describe(name: str) -> dict[str, Any]:
    op = REGISTRY[name]
    return {"kind": op.kind, "summary": op.summary, "schema": op.schema}
