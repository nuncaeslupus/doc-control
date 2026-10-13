"""Context-aware recipes (T38): empty paragraphs, own style and neighbour conditions.

The gate ``context_rules_not_expressible == 0``. docx-fixer's structural rules look past one
paragraph's text: a pause (exactly two empty paragraphs) gives the next paragraph its own
style, longer runs of empty paragraphs are collapsed or flagged as a possible chapter break,
the first paragraph after a chapter heading takes the chapter-opening style, an epigraph's
author line typed in the quote style before a blank line takes the author style, and empty
headings go. Each is written here as a declarative recipe (bundled when it is general, a
test definition over the house style set when it names house styles) and passes its case
on a synthetic document, applied through ``batch`` as tracked changes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import Address
from doc_control.ops.batch import batch
from doc_control.ops.styleset import styleset_install
from doc_control.recipes.engine import Context, propose
from doc_control.recipes.schema import validate_recipe
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.structural import validate_structure
from tests.helpers import CONTENT_TYPES, RELS

STYLESET = Path(__file__).parent / "house" / "stylesets" / "house-novel.json"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PROSE = "The rain fell on the village all night and nobody went out."

HOUSE_PAUSE: dict[str, Any] = {
    "recipe": "house-pause",
    "version": 1,
    "match": {"where": {"prev": [{"empty": True}, {"empty": True}, {"empty": False}]}},
    "propose": [{"op": "style.apply", "at": "$para", "style": "AfterPause"}],
}
HOUSE_CHAPTER_OPENING: dict[str, Any] = {
    "recipe": "house-chapter-opening",
    "version": 1,
    "match": {
        "where": {
            "heading": False,
            "not_style": ["Chapter open"],
            "skip_empty": True,
            "prev": [{"heading": True}],
        }
    },
    "propose": [{"op": "style.apply", "at": "$para", "style": "ChapterOpen"}],
}
HOUSE_QUOTE_ATTRIBUTION: dict[str, Any] = {
    "recipe": "house-quote-attribution",
    "version": 1,
    "match": {
        "find": {"regex": "^[\u2014\u2013-] ?\\S"},
        "where": {
            "style": ["Epigraph"],
            "prev": [{"style": ["Epigraph"], "text": "\\S"}],
            "next": [{"empty": True}],
        },
    },
    "propose": [{"op": "style.apply", "at": "$para", "style": "EpigraphAuthor"}],
}


@pytest.fixture(autouse=True)
def _ops() -> None:
    load_operations()


def styled_docx(path: Path, *paragraphs: tuple[str, str]) -> Path:
    """A .docx of ``(style id or "", text)`` paragraphs (text "" is an empty paragraph),
    with the house style set installed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ""
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        run = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
        body += f"<w:p>{ppr}{run}</w:p>"
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES, zipfile.ZIP_DEFLATED)
        zf.writestr("_rels/.rels", RELS, zipfile.ZIP_DEFLATED)
        zf.writestr("word/document.xml", document, zipfile.ZIP_DEFLATED)
    styleset_install({"doc": str(path), "path": str(STYLESET)})
    return path


def accepted(path: Path) -> list[tuple[str, str]]:
    """``(style id, text)`` per paragraph once every tracked change is accepted."""
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    out: list[tuple[str, str]] = []
    for p in root.iter(f"{{{W}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{W}}}t"))
        if p.find(f"{{{W}}}pPr/{{{W}}}rPr/{{{W}}}del") is not None and not text:
            continue  # a deleted empty paragraph
        style = p.find(f"{{{W}}}pPr/{{{W}}}pStyle")
        out.append((style.get(f"{{{W}}}val", "") if style is not None else "", text))
    return out


def commented(path: Path) -> list[str]:
    """The text of each paragraph that holds a comment anchor."""
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    return [
        "".join(t.text or "" for t in p.iter(f"{{{W}}}t"))
        for p in root.iter(f"{{{W}}}p")
        if p.find(f".//{{{W}}}commentRangeStart") is not None
    ]


def run(doc: Path, recipe: str | dict[str, Any]) -> dict[str, Any]:
    """Run a bundled recipe (by name) or a definition and apply its proposal with ``batch``."""
    key = "recipe" if isinstance(recipe, str) else "definition"
    reply = REGISTRY["recipe.run"].run({"doc": str(doc), key: recipe})
    assert isinstance(reply, Reply)
    result: dict[str, Any] = reply.result
    assert result["skipped"] == [], result["skipped"]
    if result["batch"]["ops"]:
        batch(result["batch"])
    assert validate_structure(doc.read_bytes()) == []
    return result


# -- one case per structural rule ------------------------------------------------------


def case_pause(tmp: Path) -> None:
    """Exactly two empty paragraphs are a pause: the next paragraph takes the pause style."""
    doc = styled_docx(
        tmp / "pause.docx",
        ("Body", "One."),
        ("", ""),
        ("", ""),
        ("Body", "Two."),
        ("", ""),
        ("Body", "Three."),
        ("", ""),
        ("", ""),
        ("", ""),
        ("Body", "Four."),
    )
    run(doc, HOUSE_PAUSE)
    styles = dict((text, style) for style, text in accepted(doc) if text)
    assert styles == {"One.": "Body", "Two.": "AfterPause", "Three.": "Body", "Four.": "Body"}


