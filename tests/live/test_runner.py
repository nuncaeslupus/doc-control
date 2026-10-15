"""The Word suite's runners (PACKAGE T5): the same calls in-process or over MCP stdio."""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from tests.helpers import make_docx
from tests.live import word_suite
from tests.live.word_suite import McpExe

SUITE = Path(word_suite.__file__)


@pytest.fixture
def mcp() -> Iterator[McpExe]:
    runner = McpExe([sys.executable, "-m", "doc_control", "mcp"])
    try:
        yield runner
    finally:
        runner.close()


def test_mcp_runner_maps_error_envelope_to_doc_control_error(mcp: McpExe, tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "one.docx", "One.", "Two.")
    with pytest.raises(DocControlError) as err:
        mcp.run("para.get", {"doc": str(doc), "at": "p1#00000000"})
    assert err.value.code is ErrorCode.STALE_ADDRESS


def test_mcp_runner_joins_capped_pages(mcp: McpExe, tmp_path: Path) -> None:
    texts = [f"Paragraph {n}" + " word" * 60 for n in range(60)]
    doc = make_docx(tmp_path / "long.docx", *texts)
    rows = mcp.run("paragraphs.list", {"doc": str(doc)})["paragraphs"]
    assert [r["text"] for r in rows] == texts
    assert rows == word_suite.InProcess().run("paragraphs.list", {"doc": str(doc)})["paragraphs"]


def _api_calls(node: ast.AST) -> list[int]:
    return [
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in ("run", "open")
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "api"
    ]


def test_word_suite_calls_doc_control_only_through_runner() -> None:
    tree = ast.parse(SUITE.read_text(encoding="utf-8"))
    allowed = {
        line
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "InProcess"
        for line in _api_calls(node)
    }
    assert allowed, "InProcess no longer calls api.run"
    assert [line for line in _api_calls(tree) if line not in allowed] == []
