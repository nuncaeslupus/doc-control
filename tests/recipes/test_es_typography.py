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
from tests.recipes.docx_fixer_cases import APPLY_ORDER, EM, SMOKE
from tests.recipes.recipe_harness import (
    after,
    apply_recipe,
    fresh,
    proposal,
    revisions,
    texts,
)

CASES: dict[str, list[tuple[str, str]]] = {
    "es-dialogue-dash": [
        ("- Hola", "—Hola"),
        ("-Hola", "—Hola"),
        ("– Hola", "—Hola"),
        ("--Hola", "—Hola"),
        ("——Hola", "—Hola"),
        ("— Hola", "—Hola"),
        ("-\xa0¿Qué tal?", "—¿Qué tal?"),
        ("  - Hola", "  —Hola"),
        ("—Hola", "—Hola"),
        ("Un guion - suelto", "Un guion - suelto"),
        ("-", "-"),
        ("-5 grados", "-5 grados"),
        ("- 5 grados", "- 5 grados"),
        ("- Item uno", "—Item uno"),
    ],
    "es-aside-dash": [
        ("—Hola -dijo ella-. Adiós", "—Hola —dijo ella—. Adiós"),
        ("—Hola -dijo ella- y siguió", "—Hola —dijo ella— y siguió"),
        ("—Un físico-químico", "—Un físico-químico"),
        ("Sin raya -inicial- no cambia", "Sin raya -inicial- no cambia"),
    ],
    "es-dash-first-aside": [
        ("—Te admiramos— le respondieron.", "—Te admiramos —le respondieron."),
        ("—Hola —dijo ella— y se fue", "—Hola —dijo ella— y se fue"),
    ],
    "es-dash-close-space": [
        ("—Hola —dijo ella —. Adiós", "—Hola —dijo ella—. Adiós"),
        ("—Hola —dijo ella —", "—Hola —dijo ella—"),
        ("—Hola —dijo ella— y siguió", "—Hola —dijo ella— y siguió"),
    ],
    "es-dash-open-space": [
        ("—Hola — dijo ella—. Adiós", "—Hola —dijo ella—. Adiós"),
        ("—Hola —dijo ella—.", "—Hola —dijo ella—."),
        ("Texto — aparte — en inglés", "Texto — aparte — en inglés"),
        ("—Hola — dijo él — y se fue.", "—Hola —dijo él — y se fue."),
    ],
    "collapse-spaces": [
        ("Dos  espacios y   tres", "Dos espacios y tres"),
        ("Duro\xa0 y normal", "Duro y normal"),
        ("  Sangría inicial", "  Sangría inicial"),
        ("Uno solo", "Uno solo"),
    ],
    "trim-trailing-spaces": [
        ("Final con espacios   ", "Final con espacios"),
        ("Final con tabulador\t", "Final con tabulador"),
        ("Limpio", "Limpio"),
    ],
    "es-space-before-punctuation": [
        ("Hola , mundo .", "Hola, mundo."),
        ("¿Qué tal ?", "¿Qué tal?"),
        ("Dijo (mal ) que sí", "Dijo (mal) que sí"),
        ("Sin cambios, de verdad.", "Sin cambios, de verdad."),
        ("y ’tis", "y ’tis"),
        ("‘hola ’ y fin", "‘hola’ y fin"),
    ],
    "es-space-after-opener": [
        ("Dijo ( mal) que sí", "Dijo (mal) que sí"),
        ("¿ Qué?", "¿Qué?"),
        ("« hola»", "«hola»"),
        ("Dijo (mal) que sí", "Dijo (mal) que sí"),
    ],
    "es-space-after-punctuation": [
        ("Uno,dos;tres:cuatro", "Uno, dos; tres: cuatro"),
        ("Hora 10:30 y 3,5", "Hora 10:30 y 3,5"),
        ("Ya, está bien", "Ya, está bien"),
        ("a,b,c", "a, b, c"),
        ("Ver https://ex.com/a,b y listo,gracias", "Ver https://ex.com/a,b y listo, gracias"),
        ("mailto:x@y.es", "mailto:x@y.es"),
        ("Escribe a x@y.es,luego", "Escribe a x@y.es,luego"),
    ],
    "ellipsis-char": [
        ("Espera... y luego", "Espera… y luego"),
        ("Espera…. Y luego", "Espera… Y luego"),
        ("Cuatro.... se queda", "Cuatro.... se queda"),
        ("Ya está…", "Ya está…"),
        ("https://example.com/a...b", "https://example.com/a...b"),
        ("foo.bar(...)", "foo.bar(...)"),
        ("Espera... mira foo.bar(...) y... luego", "Espera… mira foo.bar(...) y… luego"),
        ("Sigue (...algo) aquí", "Sigue (…algo) aquí"),
    ],
    "typographic-apostrophe": [
        ("rock'n'roll y o'clock", "rock’n’roll y o’clock"),
        ("'comillas simples'", "'comillas simples'"),
    ],
    "es-quotes-open": [
        ('Dijo "hola" y se fue', 'Dijo «hola" y se fue'),
        ('"Inicio" de línea', '«Inicio" de línea'),
        ('(¿"Qué?")', '(¿«Qué?")'),
        ('Pulgadas 5 " aparte', 'Pulgadas 5 " aparte'),
    ],
    "es-quotes-close": [
        ('Dijo "hola" y se fue', 'Dijo "hola» y se fue'),
        ('Dijo "hola".', 'Dijo "hola».'),
        ('Dijo "hola"', 'Dijo "hola»'),
        ('Pulgadas 5 " aparte', 'Pulgadas 5 " aparte'),
    ],
    "es-dash-close-pair-space": [
        ("—Hola — dijo él — y se fue.", "—Hola — dijo él— y se fue."),
        ("—Uno —dos — tres —cuatro —.", "—Uno —dos— tres —cuatro—."),
        ("—Hola —dijo él— y se fue.", "—Hola —dijo él— y se fue."),
    ],
}


