"""Direct write tools: one edit as its own operation, answered exactly as ``doc.batch``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.mcp import handle, tool_name
from doc_control.ops.batch import ALL_SCHEMAS
from doc_control.ops.direct import DIRECT
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import make_docx

DIRECT_TOOLS = (
    "text.replace",
    "para.insert",
    "para.delete",
    "comment.add",
    "revision.accept",
    "revision.reject",
)


def _paras(doc: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = api.run("para.list", {"doc": str(doc)}).result["paragraphs"]
    return rows


def _texts(doc: Path) -> list[str]:
    return [p["text"] for p in _paras(doc)]


def _revisions(doc: Path) -> list[dict[str, Any]]:
    revs: list[dict[str, Any]] = api.run("revision.list", {"doc": str(doc)}).result["revisions"]
    return revs


def _with_insertion(tmp_path: Path) -> Path:
    doc = make_docx(tmp_path / "d.docx", "One.", "Two.")
    api.run("para.insert", {"doc": str(doc), "after": _paras(doc)[0]["at"], "text": "New."})
    assert _texts(doc) == ["One.", "New.", "Two."] and _revisions(doc)
    return doc


def test_the_direct_tools_are_registered_writes() -> None:
    load_operations()
    assert set(DIRECT) == set(DIRECT_TOOLS)
    for name in DIRECT_TOOLS:
        op = REGISTRY[name]
        assert op.kind == "write" and name in ALL_SCHEMAS
        assert "doc.batch" in op.summary and op.schema["required"][0] == "doc"
        own = ALL_SCHEMAS[name]
        assert set(own["properties"]) - {"op"} <= set(op.schema["properties"])
        assert set(op.schema["required"]) == {"doc", *own["required"]} - {"op"}


def test_engine_is_offered_only_where_the_live_engine_applies_the_kind() -> None:
    load_operations()
    live = {n for n in DIRECT_TOOLS if "engine" in REGISTRY[n].schema["properties"]}
    assert live == {"text.replace", "para.insert", "comment.add"}
    with pytest.raises(DocControlError) as err:
        api.run("para.delete", {"doc": "x.docx", "at": "p1#00000000", "engine": "live"})
    assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE


def test_text_replace_applies_one_tracked_replacement(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", "Hello world.")
    at = _paras(doc)[0]["at"] + ":6-11"
    preview = api.run("text.replace", {"doc": str(doc), "at": at, "text": "there", "dry_run": True})
    assert preview.result["dry_run"] is True and _texts(doc) == ["Hello world."]
    reply = api.run("text.replace", {"doc": str(doc), "at": at, "text": "there"})
    assert reply.result["applied"] == 1 and _texts(doc) == ["Hello there."]
    assert {r["type"] for r in _revisions(doc)} >= {"insert", "delete"}


def test_text_replace_answers_exactly_as_the_batch_does(tmp_path: Path) -> None:
    one = make_docx(tmp_path / "a.docx", "Hello world.")
    two = make_docx(tmp_path / "b.docx", "Hello world.")
    at = _paras(one)[0]["at"] + ":0-5"
    direct = api.run("text.replace", {"doc": str(one), "at": at, "text": "Hi", "dry_run": True})
    op = {"op": "text.replace", "at": at, "text": "Hi"}
    batch = api.run("doc.batch", {"doc": str(two), "ops": [op], "dry_run": True})
    assert direct.result == batch.result


def test_expect_guards_a_direct_write(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", "Hello world.")
    request = {"doc": str(doc), "at": _paras(doc)[0]["at"], "expect": {"sha256": "0" * 64}}
    with pytest.raises(DocControlError) as err:
        api.run("para.delete", request)
    assert err.value.code is ErrorCode.DOC_CHANGED_ON_DISK


def test_para_insert_and_delete_are_tracked_and_undoable(tmp_path: Path) -> None:
    doc = _with_insertion(tmp_path)
    reply = api.run("para.delete", {"doc": str(doc), "at": _paras(doc)[0]["at"]})
    assert reply.result["applied"] == 1
    assert any(r["type"] == "delete" for r in _revisions(doc))
    api.run("history.undo", {"doc": str(doc)})
    assert not any(r["type"] == "delete" for r in _revisions(doc))


def test_comment_add_adds_one_comment(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", "Hello world.")
    at = _paras(doc)[0]["at"]
    api.run("comment.add", {"doc": str(doc), "at": at, "text": "Check this.", "author": "Ana"})
    (comment,) = api.run("comment.list", {"doc": str(doc)}).result["comments"]
    assert comment["text"] == "Check this." and comment["author"] == "Ana"


def test_revision_accept_resolves_the_insertion(tmp_path: Path) -> None:
    doc = _with_insertion(tmp_path)
    api.run("revision.accept", {"doc": str(doc), "all": True})
    assert _revisions(doc) == [] and _texts(doc) == ["One.", "New.", "Two."]


def test_revision_reject_takes_the_insertion_out(tmp_path: Path) -> None:
    doc = _with_insertion(tmp_path)
    ids = [r["id"] for r in _revisions(doc)]
    api.run("revision.reject", {"doc": str(doc), "ids": ids})
    assert _revisions(doc) == [] and _texts(doc) == ["One.", "Two."]


def test_revision_author_filters_and_is_never_filled_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`author` picks whose revisions to resolve, so DOC_CONTROL_AUTHOR must not narrow it."""
    monkeypatch.delenv("DOC_CONTROL_AUTHOR", raising=False)
    doc = _with_insertion(tmp_path)  # by the default author, Claude
    monkeypatch.setenv("DOC_CONTROL_AUTHOR", "Someone Else")
    api.run("revision.accept", {"doc": str(doc), "author": "Nobody", "all": True})
    assert _revisions(doc)  # the filter matched none
    api.run("revision.accept", {"doc": str(doc), "all": True})
    assert _revisions(doc) == []


def test_help_on_a_direct_tool_gives_the_operation_and_the_batch_kind() -> None:
    load_operations()
    for name in DIRECT_TOOLS:
        result = api.run("server.help", {"op": name}).result
        assert list(result["operations"]) == [name]
        assert list(result["batch_operations"]) == [name]
    only_kind = api.run("server.help", {"op": "para.move"}).result
    assert list(only_kind) == ["batch_operations"]


def test_direct_tools_are_mcp_writes_not_destructive() -> None:
    reply = handle(load_operations(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply is not None
    tools = {tool["name"]: tool for tool in reply["result"]["tools"]}
    for name in DIRECT_TOOLS:
        hints = tools[tool_name(name)]["annotations"]
        assert hints["readOnlyHint"] is False and hints["destructiveHint"] is False
