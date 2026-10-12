"""Operation registry: one JSON Schema per operation, shared by CLI and MCP."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from doc_control.envelope import Reply
from doc_control.schema import check_schema, validate

# A handler returns its result dict, or a Reply to also fill `doc`, `warnings` and `next`.
Handler = Callable[[dict[str, Any]], dict[str, Any] | Reply]


@dataclass(frozen=True)
class Operation:
    name: str
    kind: Literal["read", "write"]
    summary: str
    schema: dict[str, Any]
    handler: Handler

    def run(self, request: dict[str, Any]) -> dict[str, Any] | Reply:
        validate(request, self.schema)
        return self.handler(request)


REGISTRY: dict[str, Operation] = {}


def operation(
    name: str, kind: Literal["read", "write"], summary: str, schema: dict[str, Any]
) -> Callable[[Handler], Handler]:
    """Register a handler; the schema must be a well-formed object schema."""

    def wrap(handler: Handler) -> Handler:
        if name in REGISTRY:
            raise ValueError(f"operation registered twice: {name}")
        if schema.get("type") != "object":
            raise ValueError(f"schema for {name} must be an object schema")
        problems = check_schema(schema)
        if problems:
            raise ValueError(f"schema for {name} is malformed: {'; '.join(problems)}")
        REGISTRY[name] = Operation(name, kind, summary, schema, handler)
        return handler

    return wrap


def load_operations() -> dict[str, Operation]:
    """Import every operation module so their decorators run."""
    from doc_control.ops import find, meta, page, read  # noqa: F401

    return REGISTRY
