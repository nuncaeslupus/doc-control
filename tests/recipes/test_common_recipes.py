"""The common bundled recipes (T25): each proposes a valid batch that gives the expected text."""
# ruff: noqa: RUF001

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import make_docx

NB = " "

# recipe -> (paragraphs before, paragraphs after the proposal is applied)
CASES: dict[str, tuple[list[str], list[str]]] = {
    "leading-spaces": (["  Indented", "Plain"], ["Indented", "Plain"]),
    "fake-tabs": (["Name      Value", "a  b"], ["Name\tValue", "a  b"]),
    "blank-paragraphs": (["Text", "   ", "More"], ["Text", "", "More"]),  # tracked: text is gone
    "tab-indent": (["\tIndented", "Plain"], ["Indented", "Plain"]),
    "fake-centring": (
        [" " * 10 + "THE END", "\t\tTitle", "  short"],
        ["THE END", "Title", "  short"],
    ),
    "quotes-en": (
        ['He said "hi" and "bye" and "'],
        ['He said “hi” and “bye” and "'],
    ),
    "quotes-en-single": (
        ["'Hello there' she said", "'It's mine,' he said"],
        ["‘Hello there’ she said", "‘It's mine,’ he said"],
    ),
    "quotes-de": (['Er sagte "Hallo" laut'], ["Er sagte „Hallo“ laut"]),
    "quotes-fr": (['Il dit " bonjour " fort'], [f"Il dit «{NB}bonjour{NB}» fort"]),
    "dashes-em": (
        ["wait--what and so --- yes", "well-known"],
        ["wait—what and so—yes", "well-known"],
    ),
    "dashes-spaced-en": (
        ["one - two", "- not here", "co-op"],
        ["one – two", "- not here", "co-op"],
    ),
    "scene-break": (
        ["Text", "***", "  # # #", "* * *", "- - -"],
        ["Text", "* * *", "* * *", "* * *", "* * *"],
    ),
    "chapter-heading-period": (
        ["Chapter 3.", "Chapter 3: Title.", "Chapter IV."],
        ["Chapter 3", "Chapter 3: Title.", "Chapter IV"],
    ),
}


@pytest.fixture(autouse=True)
def _ops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    load_operations()
    monkeypatch.setenv("DOC_CONTROL_HOME", str(tmp_path / "home"))


def propose(doc: Path, name: str) -> dict[str, Any]:
    reply = REGISTRY["recipe.run"].run({"doc": str(doc), "recipe": name})
    assert isinstance(reply, Reply)
    batch: dict[str, Any] = reply.result["batch"]
    return batch


def texts(doc: Path) -> list[str]:
    return [p.text for p in Document(str(doc)).index.paragraphs]


@pytest.mark.parametrize("name", sorted(CASES))
def test_recipe_gives_expected_text_and_is_idempotent(tmp_path: Path, name: str) -> None:
    before, after = CASES[name]
    doc = make_docx(tmp_path / "a.docx", *before)
    batch = propose(doc, name)
    assert batch["ops"], name
    assert isinstance(REGISTRY["batch"].run(batch), Reply)
    assert texts(doc) == after
    assert propose(doc, name)["ops"] == []  # a second run finds nothing left to fix


def test_proposing_and_previewing_write_nothing(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "a      b", "***")
    before = doc.read_bytes()
    for name in ("fake-tabs", "scene-break"):
        preview = REGISTRY["batch"].run({**propose(doc, name), "dry_run": True})
        assert isinstance(preview, Reply)
    assert doc.read_bytes() == before


def test_scene_break_centres_every_break(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "* * *", "###")
    ops = propose(doc, "scene-break")["ops"]
    assert [o["op"] for o in ops] == ["text.replace", "format.apply", "format.apply"]
    assert [o["align"] for o in ops if o["op"] == "format.apply"] == ["center", "center"]


def test_tab_indent_and_fake_centring_set_paragraph_format(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "\tIndented", " " * 9 + "Centred")
    indent = propose(doc, "tab-indent")["ops"]
    assert [o["op"] for o in indent] == ["text.replace", "format.apply"]
    assert indent[1]["indent_first_line_cm"] == 0.75
    centre = propose(doc, "fake-centring")["ops"]
    assert centre[1]["align"] == "center" and centre[1]["at"].startswith("p2#")


