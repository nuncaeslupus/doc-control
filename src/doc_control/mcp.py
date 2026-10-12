"""MCP front door: `doc-control mcp` serves every registered operation as a tool, on stdio.

A thin wrapper over the operation registry (decision D-8): one tool per operation, the
operation's JSON Schema as the tool's input schema, and the usual response envelope as
the tool result. The transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, which
is all a tools-only MCP server needs, so this stays standard library only.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any

from doc_control import __version__
from doc_control.cli import execute
from doc_control.registry import Operation, load_operations

# Protocol revisions this server speaks; the newest is offered to a client that asks for another.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def tool_name(op: str) -> str:
    """Operation names have dots (`para.move`); many MCP clients only accept [A-Za-z0-9_-]."""
    return op.replace(".", "_")


def _tools(registry: dict[str, Operation]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool_name(op.name),
            "description": op.summary,
            "inputSchema": op.schema,
            "annotations": {"readOnlyHint": op.kind == "read"},
        }
        for op in sorted(registry.values(), key=lambda o: o.name)
    ]


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
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "doc-control", "version": __version__},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tools(registry)}
    elif method == "tools/call":
        ops = {tool_name(name): name for name in registry}
        name, arguments = params.get("name"), params.get("arguments", {})
        if name not in ops or not isinstance(arguments, dict):
            return _error(msg_id, -32602, f"unknown tool or invalid arguments: {name}")
        envelope, _ = execute(registry, ops[name], arguments)
        result = {
            "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}],
            "isError": not envelope["ok"],
        }
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
