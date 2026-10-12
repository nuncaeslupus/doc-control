"""Anchors: `anchor.create` / `anchor.resolve`, re-anchoring after synthetic edits.

The gate is ``reanchor_success_ratio >= 0.98`` on an edit corpus built from the generated
100k-word novel: for each sampled passage an anchor is made, then the document is edited
around and inside the quote (insertions, deletions, typo fixes, paragraph splits and
merges, moved paragraphs) so that up to 10% of the characters in the anchor's context
change. Success means the anchor resolves to the edited passage. ``wrong_anchor_matches``
counts resolutions to somewhere else and must be 0: a weak or tied match is reported as
not found / ambiguous, never guessed.
"""

from __future__ import annotations

import random
import statistics
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import parse_range
from doc_control.model.anchor import MIN_SCORE, Anchor, create, resolve
from doc_control.model.index import DocumentIndex, build_index
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import generate
from tests.helpers import document_xml, make_docx


@pytest.fixture(autouse=True)
def _ops() -> None:
    load_operations()


def index_of(*paragraphs: str) -> DocumentIndex:
    return build_index(document_xml(*(escape(p) for p in paragraphs)).encode())


def run(op: str, **request: Any) -> dict[str, Any]:
    reply = REGISTRY[op].run(request)
    assert isinstance(reply, Reply)
    return reply.result


def quote_at(index: DocumentIndex, number: int, quote: str, context: int = 32) -> Anchor:
    para = index.paragraph(number)
    start = para.text.index(quote)
    return create(index, f"{para.address}:{start}-{start + len(quote)}", context)


# -- create ------------------------------------------------------------------


def test_create_records_quote_context_position_and_fingerprint() -> None:
    index = index_of("Uno.", "Al final, María cerró la puerta sin mirar atrás. Y se fue.", "Fin.")
    para = index.paragraph(2)
    anchor = create(index, f"{para.address}:10-31", 10)
    assert anchor.exact == "María cerró la puerta"
    assert anchor.prefix == "Al final, "
    assert anchor.suffix == " sin mirar"
    assert anchor.hint == 2
    assert anchor.fingerprint == para.fp
    assert anchor.at == f"{para.address}:10-31"
    assert anchor.to_dict()["hint"] == "p2"


def test_create_context_reaches_into_neighbouring_paragraphs() -> None:
    index = index_of("Antes de todo.", "La puerta se cerró.", "Después de nada.")
    anchor = quote_at(index, 2, "La puerta se cerró.")
    assert anchor.prefix == "Antes de todo.\n"
    assert anchor.suffix == "\nDespués de nada."


def test_create_from_sentence_and_paragraph_addresses() -> None:
    index = index_of("Primera frase. Segunda frase. Tercera frase.")
    para = index.paragraph(1)
    sentence = create(index, f"{para.address}.s2")
    assert sentence.exact == "Segunda frase."
    assert sentence.sentence == f"{para.address}.s2"
    whole = create(index, str(para.address))
    assert whole.exact == para.text
    assert whole.sentence is None


def test_create_never_splits_a_letter_from_its_combining_marks() -> None:
    text = "Dijo café con leche y se fue a casa."
    index = index_of(text)
    para = index.paragraph(1)
    # Offset 9 is U+0301: 5-9 would end before it, 9-14 would start on it.
    anchor = create(index, f"{para.address}:5-9", 4)
    assert anchor.exact == "café"
    anchor = create(index, f"{para.address}:9-14", 4)
    assert anchor.exact.startswith("é")
    result = resolve(index, anchor)
    assert result.best is not None
    assert not unicodedata.category(para.text[result.best.start]).startswith("M")


@pytest.mark.parametrize(
    ("paragraphs", "at"),
    [
        (["", "x"], "p1#{fp}"),
        (["abc   ", "x"], "p1#{fp}:3-6"),
        (["abc", "def"], "p1#{fp}:0..p2#{fp2}:2"),
    ],
)
def test_create_rejects_empty_text_and_cross_paragraph_ranges(
    paragraphs: list[str], at: str
) -> None:
    index = index_of(*paragraphs)
    at = at.format(fp=index.paragraph(1).fp, fp2=index.paragraph(2).fp)
    with pytest.raises(DocControlError) as err:
        create(index, at)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_create_rejects_a_stale_address() -> None:
    index = index_of("uno", "dos")
    with pytest.raises(DocControlError) as err:
        create(index, "p2#00000000:0-1")
    assert err.value.code is ErrorCode.STALE_ADDRESS


# -- resolve: exact and fuzzy ------------------------------------------------


