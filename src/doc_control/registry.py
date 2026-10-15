"""Operation registry: one JSON Schema per operation, shared by CLI and MCP."""

from __future__ import annotations

import functools
import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.plaintext import refuse_write
from doc_control.ooxml.revisions import default_author
from doc_control.schema import check_schema, validate

# A handler returns its result dict, or a Reply to also fill `doc`, `warnings` and `next`.
Handler = Callable[[dict[str, Any]], dict[str, Any] | Reply]

#: Called with ``(operation name, what the handler returned)`` after every handler returns.
#: None in use; the test suite sets it to check each result against the operation's ``output``.
RESULT_CHECK: Callable[[str, dict[str, Any] | Reply], None] | None = None


@dataclass(frozen=True)
class Operation:
    name: str
    kind: Literal["read", "write"]
    summary: str
    schema: dict[str, Any]
    handler: Handler
    #: Whether a write's missing ``author`` is filled from DOC_CONTROL_AUTHOR (default);
    #: off where ``author`` selects rather than stamps (revision.accept/reject).
    fills_author: bool = True
    #: JSON Schema of the ``result`` (see :mod:`doc_control.outputs`).
    output: dict[str, Any] | None = None

    def run(self, request: dict[str, Any]) -> dict[str, Any] | Reply:
        if request.get("engine") == "live" and "engine" not in self.schema["properties"]:
            raise DocControlError(
                ErrorCode.ENGINE_UNAVAILABLE,
                f"{self.name} has no live engine",
                reason="operation not supported live",
            )
        validate(request, self.schema)
        if (
            self.kind == "write"
            and self.fills_author
            and "author" in self.schema["properties"]
            and "author" not in request
            and request.get("engine") != "live"
        ):
            request = {**request, "author": default_author()}
        if self.kind == "write" and isinstance(request.get("doc"), str):
            refuse_write(Path(request["doc"]))
        return self.handler(request)


REGISTRY: dict[str, Operation] = {}


def operation(
    name: str,
    kind: Literal["read", "write"],
    summary: str,
    schema: dict[str, Any],
    *,
    fills_author: bool = True,
    output: dict[str, Any] | None = None,
) -> Callable[[Handler], Handler]:
    """Register a handler; the schema (and the ``output`` schema of its result) must be
    well-formed object schemas."""

    def wrap(handler: Handler) -> Handler:
        if name in REGISTRY:
            raise ValueError(f"operation registered twice: {name}")
        for label, value in (("schema", schema), ("output schema", output)):
            if value is None:
                continue
            if value.get("type") != "object":
                raise ValueError(f"{label} for {name} must be an object schema")
            problems = check_schema(value)
            if problems:
                raise ValueError(f"{label} for {name} is malformed: {'; '.join(problems)}")

        @functools.wraps(handler)
        def checked(request: dict[str, Any]) -> dict[str, Any] | Reply:
            out = handler(request)
            if RESULT_CHECK is not None:
                RESULT_CHECK(name, out)
            return out

        REGISTRY[name] = Operation(name, kind, summary, schema, checked, fills_author, output)
        return checked

    return wrap


#: The module (in ``doc_control.ops``) that registers each operation, so one operation
#: can be loaded without importing them all (``tests/test_registry.py`` keeps it true).
OPERATION_MODULES: dict[str, str] = {
    "anchor.create": "anchor",
    "anchor.resolve": "anchor",
    "caption.add": "crossref",
    "comment.add": "direct",
    "comment.list": "read",
    "doc.batch": "batch",
    "doc.compare": "compare",
    "doc.diff": "diff",
    "doc.info": "read",
    "doc.open": "history",
    "doc.outline": "read",
    "doc.segment": "segment",
    "doc.stats": "read",
    "field.list": "fields",
    "field.update": "fieldwrite",
    "history.adopt": "history",
    "history.list": "history",
    "history.orphans": "history",
    "history.purge": "history",
    "history.restore": "history",
    "history.search": "history",
    "history.undo": "history",
    "index.insert": "crossref",
    "index.mark": "crossref",
    "layout.apply": "layout",
    "layout.get": "layout",
    "live.close": "live",
    "live.get_selection": "live",
    "live.open": "live",
    "live.places": "live",
    "live.save": "live",
    "live.select": "live",
    "live.status": "live",
    "note.add": "notes",
    "page.estimate": "page",
    "para.delete": "direct",
    "para.get": "read",
    "para.insert": "direct",
    "para.list": "bulk",
    "para.locate": "locate",
    "recipe.list": "recipe",
    "recipe.run": "recipe",
    "revision.accept": "direct",
    "revision.list": "read",
    "revision.reject": "direct",
    "section.list": "sections",
    "server.capabilities": "meta",
    "server.help": "meta",
    "style.create": "stylewrite",
    "style.get": "read",
    "style.list": "read",
    "style.merge": "stylewrite",
    "style.rename": "stylewrite",
    "style.update": "stylewrite",
    "styleset.export": "styleset",
    "styleset.export_dotx": "dotx",
    "styleset.import_dotx": "dotx",
    "styleset.install": "styleset",
    "table.get": "tables",
    "table.row.delete": "tablerows",
    "table.row.insert": "tablerows",
    "term.rename": "rename",
    "text.find": "find",
    "text.normalize": "text",
    "text.replace": "direct",
    "toc.insert": "fieldwrite",
    "xref.add": "crossref",
}


#: Names operations had before 0.7.0 (``resource.action``, singular resource). Still
#: accepted wherever an operation is run by name; never listed (capabilities, MCP tools).
ALIASES: dict[str, str] = {
    "batch": "doc.batch",
    "capabilities": "server.capabilities",
    "comments.list": "comment.list",
    "compare": "doc.compare",
    "diff": "doc.diff",
    "fields.list": "field.list",
    "fields.update": "field.update",
    "find": "text.find",
    "help": "server.help",
    "info": "doc.info",
    "live.selection": "live.get_selection",
    "locate": "para.locate",
    "open": "doc.open",
    "outline": "doc.outline",
    "paragraphs.list": "para.list",
    "restore": "history.restore",
    "revisions.list": "revision.list",
    "sections.list": "section.list",
    "segment": "doc.segment",
    "stats": "doc.stats",
    "styles.list": "style.list",
    "styleset.export-dotx": "styleset.export_dotx",
    "styleset.import-dotx": "styleset.import_dotx",
    "text.normalized": "text.normalize",
    "undo": "history.undo",
}


def canonical(name: str) -> str:
    """The current name of operation ``name``: itself, or what an old name was renamed to."""
    return ALIASES.get(name, name)


def get_operation(name: str) -> Operation | None:
    """The operation ``name`` (or an old name in ALIASES), importing only the module that
    registers it; None if unknown."""
    name = canonical(name)
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
