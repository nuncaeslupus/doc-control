"""Every bundled recipe has a test (T25 gate: `bundled_recipes_without_tests == 0`).

The inventory is the content of ``src/doc_control/recipes/bundled`` (``*.json`` data
recipes and ``*.py`` modules), so a recipe added later by another task is covered without
editing this file. A recipe has a test when a ``test_*.py`` module of this directory (other than
the engine's own, whose fixtures reuse names on purpose) lists it as a ``CASES`` key or passes its
name to a
``propose``/``run`` call, directly or through a ``for`` loop over a literal.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from doc_control.recipes.store import BUNDLED_DIR, catalogue
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import make_docx

HERE = Path(__file__).parent
NOT_COVERAGE = {"test_inventory.py", "test_engine.py"}
CALLEES = {"propose", "run"}


def bundled_names() -> list[str]:
    stems = {p.stem for p in BUNDLED_DIR.iterdir() if p.suffix in {".json", ".py"}}
    return sorted(s for s in stems if not s.startswith("_"))


def _strings(node: ast.AST) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _calls_with(tree: ast.AST, target: str) -> bool:
    """Does ``tree`` call ``propose``/``run`` with the variable ``target`` as an argument?"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node) in CALLEES:
            args = [*node.args, *(k.value for k in node.keywords)]
            if any(isinstance(a, ast.Name) and a.id == target for a in args):
                return True
    return False


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def names_with_tests() -> set[str]:
    """Recipe names that a test module runs: a ``CASES`` key, an argument of a ``propose`` or
    ``run`` call, or an item of a literal that a ``for`` loop feeds to such a call."""
    found: set[str] = set()
    for path in HERE.glob("test_*.py"):
        if path.name in NOT_COVERAGE:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                is_cases = any(isinstance(t, ast.Name) and t.id == "CASES" for t in targets)
                if is_cases and isinstance(node.value, ast.Dict):
                    for key in node.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            found.add(key.value)
            elif isinstance(node, ast.Call) and _callee(node) in CALLEES:
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
            elif (
                isinstance(node, ast.For)
                and isinstance(node.target, ast.Name)
                and isinstance(node.iter, ast.Tuple | ast.List)
                and _calls_with(ast.Module(node.body, []), node.target.id)
            ):
                found.update(_strings(node.iter))
    return found


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