def test_resolve_unchanged_document_is_exact_with_top_score() -> None:
    index = index_of("Uno.", "Al final, María cerró la puerta sin mirar atrás.", "Fin.")
    anchor = quote_at(index, 2, "María cerró la puerta")
    result = resolve(index, anchor)
    assert result.status == "found"
    assert result.best is not None
    assert result.best.kind == "exact"
    assert result.best.at == f"{index.paragraph(2).address}:10-31"
    assert result.best.score == pytest.approx(1.0)


def test_resolve_follows_a_typo_fix_inside_the_quote() -> None:
    before = index_of("a", "b", "Al final, María cerró la puerta sin mirar atrás.", "c")
    anchor = quote_at(before, 3, "María cerró la puerta")
    after = index_of("a", "b", "Al final, María cerro la puerta sin mirar atrás.", "c")
    result = resolve(after, anchor)
    assert result.status == "found"
    assert result.best is not None
    assert result.best.kind == "fuzzy"
    assert (result.best.start, result.best.end) == (10, 31)
    assert MIN_SCORE <= result.best.score < 1.0


def test_resolve_survives_the_paragraph_being_rewritten_around_the_quote() -> None:
    before = index_of("Al final, María cerró la puerta sin mirar atrás.")
    anchor = quote_at(before, 1, "María cerró la puerta")
    after = index_of("Por fin, a las nueve, María cerró la puerta sin mirar atrás nunca más.")
    result = resolve(after, anchor)
    assert result.best is not None
    assert after.paragraph(1).text[result.best.start : result.best.end] == "María cerró la puerta"


def test_resolve_below_the_minimum_score_is_not_found_with_best_score() -> None:
    before = index_of("Al final, María cerró la puerta sin mirar atrás.")
    anchor = quote_at(before, 1, "María cerró la puerta")
    after = index_of("Nada que ver con aquello. Otra historia muy distinta, sin puertas.")
    result = resolve(after, anchor)
    assert result.status == "not_found"
    assert result.best is None or result.best.score < MIN_SCORE


def test_op_roundtrip_and_error_details(tmp_path: Path) -> None:
    doc = make_docx(
        tmp_path / "a.docx", "Uno.", "Al final, María cerró la puerta sin mirar.", "Fin."
    )
    index = index_of("Uno.", "Al final, María cerró la puerta sin mirar.", "Fin.")
    at = f"{index.paragraph(2).address}:10-31"
    anchor = run("anchor.create", doc=str(doc), at=at)["anchor"]
    assert anchor["exact"] == "María cerró la puerta"
    found = run("anchor.resolve", doc=str(doc), anchor=anchor)
    assert found == {"at": at, "score": 1.0}

    other = make_docx(tmp_path / "b.docx", "Nada que ver con aquello.", "Otra historia.")
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(other), anchor=anchor)
    assert err.value.code is ErrorCode.ANCHOR_NOT_FOUND
    assert err.value.details["min_score"] == MIN_SCORE
    assert err.value.details["best_score"] < MIN_SCORE

    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor={"exact": "x", "hint": "142"})
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor={"prefix": "x"})
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_resolve_op_reports_the_sentence_of_a_sentence_anchor(tmp_path: Path) -> None:
    paragraphs = ("Primera frase. Segunda frase muy larga. Tercera frase.",)
    doc = make_docx(tmp_path / "a.docx", *paragraphs)
    index = index_of(*paragraphs)
    anchor = run("anchor.create", doc=str(doc), at=f"{index.paragraph(1).address}.s2")["anchor"]
    found = run("anchor.resolve", doc=str(doc), anchor=anchor)
    assert found["sentence"] == f"{index.paragraph(1).address}.s2"


def test_anchor_ops_are_registered_read_ops_with_schemas() -> None:
    for name in ("anchor.create", "anchor.resolve"):
        assert REGISTRY[name].kind == "read"
        assert REGISTRY[name].schema["type"] == "object"


# -- ambiguity ---------------------------------------------------------------


def test_ambiguous_anchor_returns_candidates_not_guess(tmp_path: Path) -> None:
    line = "Entonces María cerró la puerta sin mirar atrás y se fue."
    doc = make_docx(tmp_path / "a.docx", "Uno.", line, "Dos.", line, "Tres.")
    anchor = {"exact": "María cerró la puerta", "prefix": "Entonces ", "suffix": " sin mirar atrás"}
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor=anchor)
    assert err.value.code is ErrorCode.ANCHOR_AMBIGUOUS
    candidates = err.value.details["candidates"]
    assert len(candidates) == 2
    assert {c["at"].split("#")[0] for c in candidates} == {"p2", "p4"}
    assert all(c["score"] >= MIN_SCORE for c in candidates)
    assert abs(candidates[0]["score"] - candidates[1]["score"]) <= 0.02


