"""The MCP front door: the stdio JSON-RPC loop over the operation registry."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

from doc_control import __version__
from doc_control.mcp import (
    APPS,
    CREATES_FILES,
    DESTRUCTIVE,
    PROTOCOL_VERSIONS,
    UI_EXTENSION,
    UI_MIME,
    serve,
    tool_name,
)
from doc_control.outputs import envelope_schema
from doc_control.registry import ALIASES, load_operations
from doc_control.schema import validate
from tests.helpers import make_docx


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
    assert known["result"]["capabilities"] == {
        "tools": {},
        "resources": {},
        "extensions": {UI_EXTENSION: {"mimeTypes": [UI_MIME]}},
    }


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
        assert op.output is not None
        assert tool["outputSchema"] == envelope_schema(op.output)
        hints = tool["annotations"]
        read_only = op.kind == "read" and op.name not in CREATES_FILES
        assert hints["readOnlyHint"] is read_only
        assert hints["openWorldHint"] is False
        if read_only:
            assert hints["idempotentHint"] is True
        else:
            assert hints["destructiveHint"] is (op.name in DESTRUCTIVE)


def test_tools_list_names_only_current_operations() -> None:
    """Old operation names (registry.ALIASES) still run but are never listed as tools."""
    (reply,) = talk(call(1, "tools/list"))
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert names == {tool_name(name) for name in load_operations()}
    assert not names & {tool_name(old) for old in ALIASES}


def test_an_old_tool_name_still_calls_the_renamed_operation() -> None:
    (reply,) = talk(call(1, "tools/call", name="capabilities", arguments={}))
    envelope = json.loads(reply["result"]["content"][0]["text"])
    assert envelope["ok"] is True and envelope["op"] == "server.capabilities"


def test_tool_call_returns_the_envelope() -> None:
    (reply,) = talk(call(1, "tools/call", name="server_capabilities", arguments={}))
    result = reply["result"]
    assert result["isError"] is False
    envelope = json.loads(result["content"][0]["text"])
    assert envelope["ok"] is True
    assert envelope["op"] == "server.capabilities"
    assert envelope["result"]["version"] == __version__


def test_operation_error_is_a_tool_error_not_a_protocol_error() -> None:
    (reply,) = talk(call(1, "tools/call", name="server_help", arguments={"op": "no.such.op"}))
    assert reply["result"]["isError"] is True
    envelope = json.loads(reply["result"]["content"][0]["text"])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "SCHEMA_INVALID"


def test_protocol_errors() -> None:
    parse, invalid, method, tool, later = talk(
        b"{not json",
        [1, 2],
        call(3, "prompts/list"),
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
        input=json.dumps(call(1, "tools/call", name="server_capabilities", arguments={})) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    (line,) = proc.stdout.splitlines()
    assert json.loads(json.loads(line)["result"]["content"][0]["text"])["ok"] is True


PLACES = {
    "doc": "Novel.docx",
    "places": [
        {"label": "Chapter 7", "at": "p142#a7b2a7b2", "find_phrase": "It was"},
        {"label": "Chapter 8", "find_phrase": "Later\nThat", "find_phrase_across": "next"},
    ],
}


def test_live_places_tool_names_its_app_page() -> None:
    (tools, resources, read, missing, templates) = talk(
        call(1, "tools/list"),
        call(2, "resources/list"),
        call(3, "resources/read", uri="ui://doc-control/places-00000000.html"),
        call(4, "resources/read", uri="ui://doc-control/nope.html"),
        call(6, "resources/templates/list"),
    )
    (tool,) = [t for t in tools["result"]["tools"] if t["name"] == "live_places"]
    uri = tool["_meta"]["ui"]["resourceUri"]
    assert re.fullmatch(r"ui://doc-control/places-[0-9a-f]{8}\.html", uri)
    assert tool["_meta"]["ui"]["visibility"] == ["model", "app"]
    assert "ui/resourceUri" not in tool["_meta"]  # the deprecated flat key
    others = [t for t in tools["result"]["tools"] if t["name"] not in APPS]
    assert all("_meta" not in t for t in others)
    (resource,) = resources["result"]["resources"]
    assert (resource["uri"], resource["mimeType"]) == (uri, UI_MIME)
    assert "error" in read and missing["error"]["code"] == -32002
    assert templates["result"] == {"resourceTemplates": []}

    (page,) = talk(call(5, "resources/read", uri=uri))
    (content,) = page["result"]["contents"]
    assert (content["uri"], content["mimeType"]) == (uri, UI_MIME)
    html = content["text"]
    assert html.startswith("<!doctype html>")
    assert "live_select" in html and "ui/initialize" in html
    assert "<script src" not in html  # self-contained: app frames are sandboxed


def test_live_places_result_carries_structured_content() -> None:
    (reply,) = talk(call(1, "tools/call", name="live_places", arguments=PLACES))
    result = reply["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["result"] == {"mode": "cursor", **PLACES}
    assert result["_meta"]["ui"]["resourceUri"].startswith("ui://doc-control/places-")
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_a_success_carries_the_envelope_as_structured_content(tmp_path: Path) -> None:
    """structuredContent is the envelope the text holds, and it conforms to the tool's
    outputSchema; an error reply has none."""
    doc = make_docx(tmp_path / "a.docx", "Hello world.")
    listed, ok, bad = talk(
        call(1, "tools/list"),
        call(2, "tools/call", name="para_list", arguments={"doc": str(doc)}),
        call(3, "tools/call", name="para_list", arguments={"doc": str(tmp_path / "no.docx")}),
    )
    schema = {t["name"]: t for t in listed["result"]["tools"]}["para_list"]["outputSchema"]
    structured = ok["result"]["structuredContent"]
    assert structured == json.loads(ok["result"]["content"][0]["text"])
    assert structured["result"]["paragraphs"][0]["text"] == "Hello world."
    validate(structured, schema)
    assert bad["result"]["isError"] is True
    assert "structuredContent" not in bad["result"]


def test_live_places_refuses_a_place_with_no_address() -> None:
    bad = [
        {"doc": "a.docx"},
        {"doc": "a.docx", "places": []},
        {"doc": "a.docx", "places": [{"label": "x", "at": ""}]},
        {"doc": "a.docx", "places": [{"label": "x", "at": "p1", "find_phrase_across": "next"}]},
        {"doc": "a.docx", "places": [{"label": "x"}]},
        {"doc": "a.docx", "places": [{"at": "p1#00000000"}]},
        {"doc": "a.docx", "places": [{"label": "x", "at": "p1"}], "mode": "jump"},
    ]
    replies = talk(
        *(call(n, "tools/call", name="live_places", arguments=a) for n, a in enumerate(bad))
    )
    for reply in replies:
        assert reply["result"]["isError"] is True
        assert "structuredContent" not in reply["result"]
        assert "_meta" not in reply["result"]
        envelope = json.loads(reply["result"]["content"][0]["text"])
        assert envelope["error"]["code"] == "SCHEMA_INVALID"


def test_server_json_matches_the_released_package() -> None:
    """The MCP Registry publishes server.json as is, so it must name the PyPI version."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).parent.parent
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    server = json.loads((root / "server.json").read_text())
    assert server["version"] == version == __version__
    assert {p["version"] for p in server["packages"]} == {version}
    assert f"mcp-name: {server['name']}" in (root / "README.md").read_text(encoding="utf-8")
