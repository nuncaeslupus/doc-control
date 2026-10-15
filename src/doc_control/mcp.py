"""MCP front door: `doc-control mcp` serves every registered operation as a tool, on stdio.

A thin wrapper over the operation registry (decision D-8): one tool per operation, the
operation's JSON Schema as the tool's input schema, and the usual response envelope as
the tool result: as JSON text, and on success also as ``structuredContent``, which the
tool's ``outputSchema`` (the envelope with the operation's output schema as ``result``)
describes. The transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, which
is all a tools-only MCP server needs, so this stays standard library only.

One tool also has a UI (the MCP Apps extension, ``io.modelcontextprotocol/ui``):
``live_places`` names the page ``app/places.html`` as a ``ui://`` resource, and a client
that renders MCP Apps shows it with the tool's result; its buttons call ``live_select``
through the client.
"""

from __future__ import annotations

import hashlib
import json
import sys
from functools import cache
from pathlib import Path
from typing import IO, Any

from doc_control import __version__
from doc_control.cli import execute
from doc_control.outputs import envelope_schema
from doc_control.registry import ALIASES, Operation, load_operations

# Protocol revisions this server speaks; the newest is offered to a client that asks for another.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME = "text/html;profile=mcp-app"
# Tool name -> the page that renders its result.
APPS = {"live_places": Path(__file__).with_name("app") / "places.html"}


@cache
def _app(tool: str) -> tuple[str, str]:
    """``(uri, html)`` for a tool's page; the URI carries a hash so a client never shows a
    page cached from another version."""
    html = APPS[tool].read_text(encoding="utf-8")
    digest = hashlib.sha256(html.encode()).hexdigest()[:8]
    return f"ui://doc-control/{APPS[tool].stem}-{digest}.html", html


def _resources() -> list[dict[str, Any]]:
    return [
        {
            "uri": _app(tool)[0],
            "name": APPS[tool].stem,
            "mimeType": UI_MIME,
            "_meta": {"ui": {"prefersBorder": True}},
        }
        for tool in sorted(APPS)
    ]


def tool_name(op: str) -> str:
    """Operation names have dots (`para.move`); many MCP clients only accept [A-Za-z0-9_-]."""
    return op.replace(".", "_")


# Writes that drop something a later call cannot take back from the document itself:
# purged history, discarded unsaved edits, merged-away styles.
DESTRUCTIVE = frozenset({"history.purge", "live.close", "style.merge"})
# Reads of the document that still create a file: compare's redline, styleset.export's `out`.
CREATES_FILES = frozenset({"doc.compare", "styleset.export"})
# Write-kind operations that change nothing: recipe.run only proposes a batch for doc.batch.
PROPOSES = frozenset({"recipe.run"})


def _annotations(op: Operation) -> dict[str, bool]:
    """MCP tool hints: reads are safe to repeat; most writes are tracked changes, backed up
    and undoable, so only the few in DESTRUCTIVE are marked destructive."""
    if (op.kind == "read" and op.name not in CREATES_FILES) or op.name in PROPOSES:
        return {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
    return {
        "readOnlyHint": False,
        "destructiveHint": op.name in DESTRUCTIVE,
        "openWorldHint": False,
    }


def _tools(registry: dict[str, Operation]) -> list[dict[str, Any]]:
    tools = []
    for op in sorted(registry.values(), key=lambda o: o.name):
        tool: dict[str, Any] = {
            "name": tool_name(op.name),
            "description": op.summary,
            "inputSchema": op.schema,
            "annotations": _annotations(op),
        }
        if op.output is not None:
            tool["outputSchema"] = envelope_schema(op.output)
        if tool["name"] in APPS:
            tool["_meta"] = {
                "ui": {"resourceUri": _app(tool["name"])[0], "visibility": ["model", "app"]}
            }
        tools.append(tool)
    return tools


def handle(registry: dict[str, Operation], message: Any) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; None for a notification."""
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return _error(None, -32600, "invalid request")
    if "id" not in message:
        return None
    msg_id, method = message["id"], message["method"]
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        result: dict[str, Any] = {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {
                "tools": {},
                "resources": {},
                "extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME]}},
            },
            "serverInfo": {"name": "doc-control", "version": __version__},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tools(registry)}
    elif method == "resources/list":
        result = {"resources": _resources()}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "resources/read":
        pages = {_app(tool)[0]: _app(tool)[1] for tool in APPS}
        uri = params.get("uri")
        if uri not in pages:
            return _error(msg_id, -32002, f"resource not found: {uri}")
        result = {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": UI_MIME,
                    "text": pages[uri],
                    "_meta": {"ui": {"prefersBorder": True}},
                }
            ]
        }
    elif method == "tools/call":
        ops = {tool_name(old): new for old, new in ALIASES.items()}  # old names still run
        ops.update({tool_name(name): name for name in registry})
        name, arguments = params.get("name"), params.get("arguments", {})
        if name not in ops or not isinstance(arguments, dict):
            return _error(msg_id, -32602, f"unknown tool or invalid arguments: {name}")
        envelope, _ = execute(registry, ops[name], arguments)
        result = {
            "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}],
            "isError": not envelope["ok"],
        }
        if envelope["ok"]:
            # The envelope, as the text holds it; an error reply has none (it is not a result).
            result["structuredContent"] = envelope
            if name in APPS:
                result["_meta"] = {"ui": {"resourceUri": _app(name)[0]}}
    else:
        return _error(msg_id, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(stdin: IO[bytes] | None = None, stdout: IO[bytes] | None = None) -> int:
    """Serve until stdin closes. Bytes in and out, so the encoding is UTF-8 on every platform."""
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    registry = load_operations()
    for line in stdin:
        if not line.strip():
            continue
        try:
            reply = handle(registry, json.loads(line))
        except ValueError:
            reply = _error(None, -32700, "parse error")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
            stdout.flush()
    return 0
