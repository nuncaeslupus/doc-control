"""Every bundled recipe has a test (T25 gate: `bundled_recipes_without_tests == 0`).

The inventory is the content of ``src/doc_control/recipes/bundled`` (``*.json`` data
recipes and ``*.py`` modules), so a recipe added later by another task is covered without
editing this file. A recipe has a test when its name is quoted in a ``test_*.py`` module of
this directory other than the engine's own (whose fixtures reuse names on purpose).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from doc_control.recipes.store import BUNDLED_DIR, catalogue
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import make_docx

HERE = Path(__file__).parent
NOT_COVERAGE = {"test_inventory.py", "test_engine.py"}


def bundled_names() -> list[str]:
    stems = {p.stem for p in BUNDLED_DIR.iterdir() if p.suffix in {".json", ".py"}}
    return sorted(s for s in stems if not s.startswith("_"))


def names_with_tests() -> set[str]:
    quoted: set[str] = set()
    for path in HERE.glob("test_*.py"):
        if path.name not in NOT_COVERAGE:
            quoted.update(re.findall(r"""["']([a-z0-9][a-z0-9-]*)["']""", path.read_text("utf-8")))
    return quoted


def test_every_bundled_recipe_has_a_test_module(capsys: pytest.CaptureFixture[str]) -> None:
    names = bundled_names()
    assert len(names) >= 15
    tested = names_with_tests()
    without = [n for n in names if n not in tested]
    with capsys.disabled():
        print(f"\nbundled_recipes={len(names)} bundled_recipes_without_tests={len(without)}")
    assert without == [], f"bundled recipes with no test: {without}"


def test_bundled_recipes_are_valid_and_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOC_CONTROL_HOME", str(tmp_path / "home"))
    cat = catalogue()
    assert [b for b in cat.invalid if b["source"] == "bundled"] == []
    declarative = {n for n in bundled_names() if (BUNDLED_DIR / f"{n}.json").exists()}
    assert declarative <= set(cat.recipes)
    assert all(cat.recipes[n][0] == "bundled" for n in declarative)


def test_every_bundled_recipe_runs_on_a_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load_operations()
    monkeypatch.setenv("DOC_CONTROL_HOME", str(tmp_path / "home"))
    doc = make_docx(tmp_path / "a.docx", "Plain text.", "Chapter 1", "***")
    for name in catalogue().recipes:
        reply = REGISTRY["recipe.run"].run({"doc": str(doc), "recipe": name})
        assert reply is not None, name
