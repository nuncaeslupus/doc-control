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


def test_errors_md_is_generated() -> None:
    assert (DOCS / "errors.md").read_text("utf-8") == errors_markdown()


def test_operations_md_is_generated() -> None:
    """FIND T10: no drift, and every op taking ``match_level`` documents it and "exact"."""
    text = (DOCS / "operations.md").read_text("utf-8")
    assert text == operations_markdown(), (
        "run `uv run python -m doc_control.reference` and commit docs/"
    )
    levelled = sorted(n for n, op in REGISTRY.items() if "match_level" in op.schema["properties"])
    assert levelled == [
        "anchor.resolve",
        "find",
        "history.search",
        "live.select",
        "term.rename",
    ]
    for name in levelled:
        section = text.split(f"## `{name}` (", 1)[1].split("\n## ", 1)[0]
        row = next(line for line in section.splitlines() if line.startswith("| `match_level` |"))
        assert "`exact`" in row and "`default`" in row and "`loose`" in row, name
    search = text.split("## `history.search` (", 1)[1].split("\n## ", 1)[0]
    assert "`start` and `end`" in search


def test_operation_reference_names_every_operation() -> None:
    load_operations()
    text = (DOCS / "operations.md").read_text("utf-8")
    assert [name for name in REGISTRY if f"## `{name}`" not in text] == []
