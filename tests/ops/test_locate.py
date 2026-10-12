"""`locate`: chapter, scene, estimated page and the shortest unique first-and-last-words phrase."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.model.document import Document
from doc_control.ops.locate import SEPARATOR, FindIndex, LocatorIndex
from doc_control.ops.paging import RESPONSE_BUDGET
from tests.corpus.generator import KINDS
from tests.helpers import make_docx

Run = Callable[..., tuple[int, dict[str, Any], int]]
CorpusWriter = Callable[..., Path]


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    def call(op: str, **request: Any) -> tuple[int, dict[str, Any], int]:
        code = main([op, "--json", json.dumps(request)])
        out = capsys.readouterr().out
        return code, json.loads(out), len(out.rstrip("\n").encode("utf-8"))

    return call


def fold(word: str) -> str:
    """Word's Find folding: case-insensitive, accent-sensitive, characters untouched."""
    return word.casefold()


def folded(texts: list[str]) -> list[list[str]]:
    return [[fold(w) for w in t.split()] for t in texts]


def occurrences(docs: list[list[str]], row: dict[str, Any]) -> int:
    """Paragraphs matching the row's phrase, counted independently of the implementation."""
    phrase: str = row["phrase"]
    if row.get("complete"):
        want = [fold(w) for w in phrase.split()]
        return sum(1 for words in docs if words == want)
    head, tail = phrase.split(SEPARATOR)
    first = [fold(w) for w in head.split()]
    last = [fold(w) for w in tail.split()]
    need = len(first) + len(last)
    return sum(
        1
        for words in docs
        if len(words) >= need and words[: len(first)] == first and words[-len(last) :] == last
    )


def find_count(texts: list[str], phrase: str) -> int:
    """Occurrences (overlaps included) of ``phrase`` in the paragraphs joined by paragraph marks."""
    joined = "\r".join(t.casefold() for t in texts)
    needle = phrase.casefold()
    count = 0
    at = joined.find(needle)
    while at >= 0:
        count += 1
        at = joined.find(needle, at + 1)
    return count


def find_failures(texts: list[str], phrases: list[str | None]) -> int:
    return sum(1 for p in phrases if p is not None and find_count(texts, p) != 1)


