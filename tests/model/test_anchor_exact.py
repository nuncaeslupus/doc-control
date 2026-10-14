"""`anchor.resolve` exact mode and match kind (T34).

Gate: ``exact_mode_fuzzy_matches == 0``. In ``mode: "exact"`` a one-character change to the
quote is ``ANCHOR_NOT_FOUND`` (never a fuzzy match), while ``mode: "best"`` still finds it
and says ``match: fuzzy``. Every successful response names its match kind.
"""

from __future__ import annotations

import random
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.anchor import create
from doc_control.model.index import build_index
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import generate
from tests.helpers import make_docx


@pytest.fixture(autouse=True)
def _ops() -> None:
    load_operations()


def run(op: str, **request: Any) -> dict[str, Any]:
    reply = REGISTRY[op].run(request)
    assert isinstance(reply, Reply)
    return reply.result


def _novel() -> list[str]:
    data = zipfile.ZipFile(BytesIO(generate("novel", 11))).read("word/document.xml")
    return [p.text for p in build_index(data).paragraphs]


def _one_char_change(quote: str, rng: random.Random) -> str:
    """Substitute, delete or insert one character inside a word of ``quote``."""
    spots = [i for i in range(2, len(quote) - 2) if quote[i].isalpha()]
    i = rng.choice(spots)
    kind = rng.choice(["substitute", "delete", "insert"])
    letters = "abcdefghijklmnopqrstuvwxyz"
    if kind == "substitute":
        other = rng.choice([c for c in letters if c != quote[i].lower()])
        return quote[:i] + other + quote[i + 1 :]
    if kind == "delete":
        return quote[:i] + quote[i + 1 :]
    return quote[:i] + rng.choice(letters) + quote[i:]


def test_exact_mode_rejects_fuzzy_candidate(tmp_path: Path) -> None:
    texts = _novel()
    original = make_docx(tmp_path / "original.docx", *texts)
    index = build_index(zipfile.ZipFile(original).read("word/document.xml"))
    rng = random.Random(34)
    long_ones = [i for i, t in enumerate(texts) if len(t) >= 150 and t.count(" ") > 20]
    picks = long_ones[:: max(1, len(long_ones) // 40)][:40]
    fuzzy_matches = 0
    best_fuzzy = 0
    for n, i in enumerate(picks):
        text = texts[i]
        words = [j + 1 for j, c in enumerate(text) if c == " "]
        start = rng.choice([w for w in words if 12 <= w <= len(text) - 100])
        end = next(w - 1 for w in words if w > start + rng.randrange(40, 80))
        anchor = create(index, f"{index.paragraph(i + 1).address}:{start}-{end}", 32).to_dict()
        edited_quote = _one_char_change(text[start:end], rng)
        assert edited_quote != text[start:end]
        edited = list(texts)
        edited[i] = text[:start] + edited_quote + text[end:]
        doc = make_docx(tmp_path / f"edited{n}.docx", *edited)

        unchanged = run("anchor.resolve", doc=str(original), anchor=anchor, mode="exact")
        assert unchanged["match"] == "exact"
        try:
            run("anchor.resolve", doc=str(doc), anchor=anchor, mode="exact")
        except DocControlError as err:
            assert err.code is ErrorCode.ANCHOR_NOT_FOUND
        else:  # any answer at all means exact mode accepted a changed quote
            fuzzy_matches += 1
        if run("anchor.resolve", doc=str(doc), anchor=anchor, mode="best")["match"] == "fuzzy":
            best_fuzzy += 1

    print(f"exact_mode_fuzzy_matches={fuzzy_matches} (of {len(picks)} one-character edits)")
    print(f"best_mode_fuzzy_matches={best_fuzzy} (of {len(picks)})")
    assert len(picks) >= 30
    assert fuzzy_matches == 0
    assert best_fuzzy == len(picks)


def test_default_mode_is_best_and_reports_match_kind(tmp_path: Path) -> None:
    before = make_docx(tmp_path / "a.docx", "Uno.", "Al final, María cerró la puerta sin mirar.")
    index = build_index(zipfile.ZipFile(before).read("word/document.xml"))
    at = f"{index.paragraph(2).address}:10-31"
    anchor = run("anchor.create", doc=str(before), at=at)["anchor"]
    for mode in ({}, {"mode": "best"}, {"mode": "exact"}):
        assert run("anchor.resolve", doc=str(before), anchor=anchor, **mode)["match"] == "exact"
    typo = make_docx(tmp_path / "b.docx", "Uno.", "Al final, María cerro la puerta sin mirar.")
    exact = {"match_level": "exact"}  # the accent change would fold away at default
    assert run("anchor.resolve", doc=str(typo), anchor=anchor, **exact)["match"] == "fuzzy"
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(typo), anchor=anchor, mode="exact", **exact)
    assert err.value.code is ErrorCode.ANCHOR_NOT_FOUND
    assert err.value.details["mode"] == "exact"


def test_ambiguous_candidates_name_their_match_kind(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Dijo que sí, claro.", "Dijo que sí, claro.")
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor={"exact": "Dijo que sí, claro."}, mode="exact")
    assert err.value.code is ErrorCode.ANCHOR_AMBIGUOUS
    assert {c["match"] for c in err.value.details["candidates"]} == {"exact"}


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Uno.")
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor={"exact": "Uno."}, mode="loose")
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_verbatim_quote_with_changed_context_is_exact(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Hoy llovía. María cerró la puerta con cuidado. Nada más.")
    anchor = {
        "exact": "María cerró la puerta con cuidado",
        "prefix": "Al final de la larga noche, ",
        "suffix": " y se fue sin mirar atrás.",
        "hint": "p1",
    }
    found = run("anchor.resolve", doc=str(doc), anchor=anchor, mode="exact")
    assert found["match"] == "exact"
    assert found["at"].endswith(":12-45")


def test_short_verbatim_quote_is_exact_in_exact_mode(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Nada que ver. Dijo que sí. Fin.")
    anchor = {"exact": "Dijo que sí", "prefix": "Otro texto distinto, ", "suffix": " y mucho más"}
    assert run("anchor.resolve", doc=str(doc), anchor=anchor, mode="exact")["match"] == "exact"


def test_two_verbatim_occurrences_are_ambiguous_in_exact_mode(tmp_path: Path) -> None:
    doc = make_docx(
        tmp_path / "a.docx", "Uno. María cerró la puerta.", "Dos. María cerró la puerta."
    )
    anchor = {"exact": "María cerró la puerta", "prefix": "zzz ", "suffix": " zzz"}
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor=anchor, mode="exact")
    assert err.value.code is ErrorCode.ANCHOR_AMBIGUOUS
    assert len(err.value.details["candidates"]) == 2


def test_exact_mode_not_found_says_no_verbatim_occurrence(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Nada que ver.")
    with pytest.raises(DocControlError) as err:
        run("anchor.resolve", doc=str(doc), anchor={"exact": "María cerró"}, mode="exact")
    assert err.value.code is ErrorCode.ANCHOR_NOT_FOUND
    assert "verbatim" in err.value.message
