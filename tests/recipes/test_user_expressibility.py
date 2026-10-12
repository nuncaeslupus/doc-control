"""Owner-rule expressibility (D-17): the docx-fixer rules that are the owner's own.

The gate ``owner_rules_not_expressible == 0``. docx-fixer carries three kinds of rule that
are specific to one owner and so are not bundled (D-17): the house style table (style ids
and measurements), the house template, and the author's working-note patterns. Each one is
shown here to have a declarative equivalent, a user recipe and/or a user style set, that
passes its case on synthetic documents. The recipes and the style set are plain JSON
fixtures under ``tests/recipes/house``; the test copies them into a throwaway user store,
exactly where an owner would keep them, and runs them through the public operations.
"""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from doc_control.model.document import Document
from doc_control.ops.batch import batch
from doc_control.ops.dotx import styleset_export_dotx, styleset_import_dotx
from doc_control.ops.styleset import styleset_export, styleset_install
from doc_control.ops.stylewrite import apply_style_ops
from doc_control.recipes.store import catalogue
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.structural import validate_structure
from tests.corpus.validator import require_soffice, validate_documents
from tests.helpers import CONTENT_TYPES, RELS

HOUSE = Path(__file__).parent / "house"
STYLESET = HOUSE / "stylesets" / "house-novel.json"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PROSE = "The rain fell on the village all night and nobody went out."


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the operations and give the test a user store holding the house fixtures."""
    load_operations()
    home = tmp_path / "home"
    shutil.copytree(HOUSE, home)
    monkeypatch.setenv("DOC_CONTROL_HOME", str(home))


def styled_docx(path: Path, *paragraphs: tuple[str, str]) -> Path:
    """A valid .docx whose paragraphs are ``(style id or "", text)``; no styles part."""
    body = ""
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body += f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES, zipfile.ZIP_DEFLATED)
        zf.writestr("_rels/.rels", RELS, zipfile.ZIP_DEFLATED)
        zf.writestr("word/document.xml", document, zipfile.ZIP_DEFLATED)
    return path


def paragraphs(path: Path) -> list[tuple[str, str]]:
    """``(style id, visible text)`` of every paragraph, tracked deletions excluded."""
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    out: list[tuple[str, str]] = []
    for p in root.iter(f"{{{W}}}p"):
        style = p.find(f"{{{W}}}pPr/{{{W}}}pStyle")
        text = "".join(t.text or "" for t in p.iter(f"{{{W}}}t"))
        out.append((style.get(f"{{{W}}}val", "") if style is not None else "", text))
    return out


def run_recipe(doc: Path, name: str) -> dict[str, Any]:
    """Run a stored user recipe and apply its proposal with ``batch``; return the proposal."""
    assert catalogue().recipes[name][0] == "user"
    proposal = REGISTRY["recipe.run"].run({"doc": str(doc), "recipe": name}).result  # type: ignore[union-attr]
    assert proposal["stable"] is True and proposal["applied"] is False
    if proposal["batch"]["ops"]:
        batch(proposal["batch"])
    result: dict[str, Any] = proposal
    return result


def install_house(doc: Path) -> None:
    styleset_install({"doc": str(doc), "path": str(STYLESET)})


def style_entries(doc: Path) -> dict[str, dict[str, Any]]:
    exported = styleset_export({"doc": str(doc)}).result["styleset"]  # type: ignore[union-attr]
    return {s["id"]: s for s in exported["styles"]}


# -- one case per owner rule ----------------------------------------------------------


def case_house_style_table(tmp: Path) -> None:
    """House style ids and measurements: a user style set, installed over a book in progress."""
    doc = styled_docx(
        tmp / "book.docx",
        ("BookTitle", "A Synthetic Novel"),
        ("Heading1", "Chapter one"),
        ("ChapterOpen", PROSE),
        ("Body", PROSE),
        ("SceneBreak", "* * *"),
        ("AfterBreak", PROSE),
    )
    before = paragraphs(doc)
    install_house(doc)
    wanted = {s["id"]: s for s in json.loads(STYLESET.read_text("utf-8"))["styles"]}
    have = style_entries(doc)
    for sid, spec in wanted.items():
        got = have[sid]
        for key in ("name", "type", "based_on", "next", "run", "paragraph"):
            assert got.get(key) == spec.get(key), (sid, key)
    assert paragraphs(doc) == before  # the text and the style of every paragraph is untouched
    # installing again is a no-op on the definitions (the set is the source of truth)
    again = style_entries(doc)
    install_house(doc)
    assert style_entries(doc) == again
    assert validate_structure(doc.read_bytes()) == []


def case_house_template(tmp: Path) -> None:
    """The house template: the same style set travels as a .dotx and lands unchanged."""
    source = styled_docx(tmp / "source.docx", ("Body", PROSE))
    install_house(source)
    template = tmp / "house.dotx"
    styleset_export_dotx({"doc": str(source), "out": str(template)})
    target = styled_docx(tmp / "target.docx", ("", PROSE))
    styleset_import_dotx({"doc": str(target), "path": str(template)})
    assert style_entries(target) == style_entries(source)
    assert paragraphs(target) == [("", PROSE)]


def case_legacy_styles_follow_the_house_table(tmp: Path) -> None:
    """Style aliases (a document's own names for the house styles) merge into the house ids."""
    doc = styled_docx(
        tmp / "legacy.docx",
        ("Title1", "Chapter one"),
        ("Plain", PROSE),
        ("Plain", PROSE),
        ("Letter", "* * *"),
    )
    apply_style_ops(
        doc,
        [
            {"op": "style.create", "id": "Title1", "name": "Title 1"},
            {"op": "style.create", "id": "Plain", "name": "Plain"},
            {"op": "style.create", "id": "Letter", "name": "Letter"},
        ],
    )
    install_house(doc)
    apply_style_ops(
        doc,
        [
            {"op": "style.merge", "from": "Title1", "into": "Heading1"},
            {"op": "style.merge", "from": "Plain", "into": "Body"},
            {"op": "style.merge", "from": "Letter", "into": "SceneBreak"},
        ],
    )
    assert [s for s, _ in paragraphs(doc)] == ["Heading1", "Body", "Body", "SceneBreak"]
    assert {"Title1", "Plain", "Letter"}.isdisjoint(style_entries(doc))


def case_author_note_patterns(tmp: Path) -> None:
    """Capital-letter working notes inside the prose become comments, as docx-fixer flagged them."""
    notes = [
        "She waited XXXX for him.",
        "He left at dawn. CHECK!",
        "FROM HERE rewrite the whole scene",
        "NOT SURE about this ending",
        "REVISION 2031 shorter",
        "Why would she?? Nobody knows",
        "Stop!!! Now.",
        "FINAL (3 drafts) of this passage",
    ]
    ordinary = [
        "Please check the door, it is time to delete nothing and move on.",  # lower case
        "Maybe todo lists and revision notes are ordinary words in a story.",
        "She asked who? Then why? Then nothing.",  # single marks
        "He said NOTHING at all.",  # an upper-case word that is not a marker
        "Exactly XXX of them came.",  # too short a run of X
        "The final (2 of them) arrived.",  # lower case
        "FINAL answer, in capitals but no bracket.",
    ]
    doc = styled_docx(tmp / "notes.docx", *[("Body", t) for t in notes + ordinary])
    install_house(doc)
    proposal = run_recipe(doc, "house-author-notes")
    assert proposal["matches"] == len(notes)
    ops = proposal["batch"]["ops"]
    assert [o["op"] for o in ops] == ["comment.add"] * len(notes)
    commented = REGISTRY["comments.list"].run({"doc": str(doc)}).result["comments"]  # type: ignore[union-attr]
    assert len(commented) == len(notes)
    assert [t for _, t in paragraphs(doc)] == notes + ordinary  # comments only: prose untouched
    assert run_recipe(doc, "house-author-notes")["matches"] == len(
        notes
    )  # still flagged, by design


def case_whole_line_notes_are_removed(tmp: Path) -> None:
    """A paragraph that is only a note marker is deleted (tracked); prose lines are kept."""
    doc = styled_docx(
        tmp / "lines.docx",
        ("Body", PROSE),
        ("Body", "XXXXXX"),
        ("Body", "  TODO!  "),
        ("Body", "TODO: but this one has prose in it"),
        ("Body", PROSE),
    )
    proposal = run_recipe(doc, "house-note-lines")
    assert proposal["matches"] == 2
    with zipfile.ZipFile(doc) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    deleted = [
        "".join(t.text or "" for t in d.iter(f"{{{W}}}delText")) for d in root.iter(f"{{{W}}}del")
    ]
    assert [d.strip() for d in deleted if d.strip()] == ["XXXXXX", "TODO!"]
    texts = [t for _, t in paragraphs(doc)]
    assert PROSE in texts and "TODO: but this one has prose in it" in texts


def case_scene_separator(tmp: Path) -> None:
    """Asterisks typed as text take the house separator style; real separators are left."""
    doc = styled_docx(
        tmp / "scenes.docx",
        ("Body", PROSE),
        ("Body", "* * *"),
        ("Body", PROSE),
        ("Body", "***"),
        ("SceneBreak", "* * *"),
        ("Body", "A star * in a sentence"),
    )
    install_house(doc)
    proposal = run_recipe(doc, "house-scene-separator")
    assert proposal["matches"] == 3  # the one already styled still matches the text pattern
    assert [s for s, _ in paragraphs(doc)] == [
        "Body",
        "SceneBreak",
        "Body",
        "SceneBreak",
        "SceneBreak",
        "Body",
    ]


def case_note_style_layer(tmp: Path) -> None:
    """The note style layer: a style in the set, and a batch that removes every paragraph in it."""
    doc = styled_docx(
        tmp / "layer.docx",
        ("Body", PROSE),
        ("WorkNote", "an observation for the author"),
        ("Body", PROSE),
        ("WorkNote", "another one"),
        ("Body", "The end."),
    )
    install_house(doc)
    indexed = Document(str(doc)).index.paragraphs
    marked = [
        p.address
        for p, (style, _) in zip(indexed, paragraphs(doc), strict=True)
        if style == "WorkNote"
    ]
    assert len(marked) == 2
    batch({"doc": str(doc), "ops": [{"op": "para.delete", "at": str(a)} for a in marked]})
    assert [t for _, t in paragraphs(doc) if t] == [PROSE, PROSE, "The end."]


OWNER_RULES: dict[str, Callable[[Path], None]] = {
    # rule name -> case; each case's docstring names the declarative equivalent
    "house-style-table": case_house_style_table,
    "house-template": case_house_template,
    "style-aliases": case_legacy_styles_follow_the_house_table,
    "author-note-patterns": case_author_note_patterns,
    "whole-line-notes": case_whole_line_notes_are_removed,
    "scene-separator": case_scene_separator,
    "note-style-layer": case_note_style_layer,
}


def test_house_rules_expressible_as_user_recipes(tmp_path: Path) -> None:
    failures: list[str] = []
    for i, (rule, case) in enumerate(OWNER_RULES.items()):
        work = tmp_path / f"case-{i}"
        work.mkdir()
        try:
            case(work)
        except Exception as exc:  # report every rule that fails, not only the first
            failures.append(f"{rule}: {type(exc).__name__}: {exc}")
    print(f"owner_rules_not_expressible == {len(failures)} (of {len(OWNER_RULES)} rules)")
    assert failures == []


def test_user_recipes_are_valid_user_store_entries() -> None:
    cat = catalogue()
    assert cat.invalid == []
    for name in ("house-author-notes", "house-note-lines", "house-scene-separator"):
        assert cat.recipes[name][0] == "user"


def test_house_documents_pass_the_validator(tmp_path: Path) -> None:
    """Every document the cases write is a valid .docx (lint, plus LibreOffice when present)."""
    require_soffice()
    out: dict[str, bytes] = {}
    for i, case in enumerate(OWNER_RULES.values()):
        work = tmp_path / f"case-{i}"
        work.mkdir()
        case(work)
        for doc in sorted(work.glob("*.docx")):
            out[f"{i}-{doc.stem}"] = doc.read_bytes()
    assert out
    assert all(validate_structure(data) == [] for data in out.values())
    assert validate_documents(out, tmp_path) == {key: [] for key in out}