def test_blank_paragraphs_deletes_only_whitespace_paragraphs(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Keep", f"{NB} \t", "Keep too")
    ops = propose(doc, "blank-paragraphs")["ops"]
    assert [(o["op"], o["at"].split("#")[0]) for o in ops] == [("para.delete", "p2")]


def test_chapter_heading_style_matches_headings_only(tmp_path: Path) -> None:
    doc = make_docx(
        tmp_path / "a.docx",
        "Chapter 3",
        "Chapter IV: The Return",
        "Chapter and verse",
        "She read Chapter 3 twice",
    )
    ops = propose(doc, "chapter-heading-style")["ops"]
    assert [(o["op"], o["style"]) for o in ops] == [("style.apply", "Heading 1")] * 2
    assert [o["at"].split("#")[0] for o in ops] == ["p1", "p2"]


def test_leftover_markers_comment_without_changing_text(tmp_path: Path) -> None:
    doc = make_docx(
        tmp_path / "a.docx", "Fix this [TODO check date] now", "Roman XXX stays", "FIXME later"
    )
    ops = propose(doc, "leftover-markers")["ops"]
    assert [o["op"] for o in ops] == ["comment.add", "comment.add"]
    assert ops[0]["text"] == "Leftover marker: [TODO check date]"
    assert isinstance(REGISTRY["batch"].run({"doc": str(doc), "ops": ops}), Reply)
    assert texts(doc)[0] == "Fix this [TODO check date] now"


def test_quote_recipes_work_on_the_same_paragraph_in_turn(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "'It's mine,' he said, \"no.\"")
    for name in ("quotes-en-single", "quotes-en"):
        batch = propose(doc, name)
        assert batch["ops"], name
        assert isinstance(REGISTRY["batch"].run(batch), Reply)
    assert texts(doc) == ["\u2018It's mine,\u2019 he said, \u201cno.\u201d"]


def unchanged(tmp_path: Path, name: str, *paragraphs: str) -> None:
    doc = make_docx(tmp_path / "a.docx", *paragraphs)
    assert propose(doc, name)["ops"] == [], paragraphs


def test_dashes_em_leaves_options_urls_ranges_sums_and_long_runs(tmp_path: Path) -> None:
    unchanged(
        tmp_path,
        "dashes-em",
        "run --verbose now",
        "see http://a.com/a--b today",
        "in 2020--2021 it",
        "5 -- 3 is two",
        "a ---- b",
        "a----b",
    )


def test_dashes_spaced_en_leaves_sums_negatives_and_list_dashes(tmp_path: Path) -> None:
    unchanged(tmp_path, "dashes-spaced-en", "5 - 3 = 2", "-5 - -3", "- item - x", "x2 - y")


@pytest.mark.parametrize("name", ["quotes-en", "quotes-de", "quotes-fr"])
def test_quote_recipes_leave_inch_and_foot_marks(tmp_path: Path, name: str) -> None:
    unchanged(tmp_path, name, 'the 5" pipe and the 6" pipe', "He is 5'10\" tall")


def test_quotes_after_inch_marks_still_pair_up(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", 'the 5" pipe said "hi"')
    assert isinstance(REGISTRY["batch"].run(propose(doc, "quotes-en")), Reply)
    assert texts(doc) == ['the 5" pipe said \u201chi\u201d']


def test_single_quotes_leave_rock_n_roll(tmp_path: Path) -> None:
    unchanged(tmp_path, "quotes-en-single", "rock 'n' roll")


def test_scene_break_does_not_propose_centring_for_a_centred_paragraph(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "* * *")
    first = propose(doc, "scene-break")["ops"]
    assert [o["op"] for o in first] == ["format.apply"]  # text is right, centring is not
    assert isinstance(REGISTRY["batch"].run(propose(doc, "scene-break")), Reply)
    reply = REGISTRY["recipe.run"].run({"doc": str(doc), "recipe": "scene-break"})
    assert isinstance(reply, Reply) and reply.result["batch"]["ops"] == []
    assert reply.result["skipped"] == [] and reply.warnings == []


def test_undeletable_blank_paragraph_is_skipped_not_fatal(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Keep", "  ", "Keep too", "   ")  # last one: no mark
    reply = REGISTRY["recipe.run"].run({"doc": str(doc), "recipe": "blank-paragraphs"})
    assert isinstance(reply, Reply)
    ops = reply.result["batch"]["ops"]
    assert [(o["op"], o["at"].split("#")[0]) for o in ops] == [("para.delete", "p2")]
    skipped = reply.result["skipped"]
    assert [s["at"].split("#")[0] for s in skipped] == ["p4"]
    assert "last paragraph" in skipped[0]["reason"]
    assert any(w.startswith("SKIPPED") for w in reply.warnings)
    assert isinstance(REGISTRY["batch"].run(reply.result["batch"]), Reply)
    assert texts(doc) == ["Keep", "", "Keep too", "   "]
