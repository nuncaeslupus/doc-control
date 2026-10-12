"""Self-description: capabilities and help."""

from __future__ import annotations

from typing import Any

from doc_control import API_VERSION, __version__
from doc_control.errors import HINTS, DocControlError, ErrorCode
from doc_control.registry import REGISTRY, operation

_EMPTY: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


@operation("capabilities", "read", "API version, engines and the list of operations.", _EMPTY)
def capabilities(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "api": API_VERSION,
        "version": __version__,
        "engines": ["file"],
        "operations": [
            {"name": op.name, "kind": op.kind, "summary": op.summary}
            for op in sorted(REGISTRY.values(), key=lambda o: o.name)
        ],
        "errors": sorted(code.value for code in HINTS),
    }


@operation(
    "help",
    "read",
    "JSON Schema of one operation, or of all operations.",
    {
        "type": "object",
        "properties": {"op": {"type": "string", "description": "Operation name."}},
        "additionalProperties": False,
    },
)
def help_(request: dict[str, Any]) -> dict[str, Any]:
    name = request.get("op")
    if name is None:
        return {"operations": {n: _describe(n) for n in sorted(REGISTRY)}}
    if name not in REGISTRY:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown operation: {name}", op=name)
    return {"operations": {name: _describe(name)}}


def _describe(name: str) -> dict[str, Any]:
    op = REGISTRY[name]
    return {"kind": op.kind, "summary": op.summary, "schema": op.schema}
