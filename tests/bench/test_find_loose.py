"""Loose find on the 100k-word novel (FIND T9): what ``match_level`` costs.

Gates: ``find_loose_overhead_ratio <= 1.5``: the median time of ``find`` with
``match_level: "default"`` over 20 queries, against the same queries as literal ``find``
(no ``match_level``), on one open document; and ``live_select_loose_extra_ms <= 50``: the
p95, over the same phrases, of ``live.select``'s phrase resolution (``_found``) at
``default`` minus at ``exact``, on the novel's paragraph texts, without LibreOffice.

The queries are phrases of the novel as written, the same with accents or punctuation
dropped (found only when folded), and phrases it does not hold. Each query is timed
interleaved in both modes, after a warm-up, as the median of several runs, so machine
load hits both alike. The ratio is also printed with the fold caches emptied before each
run (``find_loose_cold_ratio``), the first search of a fresh process; that one is
reported, not gated. Opt-in like every wall-clock test (``make bench``); it runs when
named by node id or ``-k``.
"""

from __future__ import annotations

import statistics
import time
import unicodedata
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model import loosefold
from doc_control.model.loosefold import Level
from doc_control.ops.live import _found
from tests.corpus.generator import generate

MAX_RATIO = 1.5
MAX_EXTRA_MS = 50.0
REPEATS = 7
COLD_REPEATS = 3
PHRASES = 40


@pytest.fixture(scope="module")
def novel(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[api.OpenDocument, list[str]]]:
    path = tmp_path_factory.mktemp("find_loose") / "novel.docx"
    path.write_bytes(generate("novel", 1))
    with api.open(path) as doc:
        texts = [str(row["text"]) for row in doc.paragraphs()]
        yield doc, texts


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize(
        "NFC", "".join(c for c in decomposed if not unicodedata.combining(c))
    )


def _strip_punctuation(text: str) -> str:
    bare = "".join(" " if unicodedata.category(c)[0] == "P" else c for c in text)
    return " ".join(bare.split())


def _words(text: str, start: int, count: int) -> str:
    return " ".join(text.split()[start : start + count])


def _queries(texts: list[str]) -> list[str]:
    """20 queries: 8 as written, 5 without accents, 4 without punctuation, 3 not there."""
    prose = [t for t in texts if len(t.split()) >= 12]
    accented = [t for t in prose if _strip_accents(t) != t]
    punctuated = [t for t in prose if sum(unicodedata.category(c)[0] == "P" for c in t) >= 4]
    step = len(prose) // 8
    out = [_words(prose[k * step], 2 + k % 3, 3 + k % 3) for k in range(8)]
    step = len(accented) // 5
    out += [_strip_accents(_words(accented[k * step + 3], 1, 4)).lower() for k in range(5)]
    # A dialogue opening: its dash, comma and quote marks dropped.
    step = len(punctuated) // 4
    out += [_strip_punctuation(_words(punctuated[k * step + 7], 0, 5)) for k in range(4)]
    out += ["zanahoria púrpura voladora", "the quick brown fox", "Ángel olvidaba"]
    assert len(out) == 20 and len(set(out)) == 20
    return out


def _clear_fold_caches() -> None:
    loosefold._fold_cached.cache_clear()
    loosefold._fold_text_cached.cache_clear()


def _timed(fn: Callable[[], Any], before: Callable[[], None] | None = None) -> float:
    if before is not None:
        before()
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def _find(doc: api.OpenDocument, query: str, level: Level | None) -> Callable[[], Any]:
    request: dict[str, Any] = {"query": query}
    if level is not None:
        request["match_level"] = level
    return lambda: doc.run("find", request)


def _ratio(
    doc: api.OpenDocument, queries: list[str], repeats: int, before: Callable[[], None] | None
) -> tuple[float, float, float]:
    """Median over queries of each mode's median run, and their ratio (loose / literal)."""
    literal, loose = [], []
    for query in queries:
        runs: dict[str, list[float]] = {"literal": [], "loose": []}
        for _ in range(repeats):  # interleaved, so drift hits both
            runs["literal"].append(_timed(_find(doc, query, None), before))
            runs["loose"].append(_timed(_find(doc, query, "default"), before))
        literal.append(statistics.median(runs["literal"]))
        loose.append(statistics.median(runs["loose"]))
    lit, fold = statistics.median(literal), statistics.median(loose)
    return lit, fold, fold / lit


@pytest.mark.bench
def test_find_loose_overhead_on_novel(novel: tuple[api.OpenDocument, list[str]]) -> None:
    doc, texts = novel
    queries = _queries(texts)
    found = {q: doc.run("find", {"query": q, "match_level": "default"})["total"] for q in queries}
    assert sum(1 for n in found.values() if n) >= 15, found  # the folded queries do find
    assert all(found[q] == 0 for q in queries[-3:]), found
    for query in queries:  # warm-up: the parse, the fold caches, the compiled patterns
        _find(doc, query, None)()
        _find(doc, query, "default")()
    lit, fold, ratio = _ratio(doc, queries, REPEATS, None)
    cold_lit, cold_fold, cold = _ratio(doc, queries, COLD_REPEATS, _clear_fold_caches)
    print(
        f"find_loose_overhead_ratio={ratio:.2f} (literal {lit * 1000:.1f} ms,"
        f" default {fold * 1000:.1f} ms, budget {MAX_RATIO}, n={len(queries)})"
    )
    print(
        f"find_loose_cold_ratio={cold:.2f} (literal {cold_lit * 1000:.1f} ms,"
        f" default {cold_fold * 1000:.1f} ms, fold caches emptied before each run)"
    )
    assert ratio <= MAX_RATIO


def _resolve(texts: list[str], phrase: str, level: Level) -> float:
    def run() -> None:
        try:
            _found(texts, phrase, None, level)
        except DocControlError as exc:  # exact misses what only a fold finds
            assert exc.code in (ErrorCode.ANCHOR_NOT_FOUND, ErrorCode.ANCHOR_AMBIGUOUS)

    return statistics.median(_timed(run) for _ in range(REPEATS))


def _resolves(texts: list[str], phrase: str) -> bool:
    try:
        _found(texts, phrase, None, "default")
    except DocControlError as exc:
        assert exc.code == ErrorCode.ANCHOR_AMBIGUOUS
        return False
    return True


@pytest.mark.bench
def test_live_select_loose_extra_ms(novel: tuple[api.OpenDocument, list[str]]) -> None:
    doc, texts = novel
    rows = doc.run("locate", {"structure": False})["paragraphs"]
    phrased = [str(r["find_phrase"]) for r in rows if r.get("find_phrase")]
    picks = phrased[:: max(1, len(phrased) // PHRASES)][:PHRASES]
    # As locate gives them, lowercased, and without accents: exact (a casefold match)
    # meets the first two; only the fold meets the third.
    variants = picks + [p.lower() for p in picks[::4]] + [_strip_accents(p) for p in picks[1::4]]
    phrases = [p for p in variants if _resolves(texts, p)]  # a fold can make a phrase ambiguous
    assert len(phrases) >= len(variants) * 0.8, len(phrases)
    for phrase in phrases:
        _resolve(texts, phrase, "exact")  # warm-up
    extra = sorted(
        (_resolve(texts, p, "default") - _resolve(texts, p, "exact")) * 1000 for p in phrases
    )
    p95 = extra[max(0, int(len(extra) * 0.95) - 1)]
    print(
        f"live_select_loose_extra_ms={p95:.1f} (median {statistics.median(extra):.1f},"
        f" budget {MAX_EXTRA_MS:g}, n={len(phrases)})"
    )
    assert p95 <= MAX_EXTRA_MS
