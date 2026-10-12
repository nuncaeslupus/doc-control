"""The MCP front door: the stdio JSON-RPC loop over the operation registry."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from io import BytesIO
from typing import Any

from doc_control import __version__
from doc_control.mcp import PROTOCOL_VERSIONS, serve, tool_name
from doc_control.registry import load_operations


def talk(*messages: Any) -> list[dict[str, Any]]:
    raw = b"".join(
        (m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n" for m in messages
    )
    out = BytesIO()
    assert serve(BytesIO(raw), out) == 0
    return [json.loads(line) for line in out.getvalue().splitlines()]


def call(msg_id: int, method: str, **params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}


def test_initialize_negotiates_the_protocol_version() -> None:
    known, unknown = talk(
        call(1, "initialize", protocolVersion="2024-11-05"),
        call(2, "initialize", protocolVersion="1999-01-01"),
    )
    assert known["result"]["protocolVersion"] == "2024-11-05"
    assert unknown["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]
    assert known["result"]["serverInfo"] == {"name": "doc-control", "version": __version__}
    assert known["result"]["capabilities"] == {"tools": {}}


def test_notifications_get_no_reply_and_ping_does() -> None:
    replies = talk({"jsonrpc": "2.0", "method": "notifications/initialized"}, call(7, "ping"))
    assert replies == [{"jsonrpc": "2.0", "id": 7, "result": {}}]


def test_every_operation_is_listed_as_a_tool_with_its_schema() -> None:
    registry = load_operations()
    (reply,) = talk(call(1, "tools/list"))
    tools = {tool["name"]: tool for tool in reply["result"]["tools"]}
    assert len(tools) == len(registry)  # no two operations collapse into one tool name
    for op in registry.values():
        tool = tools[tool_name(op.name)]
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool["name"])
        assert tool["inputSchema"] == op.schema
        assert tool["description"] == op.summary
        assert tool["annotations"] == {"readOnlyHint": op.kind == "read"}


def test_tool_call_returns_the_envelope() -> None:
    (reply,) = talk(call(1, "tools/call", name="capabilities", arguments={}))
    result = reply["result"]
    assert result["isError"] is False
    envelope = json.loads(result["content"][0]["text"])
    assert envelope["ok"] is True
    assert envelope["op"] == "capabilities"
    assert envelope["result"]["version"] == __version__


def test_operation_error_is_a_tool_error_not_a_protocol_error() -> None:
    (reply,) = talk(call(1, "tools/call", name="help", arguments={"op": "no.such.op"}))
    assert reply["result"]["isError"] is True
    envelope = json.loads(reply["result"]["content"][0]["text"])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "SCHEMA_INVALID"


def test_protocol_errors() -> None:
    parse, invalid, method, tool, later = talk(
        b"{not json",
        [1, 2],
        call(3, "resources/list"),
        call(4, "tools/call", name="no_such_tool", arguments={}),
        call(5, "ping"),
    )
    assert parse["error"]["code"] == -32700
    assert invalid["error"]["code"] == -32600
    assert method["error"]["code"] == -32601
    assert tool["error"]["code"] == -32602
    assert later == {"jsonrpc": "2.0", "id": 5, "result": {}}  # the loop survives bad input


def test_cli_mcp_subcommand_serves_on_stdio() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "doc_control", "mcp"],
        input=json.dumps(call(1, "tools/call", name="capabilities", arguments={})) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    (line,) = proc.stdout.splitlines()
    assert json.loads(json.loads(line)["result"]["content"][0]["text"])["ok"] is True