FLAT = [(r, b, a) for r, items in CASES.items() for b, a in items]


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
    assert set(CASES) == set(APPLY_ORDER)
    for recipe in APPLY_ORDER:
        mine = CASES[recipe]
        assert any(b != a for b, a in mine), recipe
        assert any(b == a for b, a in mine), recipe


@pytest.mark.parametrize(("recipe", "before", "expected"), FLAT)
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
    for n, (recipe, before, expected) in enumerate(FLAT):
        if after(tmp_path / f"c{n}", recipe, before) != [expected]:
            failing.append(f"{n}:{recipe}")
    doc = fresh(tmp_path / "smoke", *(before for before, _ in SMOKE))
    for recipe in APPLY_ORDER:
        apply_recipe(doc, recipe)
    failing += [f"smoke:{i}" for i, (_, e) in enumerate(SMOKE) if texts(doc)[i] != e]
    with capsys.disabled():
        print(f"docx_fixer_general_cases_failing={len(failing)} cases={len(FLAT) + len(SMOKE)}")
    assert failing == []


def test_recipes_leave_clean_text_alone(tmp_path: Path) -> None:
    doc = fresh(tmp_path, "Texto limpio.", "Otro párrafo, igual de limpio.")
    for recipe in APPLY_ORDER:
        assert proposal(doc, recipe)["ops"] == []


def test_aside_dash_changes_are_one_small_edit_per_dash(tmp_path: Path) -> None:
    line = f"{EM}Hola -dijo ella- y luego -pensó él- que no, y más texto largo -añadió- al final."
    doc = fresh(tmp_path, line)
    ops = proposal(doc, "es-aside-dash")["ops"]
    spans = [tuple(map(int, o["at"].split(":")[1].split("-"))) for o in ops]
    assert len(ops) == 6
    assert all(end - start == 1 for start, end in spans)
    assert {o["text"] for o in ops} == {EM}


def test_open_and_close_dash_edits_remove_only_the_spaces(tmp_path: Path) -> None:
    doc = fresh(tmp_path, f"{EM}Hola {EM}  dijo él {EM} y se fue.")
    for recipe in ("es-dash-open-space", "es-dash-close-pair-space"):
        for op in proposal(doc, recipe)["ops"]:
            start, end = map(int, op["at"].split(":")[1].split("-"))
            assert op["text"] == "" and end > start
            assert set(texts(doc)[0][start:end]) == {" "}
