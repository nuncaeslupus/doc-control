"""Bundled recipes ported from docx-fixer's automatic rules, with their round-trip cases.

The gate ``docx_fixer_general_cases_failing`` counts the cases in ``docx_fixer_cases.py``
(and the combined fixed-point document) that do not come out as expected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_control.ops.revision import ResolveRevisions, apply_resolutions
from doc_control.recipes.schema import validate_recipe
from doc_control.recipes.store import BUNDLED_DIR, catalogue, parse_recipe_file
from doc_control.registry import REGISTRY, load_operations
from tests.recipes.docx_fixer_cases import APPLY_ORDER, CASES, EM, SMOKE
from tests.recipes.recipe_harness import (
    after,
    apply_recipe,
    fresh,
    proposal,
    revisions,
    texts,
)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    load_operations()
    monkeypatch.setenv("DOC_CONTROL_HOME", str(tmp_path / "home"))


def test_dialogue_dash_recipe_fixes_hyphen_openers(tmp_path: Path) -> None:
    """`- Hola` becomes `—Hola`, as a tracked change."""
    doc = fresh(tmp_path, "- Hola", "Narración")
    batch = proposal(doc, "es-dialogue-dash")
    assert [(o["op"], o["text"]) for o in batch["ops"]] == [("text.replace", EM)]
    assert apply_recipe(doc, "es-dialogue-dash") == 1
    assert texts(doc) == [f"{EM}Hola", "Narración"]
    assert revisions(doc) >= 1
    assert apply_recipe(doc, "es-dialogue-dash") == 0  # already clean


def test_every_bundled_recipe_is_valid_and_listed() -> None:
    names = {p.stem for p in BUNDLED_DIR.glob("*.json")}
    assert set(APPLY_ORDER) <= names
    for path in BUNDLED_DIR.glob("*.json"):
        validate_recipe(parse_recipe_file(path))
    cat = catalogue()
    assert cat.invalid == []
    assert {n for n, (source, _) in cat.recipes.items() if source == "bundled"} == names
    listed = REGISTRY["recipe.list"].run({})
    assert isinstance(listed, dict)
    assert {r["name"] for r in listed["recipes"]} >= set(APPLY_ORDER)


def test_every_ported_recipe_has_changing_and_untouched_cases() -> None:
    assert {recipe for recipe, _, _ in CASES} == set(APPLY_ORDER)
    for recipe in APPLY_ORDER:
        mine = [c for c in CASES if c[0] == recipe]
        assert any(c[1] != c[2] for c in mine), recipe
        assert any(c[1] == c[2] for c in mine), recipe


@pytest.mark.parametrize(("recipe", "before", "expected"), CASES)
def test_round_trip_case(tmp_path: Path, recipe: str, before: str, expected: str) -> None:
    assert after(tmp_path, recipe, before) == [expected]


def test_changes_are_tracked_and_reject_all_restores_the_text(tmp_path: Path) -> None:
    original = ["- Hola  -dijo ella-. ", "Espera..."]
    doc = fresh(tmp_path, *original)
    for recipe in APPLY_ORDER:
        apply_recipe(doc, recipe)
    assert texts(doc) != original
    assert revisions(doc) > 0
    apply_resolutions(doc, [ResolveRevisions("reject")], env={})
    assert texts(doc) == original


def test_combined_recipes_reach_a_fixed_point(tmp_path: Path) -> None:
    doc = fresh(tmp_path, *(before for before, _ in SMOKE))
    for recipe in APPLY_ORDER:
        apply_recipe(doc, recipe)
    assert texts(doc) == [expected for _, expected in SMOKE]
    assert [r for r in APPLY_ORDER if proposal(doc, r)["ops"]] == []


def test_docx_fixer_general_cases_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failing: list[str] = []
    for n, (recipe, before, expected) in enumerate(CASES):
        if after(tmp_path / f"c{n}", recipe, before) != [expected]:
            failing.append(f"{n}:{recipe}")
    doc = fresh(tmp_path / "smoke", *(before for before, _ in SMOKE))
    for recipe in APPLY_ORDER:
        apply_recipe(doc, recipe)
    failing += [f"smoke:{i}" for i, (_, e) in enumerate(SMOKE) if texts(doc)[i] != e]
    with capsys.disabled():
        print(f"docx_fixer_general_cases_failing={len(failing)} cases={len(CASES) + len(SMOKE)}")
    assert failing == []


def test_recipes_leave_clean_text_alone(tmp_path: Path) -> None:
    doc = fresh(tmp_path, "Texto limpio.", "Otro párrafo, igual de limpio.")
    for recipe in APPLY_ORDER:
        assert proposal(doc, recipe)["ops"] == []
