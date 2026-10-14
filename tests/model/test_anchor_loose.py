"""`anchor.resolve` with fold levels (FIND T6).

Gate: ``anchor_regressions == 0``. The folded stages (``case``, ``default``, ``loose``) run
after the normalised one and before fuzzy, only when no verbatim or normalised span
exists; the context score, the tie margin and the fingerprint rule are unchanged, and
``match_level: "exact"`` reproduces today's results (pinned by
``data/anchor_exact_golden.json``, recorded before the folded stages existed).
"""

from __future__ import annotations

import json
import random
import zipfile
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model import anchor as anchor_model
from doc_control.model.anchor import Anchor, Resolution, create, resolve
from doc_control.model.index import DocumentIndex, build_index
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import generate
from tests.helpers import document_xml, make_docx

GOLDEN = Path(__file__).parent / "data" / "anchor_exact_golden.json"


@pytest.fixture(autouse=True)
def _ops() -> None:
    load_operations()


def index_of(*paragraphs: str) -> DocumentIndex:
    return build_index(document_xml(*(escape(p) for p in paragraphs)).encode())


def run(op: str, **request: Any) -> dict[str, Any]:
    reply = REGISTRY[op].run(request)
    assert isinstance(reply, Reply)
    return reply.result


def _novel(seed: int) -> list[str]:
    data = zipfile.ZipFile(BytesIO(generate("novel", seed))).read("word/document.xml")
    return [p.text for p in build_index(data).paragraphs]


def _quote(index: DocumentIndex, number: int, start: int, end: int) -> Anchor:
    return create(index, f"{index.paragraph(number).address}:{start}-{end}")


SMALL: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("Uno.", "Al final, María cerró la puerta sin mirar atrás."), {"exact": "María cerró"}),
    (("Al final, María cerro la puerta sin mirar atrás.",), {"exact": "María cerró la puerta"}),
    (("Al final, maría cerró la puerta sin mirar atrás.",), {"exact": "María cerró la puerta"}),
    (("Al final, Maria cerro la puerta sin mirar atras.",), {"exact": "María cerró la puerta"}),
    (("-¿Estás ahí? -dijo.",), {"exact": "Estas ahi"}),
    (("--Estas ahi, dijo ella sin mirarlo.",), {"exact": "-¿Estás ahí?"}),
    (("Costó 5$ y nada más.",), {"exact": "Costó 5 y"}),
    (("la ca­sa  blanca", "Otra."), {"exact": "casa blanca"}),
    (("la casa  blanca", "la casa blanca"), {"exact": "casa blanca"}),
    (("Dijo no, no, no y se fue.",), {"exact": "no, "}),
    (("Dijo que sí, claro.", "Dijo que sí, claro."), {"exact": "Dijo que sí, claro."}),
    (("Dijo café con leche.",), {"exact": "café con leche"}),
    (("x x x", "dijo él", "y"), {"exact": "dijo él"}),
    (("x x x", "Dijo Él", "y"), {"exact": "dijo él"}),
    (
        ("Uno. María cerró la puerta.", "Dos. María cerró la puerta."),
        {"exact": "María cerró la puerta", "prefix": "zzz ", "suffix": " zzz"},
    ),
)


def _cases() -> Iterator[tuple[str, DocumentIndex, Anchor, bool]]:
    """``(name, index, anchor, exact_only)`` for the anchors of ``test_anchor*.py`` and the
    corpus: hand cases, the edit corpus and the decoy corpus of ``test_anchor.py``, and the
    noisy quotes of ``test_anchor_normalised.py``."""
    from tests.model.test_anchor import _pick_quotes, _swap_words, build_edited, quote_at

    for n, (paragraphs, data) in enumerate(SMALL):
        index = index_of(*paragraphs)
        for exact_only in (False, True):
            yield f"small{n}", index, Anchor.from_dict(data), exact_only

    texts = _novel(11)
    original = index_of(*texts)
    picks = _pick_quotes(texts, 160, seed=3)
    anchors = {i: quote_at(original, i + 1, texts[i][s:e]) for i, (s, e) in picks.items()}
    edited = index_of(*build_edited(texts, picks, seed=5)[0])
    for i, anchor in anchors.items():
        for exact_only in (False, True):
            yield f"edit{i}", edited, anchor, exact_only

    rng = random.Random(21)
    for i, (s, e) in list(_pick_quotes(texts, 30, seed=8).items())[:12]:
        anchor = quote_at(original, i + 1, texts[i][s:e])
        changed = list(texts)
        changed[i] = "Se borró aquel párrafo."
        changed[(i + 400) % len(changed)] = _swap_words(texts[i], rng, s, e)
        decoy = index_of(*changed)
        for exact_only in (False, True):
            yield f"decoy{i}", decoy, anchor, exact_only

    from tests.model.test_anchor_normalised import noisy

    rng = random.Random(41)
    clean = [t for t in _novel(3) if len(t) > 200][:20]
    noisy_index = index_of(*(noisy(t, rng) for t in clean))
    for n, text in enumerate(clean, 1):
        start = text.index(" ", 40) + 1
        end = text.index(" ", start + 50)
        yield f"noisy{n}", noisy_index, Anchor(exact=text[start:end]), True
        yield f"noisy{n}-cased", noisy_index, Anchor(exact=text[start:end].upper()), True


