"""Match levels and the retry ladder (FIND T4).

Gate: ``loose_match_failures == 0``. Every row of the spec's variant table is found, by the
single-target ladder, at its expected level and with the expected original span.
"""

from __future__ import annotations

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.loosefold import LEVELS, Hit, Level, hits, ladder
from doc_control.model.wordcodes import translate_codes

# (phrase, paragraph, expected level, expected matched text)
VARIANTS: list[tuple[str, str, Level, str]] = [
    # The owner's rule: "Estas ahi" finds the question however it is punctuated.
    ("Estas ahi", "\u00bfEst\u00e1s ah\u00ed?", "default", "Est\u00e1s ah\u00ed"),
    ("Estas ahi", "-\u00bfEst\u00e1s ah\u00ed?", "default", "Est\u00e1s ah\u00ed"),
    ("Estas ahi", "-Estas ah\u00ed?", "default", "Estas ah\u00ed"),
    ("Estas ahi", "--Estas ahi", "exact", "Estas ahi"),
    ("\u2014Estas ahi", "--Estas ahi", "default", "--Estas ahi"),
    ("-Estas ahi", "-\u00bfEst\u00e1s ah\u00ed? -dijo.", "default", "-\u00bfEst\u00e1s ah\u00ed"),
    (
        "\u00bfEstas ahi?",
        "-\u00bfEst\u00e1s ah\u00ed? -dijo.",
        "default",
        "-\u00bfEst\u00e1s ah\u00ed?",
    ),
    ("Est\u00e1s ah\u00ed", "-\u00bfEst\u00e1s ah\u00ed?", "exact", "Est\u00e1s ah\u00ed"),
    ("ESTAS AHI", "--Estas ahi", "case", "Estas ahi"),
    ("Estas ahi", "\u00abEst\u00e1s ah\u00ed\u00bb, le dijo.", "default", "Est\u00e1s ah\u00ed"),
    # Mid-paragraph: a phrase without the marks finds a passage that has them.
    (
        "dijo estas ahi y se fue",
        "Ella dijo: \u00bfest\u00e1s ah\u00ed?, y se fue.",
        "default",
        "dijo: \u00bfest\u00e1s ah\u00ed?, y se fue",
    ),
    (
        "que estas ahi",
        "Pregunt\u00f3 que \u00bfestas ahi? otra vez",
        "default",
        "que \u00bfestas ahi",
    ),
    # Dashes.
    ("ahi dijo", "ah\u00ed\u2014dijo", "default", "ah\u00ed\u2014dijo"),
    ("ahi - dijo", "ah\u00ed \u2013 dijo", "default", "ah\u00ed \u2013 dijo"),
    ("bien-estar", "bien\u2011estar", "default", "bien\u2011estar"),
    ("buenas noches", "\u2014Buenas noches\u2014", "case", "Buenas noches"),
    # Quotes.
    ("Hola, dijo", "\u00abHola\u00bb, dijo \u00e9l.", "default", "Hola\u00bb, dijo"),
    ('dijo "basta"', "dijo \u201cbasta\u201d", "default", "dijo \u201cbasta\u201d"),
    ("it's late", "it\u2019s late", "default", "it\u2019s late"),
    # Whitespace and invisible characters.
    ("Estas  ahi", "Estas ahi", "case", "Estas ahi"),
    ("Estas ahi", "Estas\u00a0ahi", "case", "Estas\u00a0ahi"),
    ("Estas ahi", "Estas\tahi", "case", "Estas\tahi"),
    ("Estas ahi", "Es\u00adtas ahi", "case", "Es\u00adtas ahi"),
    ("Estas^sahi", "Estas\u00a0ahi", "exact", "Estas\u00a0ahi"),
    # Symbols are dropped only at loose, reached by the retry.
    ("5 dolares", "Cuesta 5$ d\u00f3lares.", "loose", "5$ d\u00f3lares"),
]


def single_target(texts: list[str], phrase: str) -> list[Hit]:
    """The hits of the first level of the ``default`` ladder that has any."""
    for level in ladder("default", retry=True):
        found = hits(texts, phrase, level)
        if found:
            return found
    return []


@pytest.mark.parametrize(("phrase", "paragraph", "level", "matched"), VARIANTS)
def test_variant_table_matches(phrase: str, paragraph: str, level: Level, matched: str) -> None:
    found = single_target([paragraph], translate_codes(phrase))
    assert len(found) == 1, found
    hit = found[0]
    assert (hit.index, hit.level) == (0, level)
    assert paragraph[hit.start : hit.end] == matched


def test_symbols_kept_at_default() -> None:
    assert hits(["5"], "5$", "default") == []
    assert hits(["5"], "5$", "loose") == [Hit(0, 0, 1, "loose")]
    assert hits(["Total: 5$"], "5$", "default") == [Hit(0, 7, 9, "exact")]


def test_exact_paragraph_preferred() -> None:
    texts = ["-\u00bfEst\u00e1s ah\u00ed?", "Estas ahi, dijo.", "\u00bfESTAS AHI?"]
    assert single_target(texts, "Estas ahi") == [Hit(1, 0, 9, "exact")]


def test_same_level_twice_both_returned() -> None:
    texts = ["\u00bfEst\u00e1s ah\u00ed?", "-Est\u00e1s ah\u00ed."]
    found = single_target(texts, "Estas ahi")
    assert [(h.index, h.level) for h in found] == [(0, "default"), (1, "default")]


def test_hits_tag_strictest_level() -> None:
    texts = ["Estas ahi. ESTAS AHI. Est\u00e1s ah\u00ed.", "nada"]
    found = hits(texts, "Estas ahi", "default")
    assert [h.level for h in found] == ["exact", "case", "default"]
    assert [texts[0][h.start : h.end] for h in found] == [
        "Estas ahi",
        "ESTAS AHI",
        "Est\u00e1s ah\u00ed",
    ]


def test_stricter_hit_keeps_its_own_span() -> None:
    # At default the phrase's punctuation would also take the leading dash.
    assert hits(["-\u00bfEst\u00e1s ah\u00ed?"], "\u00bfEst\u00e1s ah\u00ed?", "default") == [
        Hit(0, 1, 12, "exact")
    ]


def test_hits_at_exact_do_not_fold() -> None:
    assert hits(["\u00bfEst\u00e1s ah\u00ed?"], "Estas ahi", "exact") == []
    assert hits(["aaaa"], "aa", "exact") == [Hit(0, 0, 2, "exact"), Hit(0, 2, 4, "exact")]


def test_paragraph_mark_crosses_paragraphs() -> None:
    joined = "Fin.\r-\u00bfEst\u00e1s ah\u00ed?"
    found = hits([joined], translate_codes("fin^pestas ahi"), "default")
    assert [(joined[h.start : h.end], h.level) for h in found] == [
        ("Fin.\r-\u00bfEst\u00e1s ah\u00ed", "default")
    ]


def test_empty_fold_refused() -> None:
    with pytest.raises(DocControlError) as caught:
        hits(["--Estas"], "--", "default")
    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert hits(["--Estas"], "--", "exact") == [Hit(0, 0, 2, "exact")]


def test_ladder() -> None:
    assert ladder("exact", retry=True) == ("exact",)
    assert ladder("exact", retry=False) == ("exact",)
    assert ladder("default", retry=True) == LEVELS
    assert ladder("loose", retry=True) == LEVELS
    assert ladder("default", retry=False) == ("default",)
    assert ladder("loose", retry=False) == ("loose",)
    with pytest.raises(ValueError):
        ladder("fuzzy", retry=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        hits(["x"], "x", "fuzzy")  # type: ignore[arg-type]