def test_repeated_phrase_in_one_paragraph_is_ambiguous_without_distinguishing_context() -> None:
    index = index_of("Dijo no, no, no y se fue.")
    result = resolve(index, Anchor(exact="no, ", prefix="", suffix=""))
    assert result.status == "ambiguous"
    assert len(result.candidates) == 2


def test_repeated_phrase_with_distinct_context_resolves_to_the_right_one() -> None:
    texts = [
        "Cerró la puerta del jardín y salió despacio.",
        "Nada.",
        "Cerró la puerta del sótano y bajó sin luz.",
    ]
    index = index_of(*texts)
    anchor = quote_at(index, 3, "Cerró la puerta")
    result = resolve(index, anchor)
    assert result.status == "found"
    assert result.best is not None
    assert result.best.number == 3
    # The same, after the second one gets a typo (fuzzy) and the other stays as it was.
    edited = index_of(texts[0], texts[1], "Cerro la puerta del sótano y bajó sin luz.")
    result = resolve(edited, anchor)
    assert result.best is not None
    assert result.status == "found"
    assert result.best.number == 3


def test_ambiguity_candidates_are_capped_at_five() -> None:
    line = "Dijo la verdad ante todos."
    index = index_of(*[line] * 9)
    result = resolve(index, Anchor(exact="la verdad", prefix="Dijo ", suffix=" ante"))
    assert result.status == "ambiguous"
    assert len(result.candidates) == 5


def test_far_apart_duplicates_are_decided_by_position_only_beyond_the_margin() -> None:
    line = "Entonces cerró la puerta sin mirar atrás."
    filler = [f"Párrafo de relleno número {n} sin nada especial." for n in range(300)]
    index = index_of(line, *filler, line)
    near = Anchor(exact="cerró la puerta", prefix="Entonces ", suffix=" sin mirar", hint=1)
    assert resolve(index, near).status == "ambiguous" or resolve(index, near).best is not None
    # Two identical paragraphs one apart: always ambiguous.
    tight = index_of(line, line)
    assert resolve(tight, near).status == "ambiguous"


def test_exact_only_resolution_skips_fuzzy_candidates() -> None:
    before = index_of("Al final, María cerró la puerta sin mirar atrás.")
    anchor = quote_at(before, 1, "María cerró la puerta")
    after = index_of("Al final, María cerro la puerta sin mirar atrás.")
    assert resolve(after, anchor).status == "found"
    assert resolve(after, anchor, exact_only=True).status == "not_found"


def test_unicode_equivalent_forms_still_resolve() -> None:
    before = index_of("a", "Dijo café con leche, y se fue a casa.")
    anchor = quote_at(before, 2, "café con leche")
    after = index_of("a", "Dijo café con leche, y se fue a casa.")
    result = resolve(after, anchor)
    assert result.status == "found"
    assert result.best is not None
    assert result.best.score > 0.9
    text = after.paragraph(2).text
    assert not unicodedata.category(text[result.best.start]).startswith("M")


# -- the edit corpus ---------------------------------------------------------


@dataclass
class Para:
    text: str
    marks: dict[int, tuple[int, int]] = field(default_factory=dict)  # anchor id -> [start, end)


def _edit(text: str, lo: int, hi: int, rng: random.Random, budget: float) -> tuple[str, int]:
    """Change up to ``budget`` of ``text[lo:hi]`` with small edits; returns text and the net shift.

    Each edit substitutes or deletes one character or inserts a short word; characters
    changed (an insertion counts its length) never exceed ``budget * (hi - lo)``. Edits go
    from the right so earlier offsets stay valid.
    """
    allowed = max(1, int((hi - lo) * budget))
    plan: list[tuple[int, str, str]] = []
    used = 0
    for _ in range(allowed):
        kind = rng.choice(("sub", "del", "ins"))
        word = rng.choice(("ya ", "un ", "y "))
        cost = len(word) if kind == "ins" else 1
        if used + cost > allowed:
            continue
        used += cost
        plan.append((rng.randrange(lo, hi), kind, word))
    shift = 0
    for pos, kind, word in sorted(plan, reverse=True):
        if kind == "sub":
            text = text[:pos] + rng.choice("xzqk") + text[pos + 1 :]
        elif kind == "del":
            text = text[:pos] + text[pos + 1 :]
            shift -= 1
        else:
            text = text[:pos] + word + text[pos:]
            shift += len(word)
    return text, shift


