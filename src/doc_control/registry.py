"""Operation registry: one JSON Schema per operation, shared by CLI and MCP."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.plaintext import refuse_write
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
        if request.get("engine") == "live" and "engine" not in self.schema["properties"]:
            raise DocControlError(
                ErrorCode.ENGINE_UNAVAILABLE,
                f"{self.name} has no live engine",
                reason="operation not supported live",
            )
        validate(request, self.schema)
        if self.kind == "write" and isinstance(request.get("doc"), str):
            refuse_write(Path(request["doc"]))
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


#: The module (in ``doc_control.ops``) that registers each operation, so one operation
#: can be loaded without importing them all (``tests/test_registry.py`` keeps it true).
OPERATION_MODULES: dict[str, str] = {
    "anchor.create": "anchor",
    "anchor.resolve": "anchor",
    "batch": "batch",
    "capabilities": "meta",
    "comments.list": "read",
    "compare": "compare",
    "diff": "diff",
    "fields.list": "fields",
    "find": "find",
    "help": "meta",
    "history.list": "history",
    "history.orphans": "history",
    "history.purge": "history",
    "history.search": "history",
    "info": "read",
    "layout.get": "layout",
    "live.close": "live",
    "live.select": "live",
    "live.selection": "live",
    "live.open": "live",
    "live.save": "live",
    "live.status": "live",
    "locate": "locate",
    "note.add": "notes",
    "open": "history",
    "outline": "read",
    "page.estimate": "page",
    "para.get": "read",
    "paragraphs.list": "bulk",
    "recipe.list": "recipe",
    "recipe.run": "recipe",
    "restore": "history",
    "revisions.list": "read",
    "sections.list": "sections",
    "segment": "segment",
    "stats": "read",
    "style.create": "stylewrite",
    "style.get": "read",
    "style.merge": "stylewrite",
    "style.rename": "stylewrite",
    "style.update": "stylewrite",
    "styles.list": "read",
    "styleset.export": "styleset",
    "styleset.export-dotx": "dotx",
    "styleset.import-dotx": "dotx",
    "styleset.install": "styleset",
    "table.get": "tables",
    "term.rename": "rename",
    "text.normalized": "text",
    "undo": "history",
}


def get_operation(name: str) -> Operation | None:
    """The operation ``name``, importing only the module that registers it; None if unknown."""
    if name in OPERATION_MODULES:
        # Always import (a dict lookup once loaded): while another thread is still
        # importing the module, its operation may be registered before the module's
        # last lines ran; import_module waits on the module lock for those.
        importlib.import_module(f"doc_control.ops.{OPERATION_MODULES[name]}")
    return REGISTRY.get(name)


def load_operations() -> dict[str, Operation]:
    """Import every operation module so their decorators run."""
    for module in sorted(set(OPERATION_MODULES.values())):
        importlib.import_module(f"doc_control.ops.{module}")
    return REGISTRY
