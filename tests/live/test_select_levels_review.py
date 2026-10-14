"""``live.select`` match levels, review findings of FIND T5.

Requested ``exact`` is today's rule, caret and all; the ``case`` rung is today's casefold
rule before the ``case`` fold; an ambiguity always lists two candidates or more; a folded
lookup on the novel stays fast and caches no document-sized text.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import using
from doc_control.live.protocol import FakeAdapter
from doc_control.model import loosefold
from doc_control.model.address import Address, fingerprint
from doc_control.ops.live import _found
from tests.corpus.generator import generate
from tests.helpers import make_docx


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    return make_docx(tmp_path / "novel.docx", "One.")


def _select(doc: Path, texts: list[str], **ask: Any) -> dict[str, Any]:
    with using(FakeAdapter({str(doc): texts})):
        api.run("live.open", {"doc": str(doc)})
        result: dict[str, Any] = api.run(
            "live.select", {"doc": str(doc), "front": False, **ask}
        ).result
        return result


def _refused(doc: Path, texts: list[str], **ask: Any) -> DocControlError:
    with pytest.raises(DocControlError) as err:
        _select(doc, texts, **ask)
    return err.value


def _at(texts: list[str], n: int) -> str:
    return str(Address(n + 1, fingerprint(texts[n])))


def test_exact_level_keeps_todays_caret_rule(doc: Path) -> None:
    texts = ["a", "b"]
    err = _refused(
        doc, texts, find_phrase="a^pb", find_phrase_across="previous", match_level="exact"
    )
    assert err.code is ErrorCode.SCHEMA_INVALID
    # Today's rule searches the caret literally.
    err = _refused(doc, texts, find_phrase="a^pb", match_level="exact")
    assert err.code is ErrorCode.ANCHOR_NOT_FOUND
    assert _select(doc, ["x^pb", "b"], find_phrase="X^P", match_level="exact")["at"] == _at(
        ["x^pb", "b"], 0
    )
    # The default level translates the code, so the same request is valid there.
    got = _select(doc, texts, find_phrase="a^pb", find_phrase_across="previous")
    assert got["at"] == _at(texts, 1)


def test_exact_level_reports_case_and_maps_folded_lengths(doc: Path) -> None:
    texts = ["Die Straße ist lang.", "Die Gasse."]
    got = _select(doc, texts, find_phrase="STRASSE IST", match_level="exact")
    assert (got["at"], got["match_level"], got["matched_text"]) == (
        _at(texts, 0),
        "case",
        "Straße ist",
    )
    got = _select(doc, texts, find_phrase="Gasse", match_level="exact")
    assert (got["at"], got["match_level"]) == (_at(texts, 1), "exact")


def test_case_rung_is_todays_casefold_first(doc: Path) -> None:
    texts = ["Hola amigo.", "Hola  amigo."]
    got = _select(doc, texts, find_phrase="hola amigo")
    assert (got["at"], got["match_level"]) == (_at(texts, 0), "case")
    texts = ["Hola\u00a0ami\u00adgo.", "Hola amigo."]
    got = _select(doc, texts, find_phrase="HOLA AMIGO.")
    assert (got["at"], got["match_level"]) == (_at(texts, 1), "case")
    # The case fold still runs when plain casefold finds nothing.
    got = _select(doc, ["Lead.", "Hola  amigo."], find_phrase="HOLA AMIGO")
    assert (got["match_level"], got["matched_text"]) == ("case", "Hola  amigo")


def test_ambiguity_lists_two_candidates_or_more(doc: Path) -> None:
    # Three hits of a mark; the last names no paragraph, so two are left.
    err = _refused(doc, ["", ""], find_phrase="\rx")
    assert err.code is ErrorCode.ANCHOR_NOT_FOUND
    with pytest.raises(DocControlError) as caught:
        _found(["", "", ""], "\r", None)
    assert caught.value.code is ErrorCode.ANCHOR_AMBIGUOUS
    assert len(caught.value.details["candidates"]) == 2
    # One hit naming a paragraph and one past the end: the one is the answer.
    assert _found(["", ""], "\r", None)[0] == 1


def test_folded_lookup_on_the_novel_is_fast_and_uncached(tmp_path: Path) -> None:
    path = tmp_path / "novel.docx"
    path.write_bytes(generate("novel", 1))
    with api.open(path) as handle:
        texts = [p["text"] for p in handle.paragraphs()]
    phrase = " ".join(texts[700].split()[:4]).upper() + " UNICORNIO"
    took = []
    for n in range(10):
        version = list(texts)
        version[n] += f" edit {n}"  # a fresh version of the document
        start = time.perf_counter()
        with pytest.raises(DocControlError) as err:
            _found(version, phrase, None)
        took.append(time.perf_counter() - start)
        assert err.value.code is ErrorCode.ANCHOR_NOT_FOUND
    for cache in (loosefold._fold_cached, loosefold._fold_text_cached):
        assert cache.cache_info().currsize <= 4 * 3 * len(texts)
    # Generous for CI: a not-found walked every level in about a second before.
    assert sorted(took)[len(took) // 2] < 0.25
