"""Loose folding with an offset map (FIND T2).

Gate: ``span_mismatches == 0``. A folded hit mapped back with ``to_original`` is a span of
whole clusters of the original text that folds to the phrase.
"""

from __future__ import annotations

import random
import re
import unicodedata
import zipfile
from io import BytesIO

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import build_index
from doc_control.model.loosefold import LEVELS, Level, fold, fold_phrase, to_original
from doc_control.model.normalized import normalized_text
from tests.corpus.generator import generate
from tests.model.test_anchor_normalised import noisy

FOLDED: tuple[Level, ...] = ("case", "default", "loose")
ALPHABET = [
    *"abcdeiounstr ",
    *["\u00e1", "\u00e9", "\u00ed", "\u00f1", "\u00fc", "\u00c1", "\u00c9", "\u00df", "\u00e7"],
    "\u0130",
    *["e\u0301", "a\u0308", "n\u0303"],  # letters with combining marks
    *"\u00bf?\u00a1!-\u2014\u2013\u00ab\u00bb\"'.,;: ",
    *"$+=^<>|",
    *["\u00ad", "\u200b", "\ufeff", "\u200e", "\t", "\u00a0", "  ", "\n"],
]


def random_text(rng: random.Random) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(5, 60)))


def occurrences(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    pos = haystack.find(needle)
    while pos >= 0:
        out.append(pos)
        pos = haystack.find(needle, pos + 1)
    return out


def test_fold_spans_cut_the_matched_passage() -> None:
    rng = random.Random(7)
    mismatches = 0
    checked = 0
    for _ in range(400):
        text = random_text(rng)
        for level in FOLDED:
            folded = fold(text, level)
            if not folded.text:
                continue
            a = rng.randrange(len(folded.text))
            b = rng.randint(a + 1, min(len(folded.text), a + 12))
            phrase = folded.text[a:b].strip(" ")
            if not phrase:
                continue
            for pos in occurrences(folded.text, phrase):
                checked += 1
                i, j = pos, pos + len(phrase)
                lo, hi = to_original(folded, i, j)
                piece = fold(text[lo:hi], level).text
                aligned = (i == 0 or folded.starts[i] != folded.starts[i - 1]) and (
                    j == len(folded.text) or folded.ends[j - 1] != folded.ends[j]
                )
                whole = (
                    0 <= lo < hi <= len(text)
                    and not unicodedata.combining(text[lo])
                    and (hi == len(text) or not unicodedata.combining(text[hi]))
                )
                # A hit inside what one character folds to (``ß`` -> ``ss``) maps to the
                # whole character, which holds the phrase; any other hit folds to it.
                ok = whole and (piece == phrase if aligned else phrase in piece)
                if not ok:
                    mismatches += 1
                    print(level, repr(text), repr(phrase), (lo, hi), repr(piece))
    print(f"span_mismatches == {mismatches} (of {checked} hits)")
    assert checked > 200
    assert mismatches == 0


@pytest.mark.parametrize("level", FOLDED)
def test_fold_keeps_paragraph_mark_untrimmed(level: Level) -> None:
    # ``^pEstas`` and ``ahí^p`` once the phrase's Word codes are translated.
    assert fold("\rEstas", level).text == "\restas"
    assert fold("ahí\r", level).text == ("ahí\r" if level == "case" else "ahi\r")
    assert fold("ahí \r Estas", level).text == ("ahí\restas" if level == "case" else "ahi\restas")
    assert fold("  ahí \r", level).text.endswith("\r")
    assert fold_phrase("\rEstas", level).text.startswith("\r")


def test_fold_exact_is_the_identity() -> None:
    folded = fold("¿Estás  ahí?", "exact")
    assert folded.text == "¿Estás  ahí?"
    assert to_original(folded, 2, 5) == (2, 5)


@pytest.mark.parametrize(
    ("text", "case", "default", "loose"),
    [
        ("¿Estás ahí?", "¿estás ahí?", "estas ahi", "estas ahi"),
        ("-¿Estás ahí?", "-¿estás ahí?", "estas ahi", "estas ahi"),
        ("—Estas ahí?", "—estas ahí?", "estas ahi", "estas ahi"),
        ("ahí—dijo", "ahí—dijo", "ahi dijo", "ahi dijo"),
        ("5$ y  más", "5$ y más", "5$ y mas", "5 y mas"),
        ("Straße", "strasse", "strasse", "strasse"),
        ("é", "é", "e", "e"),
        ("co­sa​", "cosa", "cosa", "cosa"),
    ],
)
def test_fold_levels(text: str, case: str, default: str, loose: str) -> None:
    assert fold(text, "case").text == case
    assert fold(text, "default").text == default
    assert fold(text, "loose").text == loose


def test_widening_takes_touching_punctuation_on_the_phrase_side_only() -> None:
    folded = fold("Dijo -¿Estás ahí?! Sí", "default")
    assert folded.text == "dijo estas ahi si"
    i = folded.text.index("estas")
    j = i + len("estas ahi")
    start, end = to_original(folded, i, j)
    assert folded.source[start:end] == "Estás ahí"
    start, end = to_original(folded, i, j, widen_left=True)
    assert folded.source[start:end] == "-¿Estás ahí"
    start, end = to_original(folded, i, j, widen_right=True)
    assert folded.source[start:end] == "Estás ahí?!"
    start, end = to_original(folded, i, j, widen_left=True, widen_right=True)
    assert folded.source[start:end] == "-¿Estás ahí?!"
    # Whitespace stops the widening.
    folded = fold("x - Estás", "default")
    start, end = to_original(folded, 2, 7, widen_left=True)
    assert folded.source[start:end] == "Estás"


def test_a_cluster_is_never_cut() -> None:
    folded = fold("café ok", "default")
    assert folded.text == "cafe ok"
    assert to_original(folded, 0, 4) == (0, 5)  # the accent stays with its letter


@pytest.mark.parametrize(
    ("phrase", "level"),
    [("--", "default"), ("¿?", "loose"), ("$+", "loose"), ("  ", "case"), ("", "default")],
)
def test_an_empty_fold_is_refused(phrase: str, level: Level) -> None:
    with pytest.raises(DocControlError) as err:
        fold_phrase(phrase, level)
    assert err.value.code == ErrorCode.SCHEMA_INVALID


def test_a_phrase_of_punctuation_survives_at_case() -> None:
    assert fold_phrase("--", "case").text == "--"


def test_fold_rejects_an_unknown_level() -> None:
    assert LEVELS == ("exact", "case", "default", "loose")
    with pytest.raises(ValueError, match="unknown fold level"):
        fold("x", "fuzzy")  # type: ignore[arg-type]


def test_fold_drops_cf_like_normalised_tier() -> None:
    """Every quote of ``test_anchor_normalised`` is found at ``case`` in its noisy text."""
    rng = random.Random(41)
    data = zipfile.ZipFile(BytesIO(generate("novel", 3))).read("word/document.xml")
    clean = [p.text for p in build_index(data).paragraphs if len(p.text) > 200][:60]
    texts = [noisy(t, rng) for t in clean]
    cases = [
        ("la ca­sa  blanca", "casa blanca"),
        ("la casa blanca", "casa  blanca"),
        ("la\tcasa​ blanca", "la casa blanca"),
    ]
    for text in texts:
        flat = normalized_text(text)
        words = [m.start() for m in re.finditer(r"(?<= )\S", flat)]
        start = rng.choice(words[2:-12])
        end = next(m.end() for m in re.finditer(r"\S+", flat) if m.start() > start + 40)
        cases.append((text, flat[start:end]))
    missed = 0
    for text, quote in cases:
        hay, needle = fold(text, "case"), fold_phrase(quote, "case")
        pos = hay.text.find(needle.text)
        if pos < 0:
            missed += 1
            continue
        lo, hi = to_original(hay, pos, pos + len(needle.text))
        if normalized_text(text[lo:hi]) != normalized_text(quote):
            missed += 1
    print(f"case_quotes_missed == {missed} (of {len(cases)})")
    assert missed == 0
