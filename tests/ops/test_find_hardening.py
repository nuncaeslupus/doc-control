"""`find` hardening: accent leaks, normalisation forms, expansions, slow regexes, cursors."""

from __future__ import annotations

import time
import unicodedata
from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ops import find as find_op
from tests.helpers import make_docx
from tests.ops.test_find import run, spans

ACUTE = "\N{COMBINING ACUTE ACCENT}"
TILDE = "\N{COMBINING TILDE}"
CIRC = "\N{COMBINING CIRCUMFLEX ACCENT}"
E_ACUTE = "\N{LATIN SMALL LETTER E WITH ACUTE}"


def test_accents_never_leak_when_only_case_is_folded() -> None:
    assert spans("e", "caf" + E_ACUTE, ignore_case=True) == []
    assert spans("e", "cafe" + ACUTE, ignore_case=True) == []
    assert spans("E", "CAF" + E_ACUTE.upper(), ignore_case=True) == []
    assert spans("cafe", "cafe" + ACUTE) == []
    assert spans("e", "cafe" + ACUTE) == []
    assert spans("e", "e" + ACUTE + " e") == [(3, 4)]


def test_precomposed_and_decomposed_forms_match_each_other() -> None:
    assert spans("caf" + E_ACUTE, "cafe" + ACUTE + "!") == [(0, 5)]
    assert spans("cafe" + ACUTE, "caf" + E_ACUTE + "!") == [(0, 4)]
    assert spans(E_ACUTE, "ae" + ACUTE + "b") == [(1, 3)]
    assert spans(E_ACUTE.upper(), "ae" + ACUTE + "b", ignore_case=True) == [(1, 3)]
    assert spans("e", "e" + ACUTE + " e", whole_word=True) == [(3, 4)]
    assert spans("n", "an" + TILDE + "o") == []


def test_matches_never_start_on_a_mark_or_split_a_cluster() -> None:
    assert spans(ACUTE, "e" + ACUTE) == []
    assert spans("e" + ACUTE, "xe" + ACUTE + CIRC) == []
    for text in ("a" + ACUTE + CIRC + "b", ACUTE + "a", "xe" + ACUTE + "e" + ACUTE):
        for opts in ({}, {"ignore_case": True}, {"ignore_accents": True}):
            for q in ("a", "e", "b", "xe"):
                for s, e in spans(q, text, **opts):
                    assert s == 0 or not unicodedata.combining(text[s])
                    assert e == len(text) or not unicodedata.combining(text[e])


@pytest.mark.parametrize("pattern", ["(a|a)*b", "(a+a+)+b", "(.*a){12}x"])
def test_slow_regex_is_killed_at_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str
) -> None:
    doc = make_docx(tmp_path / "a.docx", "a" * 40)
    monkeypatch.setattr(find_op, "TIME_BUDGET_SECONDS", 0.5)
    start = time.perf_counter()
    with pytest.raises(DocControlError) as err:
        run(doc, pattern, regex=True)
    assert time.perf_counter() - start < 8
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert "p1" in err.value.message


def test_slow_regex_after_progress_returns_partial_page_and_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_docx(tmp_path / "a.docx", "aab", "nothing", "a" * 40, "ab")
    monkeypatch.setattr(find_op, "TIME_BUDGET_SECONDS", 4.0)
    start = time.perf_counter()
    page = run(doc, "(a|a)*b", regex=True)
    assert time.perf_counter() - start < 12
    assert [m["text"] for m in page["matches"]] == ["aab"]
    assert page["warnings"][0].startswith("TIME_BUDGET") and "p3" in page["warnings"][0]
    assert page["next"] is not None and page["next"].split(":")[3] == "3"
    with pytest.raises(DocControlError):  # the stuck paragraph is reported, not retried forever
        run(doc, "(a|a)*b", regex=True, cursor=page["next"])


def test_regex_scan_finds_the_same_matches_as_in_process_search(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", *[f"uno {i} dos {i * 7}" for i in range(30)])
    regex = run(doc, r"\d+", regex=True, limit=200)
    assert len(regex["matches"]) == 60
    assert regex["matches"][0]["at"].endswith(":4-5")
    page = run(doc, r"\d+", regex=True, limit=10)
    rest = run(doc, r"\d+", regex=True, limit=200, cursor=page["next"])
    assert page["matches"] + rest["matches"] == regex["matches"]


def test_cursor_past_the_last_paragraph_is_refused(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "x", "x")
    token = run(doc, "x", limit=1)["next"].split(":")[2]
    with pytest.raises(DocControlError) as err:
        run(doc, "x", cursor=f"c:find:{token}:3:0")
    assert err.value.code is ErrorCode.SCHEMA_INVALID