def case_empty_runs(tmp: Path) -> None:
    """Other runs of empty paragraphs collapse to one."""
    doc = styled_docx(
        tmp / "runs.docx",
        ("", "One."),
        ("", ""),
        ("", ""),
        ("", ""),
        ("", "Two."),
        ("", ""),
        ("", "Three."),
    )
    run(doc, "empty-paragraph-runs")
    assert [t for _, t in accepted(doc)] == ["One.", "", "Two.", "", "Three."]


def case_possible_chapter_break(tmp: Path) -> None:
    """Three or more empty paragraphs before a paragraph: flagged, not changed."""
    doc = styled_docx(
        tmp / "break.docx",
        ("", "One."),
        ("", ""),
        ("", ""),
        ("", ""),
        ("", "Two."),
        ("", ""),
        ("", ""),
        ("", "Three."),
    )
    before = accepted(doc)
    run(doc, "possible-chapter-break")
    assert commented(doc) == ["Two."]
    assert accepted(doc) == before


def case_chapter_opening(tmp: Path) -> None:
    """The first paragraph after a chapter heading (blank lines between) opens the chapter."""
    doc = styled_docx(
        tmp / "opening.docx",
        ("Heading1", "Chapter one"),
        ("", ""),
        ("Body", "First."),
        ("Body", "Second."),
        ("Heading1", "Chapter two"),
        ("ChapterOpen", "Already open."),
        ("Heading1", "Chapter three"),
        ("Body", "Third."),
    )
    run(doc, HOUSE_CHAPTER_OPENING)
    styles = dict((text, style) for style, text in accepted(doc) if text)
    assert styles["First."] == "ChapterOpen"
    assert styles["Second."] == "Body"
    assert styles["Already open."] == "ChapterOpen"
    assert styles["Third."] == "ChapterOpen"
    assert styles["Chapter two"] == "Heading1"


def case_quote_attribution(tmp: Path) -> None:
    """An epigraph's author line typed in the quote style before a blank line."""
    doc = styled_docx(
        tmp / "epigraph.docx",
        ("Epigraph", "All happy families are alike."),
        ("Epigraph", "—Leo Tolstoy"),
        ("", ""),
        ("Body", PROSE),
        ("Epigraph", "—not an attribution, a quoted line of dialogue"),
        ("Epigraph", "—and a second one"),
        ("Body", PROSE),
    )
    run(doc, HOUSE_QUOTE_ATTRIBUTION)
    styles = [style for style, text in accepted(doc) if text.startswith("—")]
    assert styles == ["EpigraphAuthor", "Epigraph", "Epigraph"]


def case_empty_headings(tmp: Path) -> None:
    """Empty paragraphs with a heading style go; empty body paragraphs stay."""
    doc = styled_docx(
        tmp / "headings.docx",
        ("Heading1", "Chapter one"),
        ("Heading1", ""),
        ("Body", PROSE),
        ("Body", ""),
        ("Body", PROSE),
    )
    run(doc, "empty-headings")
    assert accepted(doc) == [
        ("Heading1", "Chapter one"),
        ("Body", PROSE),
        ("Body", ""),
        ("Body", PROSE),
    ]


RULES: dict[str, Callable[[Path], None]] = {
    "pause": case_pause,
    "empty-paragraph-runs": case_empty_runs,
    "possible-chapter-break": case_possible_chapter_break,
    "chapter-opening": case_chapter_opening,
    "quote-attribution": case_quote_attribution,
    "empty-headings": case_empty_headings,
}


def test_structural_rules_expressible_with_context(tmp_path: Path) -> None:
    failures: list[str] = []
    for name, case in RULES.items():
        try:
            case(tmp_path / name)
        except (AssertionError, DocControlError) as err:
            failures.append(f"{name}: {err!r}")
    print(f"context_rules_not_expressible == {len(failures)} (of {len(RULES)} rules)")
    assert failures == []


@pytest.mark.parametrize("name", sorted(RULES))
def test_each_structural_rule(name: str, tmp_path: Path) -> None:
    RULES[name](tmp_path)


# -- engine and schema -----------------------------------------------------------------


def a(n: int) -> Address:
    return Address(n, "00000000")


def test_without_find_the_whole_paragraph_is_the_match() -> None:
    recipe = validate_recipe(
        {
            "recipe": "r",
            "version": 1,
            "match": {"where": {"next": [{"missing": True}]}},
            "propose": [{"op": "comment.add", "at": "$match", "text": "last: $match"}],
        }
    )
    proposal = propose(recipe, [(a(1), "First"), (a(2), "Last")])
    assert proposal.ops == [{"op": "comment.add", "at": "p2#00000000:0-4", "text": "last: Last"}]


