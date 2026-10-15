"""The CLI and MCP front doors on the live engine (LIVE T7): same replies as the API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.cli import main
from doc_control.errors import DocControlError
from doc_control.live.dispatch import using
from doc_control.live.protocol import FakeAdapter
from doc_control.mcp import handle, tool_name
from doc_control.model.address import fingerprint
from doc_control.registry import load_operations
from tests.helpers import make_docx


def _calls(doc: Path) -> list[tuple[str, dict[str, Any]]]:
    """A session touching every live operation, with two refusals in it."""
    path, first = {"doc": str(doc)}, f"p1#{fingerprint('One.')}"
    edit = {"op": "comment.add", "at": first, "text": "Sure?"}
    return [
        ("live.status", path),
        ("live.open", {**path, "engine": "live"}),
        ("live.select", {**path, "at": first}),
        ("live.select", {**path, "find_phrase": "two", "paragraphs_after": 1}),
        ("live.get_selection", path),
        ("doc.batch", {**path, "engine": "live", "author": "Ana", "ops": [edit]}),
        ("doc.batch", {**path, "engine": "live", "ops": [{"op": "para.delete", "at": first}]}),
        ("live.close", path),  # UNSAVED_CHANGES
        ("live.save", path),
        ("live.close", path),
        ("live.save", path),  # ENGINE_UNAVAILABLE: no longer open
        ("live.places", {**path, "places": [{"label": "One", "at": first}]}),
    ]


def _plain(reply: dict[str, Any]) -> dict[str, Any]:
    """A reply without the history ids, which are new on every run."""
    reply.get("result", {}).pop("op_ids", None)
    return reply


def _api(op: str, request: dict[str, Any]) -> dict[str, Any]:
    try:
        return _plain({"ok": True, "result": api.run(op, request).result})
    except DocControlError as err:
        return {"ok": False, "error": err.to_dict()}


def _cli(capsys: pytest.CaptureFixture[str], op: str, request: dict[str, Any]) -> dict[str, Any]:
    code = main([op, "--json", json.dumps(request)])
    envelope: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert (code == 0) == envelope["ok"]
    return _plain(envelope)


def _mcp(op: str, request: dict[str, Any]) -> dict[str, Any]:
    call = {"name": tool_name(op), "arguments": request}
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}
    reply = handle(load_operations(), message)
    assert reply is not None
    envelope: dict[str, Any] = json.loads(reply["result"]["content"][0]["text"])
    assert reply["result"]["isError"] == (not envelope["ok"])
    return _plain(envelope)


def test_cli_and_mcp_route_live(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = make_docx(tmp_path / "novel.docx", "One.", "Two.", "Three.")
    calls = _calls(doc)
    doors = {"api": _api, "cli": lambda op, request: _cli(capsys, op, request), "mcp": _mcp}
    replies: dict[str, list[dict[str, Any]]] = {}
    for door, run in doors.items():
        with using(FakeAdapter({str(doc): ["One.", "Two.", "Three."]})):
            replies[door] = [run(op, request) for op, request in calls]

    assert {name for name, _ in calls} >= {n for n in load_operations() if n.startswith("live.")}
    assert [r["ok"] for r in replies["api"]] == [True] * 6 + [False] * 2 + [True] * 2 + [
        False,
        True,
    ]
    assert replies["cli"] == replies["mcp"]
    mismatches = sum(
        (front["ok"], front.get("result"), front.get("error"))
        != (ours["ok"], ours.get("result"), ours.get("error"))
        for door in ("cli", "mcp")
        for front, ours in zip(replies[door], replies["api"], strict=True)
    )
    assert mismatches == 0  # live_frontdoor_mismatches

    names = {tool["name"] for tool in handle(load_operations(), _list())["result"]["tools"]}  # type: ignore[index]
    assert {tool_name(n) for n in load_operations() if n.startswith("live.")} <= names


def _list() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
