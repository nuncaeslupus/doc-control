"""Documentation (T29): the generated references are current and complete."""

from __future__ import annotations

from pathlib import Path

from doc_control.errors import ErrorCode
from doc_control.reference import errors_markdown, operations_markdown
from doc_control.registry import REGISTRY, load_operations

DOCS = Path(__file__).resolve().parent.parent / "docs"


def test_docs_list_every_error_code() -> None:
    text = (DOCS / "errors.md").read_text("utf-8")
    missing = [code.value for code in ErrorCode if f"`{code.value}`" not in text]
    assert missing == []


def test_generated_references_are_current() -> None:
    assert (DOCS / "errors.md").read_text("utf-8") == errors_markdown()
    assert (DOCS / "operations.md").read_text("utf-8") == operations_markdown(), (
        "run `uv run python -m doc_control.reference` and commit docs/"
    )


def test_operation_reference_names_every_operation() -> None:
    load_operations()
    text = (DOCS / "operations.md").read_text("utf-8")
    assert [name for name in REGISTRY if f"## `{name}`" not in text] == []