def all_rows(run: Run, path: Path, **request: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        code, env, nbytes = run("locate", doc=str(path), **request, **extra)
        assert code == 0, env
        assert nbytes <= RESPONSE_BUDGET
        assert env["result"]["estimated"] is True
        rows += env["result"]["paragraphs"]
        cursor = env["next"]
        if cursor is None:
            return rows


def uniqueness_failures(path: Path, rows: list[dict[str, Any]]) -> int:
    docs = folded([p.text for p in Document(str(path)).index.paragraphs])
    return sum(1 for r in rows if r["phrase"] is not None and occurrences(docs, r) != 1)


def test_locator_phrase_is_unique_in_document(
    corpus_doc: CorpusWriter, run: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every phrase `locate` returns matches exactly one paragraph (every corpus kind)."""
    paths = [corpus_doc(kind, 1) for kind in KINDS if kind != "novel"]
    not_unique = 0
    total = 0
    find_bad = 0
    find_total = 0
    for path in paths:
        rows = all_rows(run, path)
        texts = [p.text for p in Document(str(path)).index.paragraphs]
        found = [r["find_phrase"] for r in rows]
        find_total += sum(1 for f in found if f is not None)
        find_bad += find_failures(texts, found)
        assert all(r["find_phrase_unique"] == (r["find_phrase"] is not None) for r in rows)
        total += sum(1 for r in rows if r["phrase"] is not None)
        not_unique += uniqueness_failures(path, rows)
    # The 100,000-word novel again, straight through the index (no paging round trips).
    novel = [p.text for p in Document(str(corpus_doc("novel", 3))).index.paragraphs]
    docs = folded(novel)
    index = LocatorIndex(novel)
    for i in range(len(novel)):
        loc = index.locator(i)
        if loc.phrase is None:
            continue
        row: dict[str, Any] = {"phrase": loc.phrase, "complete": loc.complete}
        total += 1
        not_unique += occurrences(docs, row) != 1
    started = time.perf_counter()
    finds = FindIndex(index).found
    elapsed = time.perf_counter() - started
    found = [f.text for f in finds]
    find_total += sum(1 for f in found if f is not None)
    find_bad += find_failures(novel, found)
    with capsys.disabled():
        print(f"\nlocators_not_unique={not_unique} (of {total} phrases)")
        print(f"find_phrases_not_unique={find_bad} (of {find_total} phrases)")
        print(f"find phrases of a {len(novel)}-paragraph novel: {elapsed:.2f}s")
    assert total > 1000
    assert find_total > 1000
    assert not_unique == 0
    assert find_bad == 0


def test_shortest_phrase_and_fields(run: Run, tmp_path: Path) -> None:
    path = make_docx(
        tmp_path / "d.docx",
        "Prologue text before any chapter appears here",
        "Capítulo 1",
        "Alpha beta gamma delta",
        "Alpha beta gamma omega",
        "* * *",
        "Alpha zeta gamma delta",
        "Capítulo 2",
        "Lone",
    )
    rows = all_rows(run, path)
    by_text = {r["at"].split("#")[0]: r for r in rows}
    third, fourth, sixth = by_text["p3"], by_text["p4"], by_text["p6"]
    assert third["phrase"] == f"Alpha{SEPARATOR}delta" or third["phrase"].startswith("Alpha beta")
    # p3 and p6 share both ends, p4 differs in the last word: 1 + 1 words are enough for p4.
    assert fourth["phrase"] == f"Alpha{SEPARATOR}omega"
    assert (fourth["head_words"], fourth["tail_words"]) == (1, 1)
    assert third["phrase"] == f"Alpha beta{SEPARATOR}delta"
    assert sixth["phrase"] == f"Alpha zeta{SEPARATOR}delta"
    assert third["chapter"]["id"] == fourth["chapter"]["id"] == sixth["chapter"]["id"]
    assert third["chapter"]["title"] == "Capítulo 1"
    assert third["chapter"]["number"] == 1
    assert third["scene"]["ordinal"] == 1
    assert sixth["scene"]["ordinal"] == 2
    assert sixth["scene"]["id"] != third["scene"]["id"]
    assert by_text["p1"]["chapter"] is None
    assert third["page"] == 1
    assert third["page_label"] == 1
    assert by_text["p8"]["chapter"]["number"] == 2
    assert by_text["p8"]["phrase"] == "Lone"
    assert by_text["p8"]["complete"] is True


def test_short_empty_and_duplicate_paragraphs(run: Run, tmp_path: Path) -> None:
    path = make_docx(
        tmp_path / "d.docx", "Hello", "Hello", "", "Same words here", "same Words HERE"
    )
    rows = all_rows(run, path)
    assert len(rows) == 4  # the empty paragraph is left out of a whole-document list
    assert [r["phrase"] for r in rows] == [None] * 4
    assert [r["reason"] for r in rows] == ["duplicate"] * 4  # folded: case-insensitive
    code, env, _ = run("locate", doc=str(path), at=str(rows[0]["at"]))
    assert code == 0
    two = Document(str(path)).index.paragraph(3)
    code, env, _ = run("locate", doc=str(path), at=str(two.address))
    assert env["result"]["paragraphs"][0]["reason"] == "empty"
    assert env["result"]["paragraphs"][0]["phrase"] is None


def test_prefix_paragraph_does_not_match_longer_one(run: Run, tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "one two", "one two three two")
    rows = all_rows(run, path)
    assert rows[0]["phrase"] == "one two"
    assert rows[0]["complete"] is True
    assert occurrences(folded(["one two", "one two three two"]), rows[0]) == 1
    assert (
        rows[1]["phrase"] == f"one two{SEPARATOR}two" or rows[1]["phrase"] == f"one{SEPARATOR}two"
    )
    assert uniqueness_failures(path, rows) == 0


def test_long_paragraphs_sharing_both_ends_report_a_reason(run: Run, tmp_path: Path) -> None:
    ends = " ".join(f"w{i}" for i in range(12))
    a = f"{ends} a middle {ends}"
    b = f"{ends} another middle part {ends}"
    path = make_docx(tmp_path / "d.docx", a, b, a.replace("a middle", "x") + " tail")
    rows = all_rows(run, path)
    assert rows[0]["phrase"] is None
    assert rows[0]["reason"] == "not_unique_within_limit"
    assert rows[1]["reason"] == "not_unique_within_limit"
    assert rows[2]["phrase"] is not None


def test_range_and_pages(run: Run, corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("novel", 4)
    paras = Document(str(path)).index.paragraphs
    first, last = paras[100], paras[104]
    code, env, _ = run("locate", doc=str(path), at=f"{first.address}:0..{last.address}:1")
    assert code == 0, env
    rows = env["result"]["paragraphs"]
    assert [r["at"] for r in rows] == [str(p.address) for p in paras[100:105]]
    assert env["result"]["estimated"] is True
    assert all(isinstance(r["page"], int) and r["page"] >= 1 for r in rows)
    code, big, _ = run("locate", doc=str(path), limit=5)
    assert big["result"]["total"] > 1000
    assert len(big["result"]["paragraphs"]) == 5
    assert big["next"] is not None
    code, other, _ = run("locate", doc=str(path), cursor=big["next"], limit=5)
    assert other["result"]["paragraphs"][0]["at"] != big["result"]["paragraphs"][0]["at"]


def test_chapters_agree_with_segment(run: Run, corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("novel", 5)
    code, env, _ = run("segment", doc=str(path), unit="chapters", limit=3)
    assert code == 0
    chapters = env["result"]["chapters"]
    code, env, _ = run("locate", doc=str(path), at=chapters[1]["at"])
    row = env["result"]["paragraphs"][0]
    assert row["chapter"]["id"] == chapters[1]["id"]
    assert row["chapter"]["number"] == chapters[1]["number"]


def test_stale_address_is_refused(run: Run, tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "one", "two")
    code, env, _ = run("locate", doc=str(path), at="p1#00000000")
    assert code != 0
    assert env["ok"] is False


def test_whole_novel_is_fast(
    run: Run, corpus_doc: CorpusWriter, capsys: pytest.CaptureFixture[str]
) -> None:
    """One response of a 100,000-word novel, and the index over all of it, stay quick."""
    path = corpus_doc("novel", 6)
    started = time.perf_counter()
    code, _, _ = run("locate", doc=str(path))
    first_page = time.perf_counter() - started
    assert code == 0
    texts = [p.text for p in Document(str(path)).index.paragraphs]
    started = time.perf_counter()
    index = LocatorIndex(texts)
    for i in range(len(texts)):
        index.locator(i)
    full = time.perf_counter() - started
    with capsys.disabled():
        print(
            f"\nlocate novel: first response {first_page:.2f}s, "
            f"all {len(texts)} locators {full:.2f}s"
        )
    assert first_page < 20
    assert full < 20


def test_find_phrase_keeps_document_characters_and_case_rules(run: Run, tmp_path: Path) -> None:
    path = make_docx(
        tmp_path / "d.docx",
        "caf\u00e9 noir",
        "cafe noir",
        "Hello there",
        "hello there",
        "a\u00a0b c",
        "a b c",
    )
    rows = all_rows(run, path)
    by = {r["at"].split("#")[0]: r for r in rows}
    assert by["p1"]["find_phrase"] == "caf\u00e9"  # accent-sensitive
    assert by["p2"]["find_phrase"] == "cafe"
    assert by["p3"]["find_phrase"] is None  # case-insensitive twins
    assert by["p3"]["find_phrase_reason"] == "duplicate"
    assert by["p5"]["find_phrase"] == "a\u00a0b"  # no-break space stays
    assert by["p6"]["find_phrase"] == "a b"
    assert by["p5"]["reason"] == "duplicate"  # same words; the find phrase tells them apart


def test_find_phrase_never_crosses_a_line_break(run: Run, tmp_path: Path) -> None:
    # "bb cc" (across the break) is the only unique window of paragraph 1: not allowed.
    path = make_docx(
        tmp_path / "d.docx", "aa bb</w:t><w:br/><w:t>cc dd", "aa bb zz", "yy cc dd", "bb ee"
    )
    rows = all_rows(run, path)
    texts = [p.text for p in Document(str(path)).index.paragraphs]
    assert texts[0] == "aa bb\ncc dd"
    assert rows[0]["find_phrase"] is None
    assert rows[0]["find_phrase_reason"] == "none_within_25_words"
    assert find_failures(texts, [r["find_phrase"] for r in rows]) == 0


def test_find_phrase_counts_overlaps_and_substrings(run: Run, tmp_path: Path) -> None:
    # "he went" is inside "she went": counting whole words only would call it unique.
    path = make_docx(tmp_path / "d.docx", "he went", "she went", "ha ha ha")
    rows = all_rows(run, path)
    assert rows[0]["find_phrase"] is None
    assert rows[1]["find_phrase"] == "she"
    texts = [p.text for p in Document(str(path)).index.paragraphs]
    assert find_failures(texts, [r["find_phrase"] for r in rows]) == 0
    assert rows[2]["find_phrase"] == "ha ha ha"  # "ha ha" occurs twice, overlapping


def test_find_phrase_limit_and_context_for_duplicates(run: Run, tmp_path: Path) -> None:
    long = " ".join(f"w{i}" for i in range(30))
    path = make_docx(
        tmp_path / "d.docx",
        "A unique opening paragraph",
        "\u2014S\u00ed.",
        "\u2014S\u00ed.",
        long,
        long + " extra",
    )
    rows = all_rows(run, path)
    assert rows[1]["find_phrase"] is None
    assert rows[1]["find_phrase_unique"] is False
    assert rows[1]["context"] == {
        "after": rows[0]["find_phrase"],
        "at": rows[0]["at"],
        "paragraphs_after": 1,
    }
    assert rows[2]["context"]["paragraphs_after"] == 2
    assert rows[3]["find_phrase"] is None
    assert rows[3]["find_phrase_reason"] == "none_within_25_words"
    assert rows[3]["context"]["paragraphs_after"] == 3
    assert rows[4]["find_phrase"] is not None
    assert "context" not in rows[0]