def test_empty_paragraphs_are_scanned_only_on_request() -> None:
    paragraphs = [(a(1), "x"), (a(2), ""), (a(3), "")]
    plain = validate_recipe(
        {
            "recipe": "r",
            "version": 1,
            "match": {"where": {"style": ["Body"]}},
            "propose": [{"op": "para.delete", "at": "$para"}],
        }
    )
    ctx = {n: Context(frozenset({"body"})) for n in (1, 2, 3)}
    assert [o["at"] for o in propose(plain, paragraphs, context=ctx).ops] == ["p1#00000000"]
    empty = {**plain, "match": {"where": {"empty": True}}}
    assert [o["at"] for o in propose(empty, paragraphs).ops] == ["p2#00000000", "p3#00000000"]


def test_neighbour_text_and_skip_empty() -> None:
    recipe = validate_recipe(
        {
            "recipe": "r",
            "version": 1,
            "match": {
                "find": {"literal": "b"},
                "where": {"skip_empty": True, "prev": [{"text": "^A"}], "next": [{"text": "C$"}]},
            },
            "propose": [{"op": "text.replace", "at": "$match", "text": "B"}],
        }
    )
    paragraphs = [(a(1), "A1"), (a(2), ""), (a(3), "ab"), (a(4), "C"), (a(5), "ab"), (a(6), "C")]
    ops = propose(recipe, paragraphs).ops
    assert ops == [{"op": "text.replace", "at": "p3#00000000:1-2", "text": "B"}]


def test_style_conditions_use_id_or_name_case_insensitively() -> None:
    recipe = validate_recipe(
        {
            "recipe": "r",
            "version": 1,
            "match": {"where": {"style": ["chapter OPEN"], "prev": [{"not_style": ["Body"]}]}},
            "propose": [{"op": "style.apply", "at": "$para", "style": "Body"}],
        }
    )
    ctx = {
        1: Context(frozenset({"body"})),
        2: Context(frozenset({"chapteropen", "chapter open"})),
        3: Context(frozenset({"heading1", "heading 1"}), heading=True),
        4: Context(frozenset({"chapteropen", "chapter open"})),
    }
    paragraphs = [(a(n), "t") for n in (1, 2, 3, 4)]
    assert [o["at"] for o in propose(recipe, paragraphs, context=ctx).ops] == ["p4#00000000"]


def test_format_on_empty_paragraph_is_rejected() -> None:
    raw = {
        "recipe": "r",
        "version": 1,
        "match": {"where": {"empty": True}},
        "propose": [{"op": "format.apply", "at": "$para", "bold": True}],
    }
    with pytest.raises(DocControlError, match="no text to format"):
        validate_recipe(raw)


def test_rerun_ignores_empty_paragraphs_already_deleted(tmp_path: Path) -> None:
    doc = styled_docx(tmp_path / "d.docx", ("", PROSE), ("", ""), ("", ""), ("", ""), ("", PROSE))
    assert len(run(doc, "empty-paragraph-runs")["batch"]["ops"]) == 2
    assert run(doc, "empty-paragraph-runs")["batch"]["ops"] == []
    assert run(doc, "possible-chapter-break")["batch"]["ops"] == []


@pytest.mark.parametrize(
    ("match", "propose_", "extra", "message"),
    [
        ({}, None, {}, "give `find`, `where` or both"),
        ({"where": {"empty": True}, "find": {"literal": "x"}}, None, {}, "no text to `find`"),
        ({"where": {"empty": True}}, "$match", {}, "use $para"),
        ({"where": {"prev": [{}]}}, None, {}, "at least one condition"),
        ({"where": {"prev": [{"missing": True, "empty": True}]}}, None, {}, "takes no other"),
        ({"where": {"next": [{"empty": True}] * 4}}, None, {}, "at most 3"),
        ({"where": {"style": []}}, None, {}, "give 1 to 16"),
        ({"where": {"prev": [{"text": "("}]}}, None, {}, "$.match.where.prev[0].text"),
        ({"where": {"prev": [{"empty": True, "text": "x"}]}}, None, {}, "has no `text`"),
        ({"where": {"colour": "red"}}, None, {}, "colour"),
        ({"where": {"empty": False}}, None, {"repeat_until_stable": True}, "needs `find`"),
        ({"where": {}}, None, {}, "at least one condition"),
        ({"where": {"skip_empty": True}}, None, {}, "at least one condition"),
        ({"where": {"skip_empty": True, "prev": [{"empty": True}]}}, None, {}, "never sees"),
    ],
)
def test_invalid_context_is_rejected(
    match: dict[str, Any], propose_: str | None, extra: dict[str, Any], message: str
) -> None:
    raw = {
        "recipe": "r",
        "version": 1,
        "match": match,
        "propose": [{"op": "comment.add", "at": propose_ or "$para", "text": "x"}],
        **extra,
    }
    with pytest.raises(DocControlError) as err:
        validate_recipe(raw)
    assert err.value.code is ErrorCode.RECIPE_INVALID
    assert message in err.value.message + str(err.value.details)