def _outcome(result: Resolution) -> dict[str, Any]:
    best = result.best
    return {
        "status": result.status,
        "best": None if best is None else [best.at, round(best.score, 6), best.kind],
        "candidates": [[c.at, round(c.score, 6), c.kind] for c in result.candidates],
    }


def test_two_verbatim_hits_pick_by_context(tmp_path: Path) -> None:
    paragraphs = ("Ella dijo: estás ahí, y luego calló.", "Él gritó: estás ahí, y se fue.")
    anchor = {"exact": "estás ahí", "prefix": "Ella dijo: ", "suffix": ", y luego calló."}
    result = resolve(index_of(*paragraphs), Anchor.from_dict(anchor), match_level="default")
    assert result.status == "found"
    assert result.best is not None and result.best.number == 1
    doc = make_docx(tmp_path / "a.docx", *paragraphs)
    for mode in ("best", "exact"):
        found = run("anchor.resolve", doc=str(doc), anchor=anchor, mode=mode)
        assert found["at"].startswith("p1#") and found["at"].endswith(":11-20")
        assert (found["match"], found["match_level"]) == ("exact", "exact")
        assert "matched_text" not in found


def test_exact_level_reproduces_today() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    names: list[str] = []
    regressions: list[str] = []
    for name, index, anchor, exact_only in _cases():
        names.append(name)
        got = _outcome(resolve(index, anchor, exact_only=exact_only, match_level="exact"))
        if got != golden[len(names) - 1]["outcome"]:
            regressions.append(f"{name} exact_only={exact_only}: {got}")
    assert names == [row["name"] for row in golden]
    print(f"anchor_regressions == {len(regressions)} (of {len(names)} resolutions)")
    assert not regressions, regressions[:5]


def test_mode_exact_with_default_level_skips_fuzzy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paragraphs = ("Nada que ver aquí.", "-¿Estás ahí? -dijo.")
    anchor = {"exact": "Estas ahi", "prefix": "Algo totalmente distinto ", "suffix": " zzz zzz"}
    doc = make_docx(tmp_path / "a.docx", *paragraphs)

    def no_fuzzy(*_: object) -> list[object]:
        raise AssertionError("fuzzy ran")

    monkeypatch.setattr(anchor_model, "_fuzzy", no_fuzzy)
    found = run("anchor.resolve", doc=str(doc), anchor=anchor, mode="exact")
    assert found["at"].startswith("p2#") and found["at"].endswith(":2-11")
    assert found["score"] < 0.8
    assert (found["match"], found["match_level"]) == ("normalized", "default")
    # match_level exact keeps today's rule: no verbatim or normalised span, so not found.
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor=anchor, mode="exact", match_level="exact")
    assert err.value.code is ErrorCode.ANCHOR_NOT_FOUND


def test_folded_hit_reports_normalized_match(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Uno.", "-¿Estás ahí? -dijo.", "Costó 5$ y nada más.")
    found = run("anchor.resolve", doc=str(doc), anchor={"exact": "Estas ahi"})
    assert found["at"].startswith("p2#") and found["at"].endswith(":2-11")
    assert found["match"] == "normalized"
    assert found["match_level"] == "default"
    assert found["matched_text"] == "Estás ahí"
    loose = run("anchor.resolve", doc=str(doc), anchor={"exact": "Costó 5 y"}, mode="exact")
    assert (loose["match"], loose["match_level"], loose["matched_text"]) == (
        "normalized",
        "loose",
        "Costó 5$ y",
    )
    cased = run("anchor.resolve", doc=str(doc), anchor={"exact": "uno."}, mode="exact")
    assert (cased["match"], cased["match_level"], cased["matched_text"]) == (
        "normalized",
        "case",
        "Uno.",
    )


def test_normalised_tier_reports_case_and_fuzzy_reports_no_level(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "la ca­sa  blanca", "Al final, María cerró la pverta.")
    found = run("anchor.resolve", doc=str(doc), anchor={"exact": "casa blanca"}, mode="exact")
    assert (found["match"], found["match_level"]) == ("normalized", "case")
    assert found["matched_text"] == "ca­sa  blanca"
    fuzzy = run(
        "anchor.resolve", doc=str(doc), anchor={"exact": "María cerró la puerta", "hint": "p2"}
    )
    assert fuzzy["match"] == "fuzzy"
    assert "match_level" not in fuzzy and "matched_text" not in fuzzy


def test_folded_ambiguity_candidates_name_their_level(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "-¿Estás ahí?", "Uno.", "-¿Estás ahí?")
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor={"exact": "Estas ahi"}, mode="exact")
    assert err.value.code is ErrorCode.ANCHOR_AMBIGUOUS
    candidates = err.value.details["candidates"]
    assert [(c["match"], c["match_level"]) for c in candidates] == [("normalized", "default")] * 2


def test_unknown_match_level_is_rejected(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Uno.")
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor={"exact": "Uno."}, match_level="case")
    assert err.value.code is ErrorCode.SCHEMA_INVALID


@pytest.mark.parametrize("quote", ["—", "...", "¿?", "$"])
@pytest.mark.parametrize("mode", ["best", "exact"])
def test_symbol_only_quote_gone_is_not_found(tmp_path: Path, quote: str, mode: str) -> None:
    doc = make_docx(tmp_path / "a.docx", "Uno.", "Dos y tres.")
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor={"exact": quote}, mode=mode)
    assert err.value.code is ErrorCode.ANCHOR_NOT_FOUND
