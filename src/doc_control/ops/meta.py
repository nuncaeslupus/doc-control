"""Self-description: capabilities and help."""

from __future__ import annotations

from typing import Any

from doc_control import API_VERSION, __version__
from doc_control.errors import HINTS, DocControlError, ErrorCode
from doc_control.live.dispatch import apps
from doc_control.registry import REGISTRY, load_operations, operation

_EMPTY: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


@operation("capabilities", "read", "API version, engines and the list of operations.", _EMPTY)
def capabilities(_: dict[str, Any]) -> dict[str, Any]:
    load_operations()
    return {
        "api": API_VERSION,
        "version": __version__,
        "engines": ["file", "live"],
        "live": {"apps": apps()},
        "operations": [
            {"name": op.name, "kind": op.kind, "summary": op.summary}
            for op in sorted(REGISTRY.values(), key=lambda o: o.name)
        ],
        "errors": sorted(code.value for code in HINTS),
    }


@operation(
    "help",
    "read",
    "JSON Schema of one operation, or of all operations; also the kinds batch takes in `ops` "
    "(text.replace, comment.add…), listed under batch_operations.",
    {
        "type": "object",
        "properties": {
            "op": {"type": "string", "description": "Operation or batch operation kind."}
        },
        "additionalProperties": False,
    },
)
def help_(request: dict[str, Any]) -> dict[str, Any]:
    load_operations()
    name = request.get("op")
    kinds = _batch_kinds()
    if name is None:
        return {
            "operations": {n: _describe(n) for n in sorted(REGISTRY)},
            "batch_operations": {k: _describe_kind(k, s) for k, s in sorted(kinds.items())},
        }
    if name in kinds and name not in REGISTRY:
        return {"batch_operations": {name: _describe_kind(name, kinds[name])}}
    if name not in REGISTRY:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown operation: {name}", op=name)
    return {"operations": {name: _describe(name)}}


def _batch_kinds() -> dict[str, dict[str, Any]]:
    """The operation kinds a ``batch`` takes in ``ops`` (some are not operations of their own)."""
    from doc_control.ops.batch import ALL_SCHEMAS

    return dict(ALL_SCHEMAS)


def _describe_kind(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "batch",
        "summary": schema.get("description", f"`{name}` inside batch ops; see its schema."),
        "schema": schema,
    }


def _describe(name: str) -> dict[str, Any]:
    op = REGISTRY[name]
    return {"kind": op.kind, "summary": op.summary, "schema": op.schema}