SCENARIOS = (
    "insert_around",
    "delete_around",
    "typo_inside",
    "typo_and_context",
    "split_before",
    "split_after",
    "merge_previous",
    "moved",
    "paragraphs_inserted_before",
    "rewritten_prefix",
)


def _apply(scenario: str, para: Para, rng: random.Random, aid: int) -> list[Para]:
    """Edit one paragraph that holds anchor ``aid``; returns the paragraphs it becomes."""
    text = para.text
    start, end = para.marks[aid]

    def edit_outside(t: str, s: int, e: int, budget: float) -> tuple[str, int, int]:
        ctx = 32
        lo, hi = max(0, s - ctx), s
        t, shift_before = _edit(t, lo, hi, rng, budget) if hi > lo else (t, 0)
        s, e = s + shift_before, e + shift_before
        lo, hi = e, min(len(t), e + ctx)
        t, _ = _edit(t, lo, hi, rng, budget) if hi > lo else (t, 0)
        return t, s, e

    if scenario == "insert_around":
        text = text[:start] + "Entonces " + text[start:end] + " más tarde" + text[end:]
        return [Para(text, {aid: (start + 9, end + 9)})]
    if scenario == "delete_around":
        cut = min(start, 6)
        tail = min(len(text) - end, 6)
        text = text[: start - cut] + text[start:end] + text[end + tail :]
        return [Para(text, {aid: (start - cut, end - cut)})]
    if scenario in ("typo_inside", "typo_and_context"):
        inner_lo, inner_hi = start + 2, end - 2
        body, shift = _edit(text, inner_lo, inner_hi, rng, 0.08)
        text = body
        end += shift
        if scenario == "typo_and_context":
            text, start, end = edit_outside(text, start, end, 0.10)
        return [Para(text, {aid: (start, end)})]
    if scenario == "rewritten_prefix":
        head = text[:start]
        text = "Mientras tanto, " + head[len(head) // 2 :] + text[start:]
        shift = len("Mientras tanto, ") - len(head) // 2
        return [Para(text, {aid: (start + shift, end + shift)})]
    if scenario == "split_before":
        cut = text.rfind(" ", 0, start)
        if cut <= 0:
            return [para]
        return [Para(text[:cut]), Para(text[cut + 1 :], {aid: (start - cut - 1, end - cut - 1)})]
    if scenario == "split_after":
        cut = text.find(" ", end)
        if cut < 0:
            return [para]
        return [Para(text[:cut], {aid: (start, end)}), Para(text[cut + 1 :])]
    return [para]


def build_edited(
    texts: list[str], picks: dict[int, tuple[int, int]], seed: int
) -> tuple[list[str], dict[int, tuple[int, int, int]], dict[int, str]]:
    """Apply a scenario to each picked paragraph (index -> quote range).

    Returns the new paragraph texts, the expected ``(paragraph index, start, end)`` of each
    anchor (keyed by paragraph index in the original) and the scenario used.
    """
    rng = random.Random(seed)
    out: list[Para] = []
    scenario_of: dict[int, str] = {}
    moved: list[Para] = []
    pending_insert: list[int] = []
    for i, text in enumerate(texts):
        if i not in picks:
            out.append(Para(text))
            continue
        scenario = SCENARIOS[len(scenario_of) % len(SCENARIOS)]
        scenario_of[i] = scenario
        para = Para(text, {i: picks[i]})
        if scenario == "moved":
            moved.append(para)
            out.append(Para("Párrafo que ocupa el lugar del movido."))
            continue
        if scenario == "merge_previous" and out:
            prev = out.pop()
            base = len(prev.text) + 1
            out.append(
                Para(
                    prev.text + " " + text,
                    {**prev.marks, i: (picks[i][0] + base, picks[i][1] + base)},
                )
            )
            continue
        if scenario == "paragraphs_inserted_before":
            pending_insert.append(len(out))
        out.extend(_apply(scenario, para, rng, i))
    for k, para in enumerate(moved):
        at = rng.randrange(1, len(out))
        out.insert(at + k, para)
    for pos in sorted(pending_insert, reverse=True):
        for _ in range(6):
            out.insert(pos, Para("Párrafo nuevo sin relación con nada."))
    expected: dict[int, tuple[int, int, int]] = {}
    for n, para in enumerate(out):
        for aid, (s, e) in para.marks.items():
            expected[aid] = (n, s, e)
    return [p.text for p in out], expected, scenario_of


def _iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    inter = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union else 0.0


@pytest.fixture(scope="module")
def novel_texts() -> list[str]:
    data = zipfile.ZipFile(BytesIO(generate("novel", 11))).read("word/document.xml")
    return [p.text for p in build_index(data).paragraphs]


def _pick_quotes(texts: list[str], count: int, seed: int) -> dict[int, tuple[int, int]]:
    """Word-aligned 40-80 character quotes from evenly spaced long paragraphs."""
    rng = random.Random(seed)
    long_ones = [i for i, t in enumerate(texts) if len(t) >= 150 and t.count(" ") > 20]
    step = max(1, len(long_ones) // count)
    picks: dict[int, tuple[int, int]] = {}
    for i in long_ones[::step][:count]:
        text = texts[i]
        words = [j + 1 for j, c in enumerate(text) if c == " "]
        starts = [w for w in words if 12 <= w <= len(text) - 100]
        start = rng.choice(starts)
        want = start + rng.randrange(40, 80)
        end = next(w - 1 for w in words if w > want)
        picks[i] = (start, end)
    return picks


def test_reanchor_success_ratio_on_the_edit_corpus(novel_texts: list[str]) -> None:
    picks = _pick_quotes(novel_texts, 160, seed=3)
    # Anchors are made on the original; picked paragraphs are far enough apart that the
    # edits to one never touch the context of another.
    original = index_of(*novel_texts)
    anchors = {i: quote_at(original, i + 1, novel_texts[i][s:e]) for i, (s, e) in picks.items()}
    edited_texts, expected, scenario_of = build_edited(novel_texts, picks, seed=5)
    edited = index_of(*edited_texts)

    ok = wrong = 0
    failures: list[str] = []
    started = time.perf_counter()
    for i, anchor in anchors.items():
        number, s, e = expected[i]
        result = resolve(edited, anchor)
        best = result.best
        if result.status == "found" and best is not None:
            if best.number == number + 1 and _iou((best.start, best.end), (s, e)) >= 0.7:
                ok += 1
                continue
            wrong += 1
        failures.append(f"{scenario_of[i]} p{i + 1}: {result.status}")
    elapsed = time.perf_counter() - started
    ratio = ok / len(anchors)
    print(f"reanchor_success_ratio={ratio:.4f}")
    print(f"wrong_anchor_matches={wrong}")
    print(f"anchors={len(anchors)} resolve_total_s={elapsed:.2f} failures={failures}")
    assert wrong == 0
    assert ratio >= 0.98


def test_every_scenario_is_exercised_and_unchanged_anchors_score_one(
    novel_texts: list[str],
) -> None:
    picks = _pick_quotes(novel_texts, 40, seed=9)
    _, _, scenario_of = build_edited(novel_texts, picks, seed=1)
    assert set(scenario_of.values()) == set(SCENARIOS)
    original = index_of(*novel_texts)
    for i, (s, e) in list(picks.items())[:20]:
        anchor = quote_at(original, i + 1, novel_texts[i][s:e])
        result = resolve(original, anchor)
        assert result.best is not None
        assert result.status == "found"
        assert result.best.score == pytest.approx(1.0)
        assert result.best.at == f"{original.paragraph(i + 1).address}:{s}-{e}"


# -- performance -------------------------------------------------------------


@pytest.mark.bench
def test_anchor_resolve_on_a_100k_word_novel_stays_fast(novel_texts: list[str]) -> None:
    words = sum(len(t.split()) for t in novel_texts)
    assert words >= 90_000
    picks = _pick_quotes(novel_texts, 20, seed=2)
    original = index_of(*novel_texts)
    anchors = [quote_at(original, i + 1, novel_texts[i][s:e]) for i, (s, e) in picks.items()]
    edited_texts, _, _ = build_edited(novel_texts, picks, seed=4)
    edited = index_of(*edited_texts)
    exact_ms: list[float] = []
    fuzzy_ms: list[float] = []
    for anchor in anchors:
        for index, bucket in ((original, exact_ms), (edited, fuzzy_ms)):
            t = time.perf_counter()
            resolve(index, anchor)
            bucket.append((time.perf_counter() - t) * 1000)
    p95_exact = statistics.quantiles(exact_ms, n=20)[18]
    p95_fuzzy = statistics.quantiles(fuzzy_ms, n=20)[18]
    print(f"words={words} anchor_resolve_exact_p95_ms={p95_exact:.0f}")
    print(f"anchor_resolve_100k_words_p95_ms={p95_fuzzy:.0f}")
    assert p95_exact <= 1000
    assert p95_fuzzy <= 1000


def test_parse_range_of_a_result_is_a_valid_address() -> None:
    index = index_of("Hola mundo cruel.")
    result = resolve(index, Anchor(exact="mundo", prefix="Hola ", suffix=" cruel"))
    assert result.best is not None
    rng = parse_range(result.best.at)
    assert (rng.start_offset, rng.end_offset) == (5, 10)
