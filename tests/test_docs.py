"""Documentation (T29): the generated references are current and complete; the README's
examples run (LAYOUT T9)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from doc_control import api
from doc_control.errors import ErrorCode
from doc_control.reference import errors_markdown, operations_markdown
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import generate

DOCS = Path(__file__).resolve().parent.parent / "docs"
README = DOCS.parent / "README.md"
#: The README's front-matter plan example: its one `doc-control layout.apply` call.
LAYOUT_EXAMPLE = re.compile(r"doc-control layout\.apply --json '(\{.*?\})'", re.DOTALL)


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
        "history.search",
        "live.select",
        "term.rename",
        "text.find",
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


def _readme_layout_request() -> dict[str, Any]:
    found = LAYOUT_EXAMPLE.findall(README.read_text("utf-8"))
    assert len(found) == 1, "README.md should hold exactly one `doc-control layout.apply` call"
    request: dict[str, Any] = json.loads(found[0])
    return request


def _plan_mismatches(want: Any, got: Any, path: str = "plan") -> list[str]:
    """Where ``got`` differs from ``want``, on the keys ``want`` has. A section's ``from``
    is compared by its paragraph hash: a new section break renumbers the paragraphs after it."""
    if isinstance(want, dict) and isinstance(got, dict):
        return [
            d
            for key, value in want.items()
            for d in _plan_mismatches(value, got.get(key), f"{path}.{key}")
        ]
    if isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            return [f"{path}: {len(got)} entries, not {len(want)}"]
        return [
            d
            for i, (w, g) in enumerate(zip(want, got, strict=True))
            for d in _plan_mismatches(w, g, f"{path}[{i}]")
        ]
    if path.endswith(".from") and isinstance(want, str) and isinstance(got, str):
        same = want.partition("#")[2] == got.partition("#")[2]
        return [] if same else [f"{path}: {got!r}, not {want!r}"]
    return [] if want == got else [f"{path}: {got!r}, not {want!r}"]


def test_readme_layout_example_runs(tmp_path: Path) -> None:
    """The README's front-matter plan applies, as written, to the novel corpus document,
    and `layout.get` then returns that plan."""
    request = _readme_layout_request()
    plan = request["plan"]
    path = tmp_path / Path(request["doc"]).name
    path.write_bytes(generate("novel", 1))
    with api.open(path, store=tmp_path / "store") as doc:
        doc.run("layout.apply", {k: v for k, v in request.items() if k != "doc"})
        got = doc.run("layout.get", {})
    assert len(got["sections"]) == len(plan["sections"]) > 1
    assert _plan_mismatches(plan, got) == []
