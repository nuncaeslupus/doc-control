"""The joined-text search of ``live.select`` (FIND T5 review): fast, exact, never cached whole.

A whole document folded through the ``fold`` cache stayed there for every version of the
document (about 140 MB each on the novel). These tests pin that only paragraph-sized texts
are cached, that the fast fold equals the offset-mapped one, and that the joined search
finds the same spans as a search of the whole folded document.
"""

from __future__ import annotations

import random
import unicodedata

import pytest

from doc_control.errors import DocControlError
from doc_control.model import loosefold
from doc_control.model.loosefold import CACHED_TEXT_MAX, Level, fold, fold_text, joined_hits

FOLDED: tuple[Level, ...] = ("case", "default", "loose")
ALPHABET = (
    "aAe\xe9\u0301\xdf\u0130 \t\xa0\r.,-\u2014\xbf?$*\xad\u200b\u1100\u1161"
    "\uac00\u11a8\ufb01\u03a3\u03c2\u01c5\uffffxyz\xb5\xa8\x80\x85\xff"
)


def _whole(texts: list[str], phrase: str, level: Level) -> list[tuple[int, int]]:
    """The spans of ``phrase`` at ``level`` in the joined text, folded whole."""
    joined = "\r".join(texts) + "\r"
    needle = fold(phrase, level).text
    left, right = loosefold._widens(phrase) if level != "case" else (False, False)
    spans: list[tuple[int, int]] = []
    for start, end in loosefold._folded_spans(loosefold._fold(joined, level), needle, left, right):
        if not spans or start >= spans[-1][1]:
            spans.append((start, end))
    return spans


def test_whole_document_fold_is_not_cached() -> None:
    texts = [f"Párrafo número {n}: ¿estás ahí? —dijo." for n in range(2000)]
    joined = "\r".join(texts) + "\r"
    assert len(joined) > CACHED_TEXT_MAX
    loosefold._fold_cached.cache_clear()
    loosefold._fold_text_cached.cache_clear()
    for level in FOLDED:
        fold(joined, level)
        fold_text(joined, level)
        phrase = "PÁRRAFO NÚMERO 7:" if level == "case" else "PARRAFO NUMERO 7 "
        assert joined_hits(texts, phrase, level, limit=8)
    # Only the windows of the hits were folded through the cache, a paragraph each.
    assert 0 < loosefold._fold_cached.cache_info().currsize <= 3 * 8
    assert loosefold._fold_text_cached.cache_info().currsize == 0
    cached = loosefold._fold_cached.cache_info().currsize
    for level in FOLDED:
        fold(joined, level)
    assert loosefold._fold_cached.cache_info().currsize == cached


def test_fold_text_equals_fold() -> None:
    rng = random.Random(7)
    for _ in range(4000):
        text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 12)))
        for level in FOLDED:
            assert fold_text(text, level) == fold(text, level).text, (text, level)


def test_bulk_fold_falls_back_where_it_cannot() -> None:
    # Every placeholder taken, a joining mark, too many characters outside Latin-1.
    crowded = "".join(map(chr, range(0x80, 0xA0))) + " µ İ"
    marked = "Cafe\u0301 ok"
    many = " ".join(chr(cp) for cp in range(0x3B1, 0x3B1 + 60))
    for text in (crowded, marked, many):
        for level in FOLDED:
            assert loosefold._bulk_fold(text, level) is None
            assert fold_text(text, level) == fold(text, level).text
    assert loosefold._bulk_fold("Straße  — µ ", "default") == "strasse μ"


def test_a_hit_ending_on_a_mark_widens_into_the_next_paragraph() -> None:
    texts = ["Al fondo.", "* * *", "Fin."]
    for level in ("default", "loose"):
        got = joined_hits(texts, "fondo.\r* * *", level, limit=8)
        assert [(h.start, h.end) for h in got] == _whole(texts, "fondo.\r* * *", level)
        assert got[0].end > len(texts[0]) + 1


def test_joiners_cover_every_composing_second_character() -> None:
    """Every character NFC can compose onto the one before is sent to the slow fold."""
    for cp in range(0x110000):
        ch = chr(cp)
        if unicodedata.combining(ch):
            assert loosefold._joins(ch)
        decomposition = unicodedata.decomposition(ch).split()
        if len(decomposition) == 2 and not decomposition[0].startswith("<"):
            assert loosefold._joins(chr(int(decomposition[1], 16))), hex(cp)


def test_joined_hits_equal_a_whole_document_search() -> None:
    rng = random.Random(11)
    for _ in range(300):
        texts = [
            "".join(rng.choice(ALPHABET.replace("\r", "")) for _ in range(rng.randint(0, 8)))
            for _ in range(rng.randint(1, 5))
        ]
        joined = "\r".join(texts) + "\r"
        start = rng.randrange(len(joined))
        phrase = joined[start : start + rng.randint(1, 6)]
        if rng.random() < 0.5:
            phrase = phrase.upper()
        for level in FOLDED:
            try:
                got = joined_hits(texts, phrase, level, limit=1000)
            except DocControlError:
                continue
            assert [(h.start, h.end) for h in got] == _whole(texts, phrase, level)


def test_joined_hits_stop_at_the_limit() -> None:
    texts = ["Estás ahí."] * 50
    assert len(joined_hits(texts, "estas ahi", "default", limit=3)) == 3


def test_joined_hits_refuse_the_exact_level() -> None:
    with pytest.raises(ValueError, match="folded level"):
        joined_hits(["x"], "x", "exact", limit=1)
