"""`text.find`: literal and regex search, folding with exact offsets, caps and cursors."""

from __future__ import annotations

import json
import re
import statistics
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex, build_index
from doc_control.model.search import Matcher, SearchOptions
from doc_control.ooxml.zipio import Package
from doc_control.ops import find as find_op
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import KINDS, generate, generate_parts
from tests.helpers import make_docx


def run(doc: Path, query: str, **options: Any) -> dict[str, Any]:
    request = {"doc": str(doc), "query": query, **options}
    reply = REGISTRY["text.find"].run(request)
    assert isinstance(reply, Reply)
    return {
        **reply.result,
        "doc": reply.doc,
        "warnings": reply.warnings,
        "next": reply.next_cursor,
    }


def spans(query: str, text: str, **options: Any) -> list[tuple[int, int]]:
    return list(Matcher(SearchOptions(query, **options)).spans(text))


def ats(result: dict[str, Any]) -> list[str]:
    return [m["at"].split(":")[1] for m in result["matches"]]


@pytest.fixture(autouse=True)
def _ops() -> None:
    load_operations()


def test_find_accent_insensitive_matches_unaccented_query(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Dijo María, y se fue.", "Maria sin tilde.")
    exact = run(doc, "maria", ignore_case=True)
    folded = run(doc, "maria", ignore_case=True, ignore_accents=True)
    assert [m["text"] for m in exact["matches"]] == ["Maria"]
    assert [m["text"] for m in folded["matches"]] == ["María", "Maria"]
    assert folded["matches"][0]["at"].endswith(":5-10")
    assert folded["matches"][0]["context"] == "Dijo María, y se fue."


def test_literal_is_exact_by_default(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "María maría MARÍA Maria")
    assert [m["text"] for m in run(doc, "María")["matches"]] == ["María"]
    assert len(run(doc, "maría", ignore_case=True)["matches"]) == 3
    assert len(run(doc, "maria", ignore_accents=True)["matches"]) == 1  # only lower-case


def test_offsets_map_back_through_decomposition() -> None:
    nfd = "Mari\u0301a vio a Mar\u0131\u0301a"  # a + combining acute, dotless i + acute
    nfc = "María y Año"
    # accent-insensitive query over decomposed text: the mark folds away but stays in the range
    assert spans("Maria", nfd, ignore_accents=True) == [(0, 6)]
    assert nfd[0:6] == "Mari\u0301a"
    assert spans("i", nfd, ignore_accents=True)[0] == (3, 5)  # i + its mark, never the mark apart
    # precomposed letters
    assert spans("maria", nfc, ignore_accents=True, ignore_case=True) == [(0, 5)]
    assert spans("ano", nfc, ignore_accents=True, ignore_case=True) == [(8, 11)]
    assert nfc[8:11] == "Año"
    # ñ is n + tilde: accent folding treats it as n, accent-sensitive does not
    assert spans("n", "ñ", ignore_accents=True) == [(0, 1)]
    assert spans("n", "ñ") == []
    assert spans("ñ", "an\u0303o", ignore_accents=False) == [(1, 3)]  # same letter under NFC
    assert spans("ñ", "an\u0303o", ignore_accents=True) == [(1, 3)]


def test_offsets_are_code_points_with_astral_characters() -> None:
    text = "😀 María 😀 maría"
    assert spans("maria", text, ignore_accents=True, ignore_case=True) == [(2, 7), (10, 15)]
    assert text[2:7] == "María"
    assert spans("😀", text) == [(0, 1), (8, 9)]
    assert spans("😀 maria", text, ignore_accents=True) == [(8, 15)]


def test_case_folding_that_changes_length_maps_back() -> None:
    assert spans("strasse", "Die Straße ist lang", ignore_case=True) == [(4, 10)]
    assert spans("s", "ß", ignore_case=True) == []  # half of an expansion is not a match
    assert spans("ss", "ß", ignore_case=True) == [(0, 1)]
    assert spans("fi", "ﬁn", ignore_case=True) == [(0, 1)]
    assert spans("f", "ﬁn", ignore_case=True) == []
    assert spans("i", "İstanbul", ignore_case=True, ignore_accents=True)[0] == (0, 1)


def test_folded_spans_always_slice_whole_characters() -> None:
    text = "ÁRBOL árbol Árbo\u0301l a\u0301\u0302rbol ß ǅ 😀á"
    for s, e in spans("arbol", text, ignore_case=True, ignore_accents=True):
        folded = unicodedata.normalize("NFD", text[s:e]).casefold()
        stripped = "".join(c for c in folded if not unicodedata.combining(c))
        assert stripped == "arbol"
    assert len(spans("arbol", text, ignore_case=True, ignore_accents=True)) == 4


def test_whole_word_literal_and_regex() -> None:
    assert spans("ana", "ana banana Ana, ana.", whole_word=True) == [(0, 3), (16, 19)]
    assert spans("ana", "Ana Anáis", whole_word=True, ignore_case=True) == [(0, 3)]
    # a combining mark is part of the word
    assert spans("ana", "ana\u0301 ana", whole_word=True) == [(5, 8)]
    assert spans("a\\w+", "ala asa x", regex=True, whole_word=True) == [(0, 3), (4, 7)]
    assert spans("año", "el año y años", whole_word=True) == [(3, 6)]


def test_regex_matches_and_skips_empty_matches() -> None:
    assert spans(r"\d+", "a 12 b 345", regex=True) == [(2, 4), (7, 10)]
    assert spans(r"x*", "abc", regex=True) == []
    assert spans("^ {8,}(\\S.*)$", "         texto", regex=True) == [(0, 14)]
    assert spans("MARIA", "María", regex=True, ignore_case=True, ignore_accents=True) == [(0, 5)]


def test_invalid_and_catastrophic_regex_are_refused() -> None:
    for pattern in ("(", "[a-", "(a+)+$", "(a*)*", "(?:a+b*)+", "(\\w+\\s*)*", "x(?:(a+)+)y"):
        with pytest.raises(DocControlError) as err:
            Matcher(SearchOptions(pattern, regex=True))
        assert err.value.code is ErrorCode.SCHEMA_INVALID
    for pattern in ("(a|b)+", "(?:[^.]+\\.)+", "\\w+(?:\\s\\w+)*", "(\\d{1,3}\\.)+", "a+b+"):
        Matcher(SearchOptions(pattern, regex=True))
    with pytest.raises(DocControlError):
        Matcher(SearchOptions("a" * 1001))
    with pytest.raises(DocControlError):
        Matcher(SearchOptions("a" * 1001, regex=True))


def test_refused_pattern_never_runs_long(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "a" * 60 + "!")
    start = time.perf_counter()
    with pytest.raises(DocControlError):
        run(doc, "(a+)+$", regex=True)
    assert time.perf_counter() - start < 2


def test_matches_are_resolvable_addresses(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "uno dos", "dos tres dos")
    result = run(doc, "dos")
    index = DocumentIndex.from_package(Package(doc))
    assert ats(result) == ["4-7", "0-3", "9-12"]
    for m in result["matches"]:
        resolved = index.resolve_range(m["at"])
        assert resolved.first.text[resolved.start_offset : resolved.end_offset] == m["text"]
    assert result["matches"][1]["at"].startswith("p2#")
    assert result["doc"]["paragraphs"] == 2


def test_limit_and_cursor_page_through_everything(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", *[f"a b a {i} a" for i in range(25)])
    everything = run(doc, "a", whole_word=True, limit=200)
    assert len(everything["matches"]) == 25 * 3 and everything["next"] is None
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        page = run(doc, "a", whole_word=True, limit=7, **extra)
        assert len(page["matches"]) <= 7
        pages += page["matches"]
        cursor = page["next"]
        if cursor is None:
            break
    assert pages == everything["matches"]


def test_default_limit_caps_output(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", *["x"] * 100)
    result = run(doc, "x")
    assert len(result["matches"]) == find_op.DEFAULT_LIMIT
    assert result["next"].startswith("c:find:")


def test_cursor_is_bound_to_query_and_document(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", *["x y"] * 10)
    cursor = run(doc, "x", limit=2)["next"]
    with pytest.raises(DocControlError) as err:
        run(doc, "y", cursor=cursor)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    other = make_docx(tmp_path / "b.docx", *["x y"] * 11)
    with pytest.raises(DocControlError):
        run(other, "x", cursor=cursor)
    with pytest.raises(DocControlError):
        run(doc, "x", cursor="c:find:zzzz")
    assert run(doc, "x", limit=2, cursor=cursor)["matches"][0]["at"].startswith("p3#")


def test_long_match_text_is_cut_and_context_trimmed(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "ab" + "x" * 500 + "cd")
    m = run(doc, "x+", regex=True, context=5)["matches"][0]
    assert m["at"].endswith(":2-502") and len(m["text"]) == find_op.MAX_TEXT
    assert m["text_truncated"] is True
    short = run(doc, "ab", context=3)["matches"][0]
    assert short["context"] == "ab" + "x" * 3 + "…"
    assert run(doc, "cd", context=0)["matches"][0]["context"] == "…cd"


def test_time_budget_returns_partial_results_with_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_docx(tmp_path / "a.docx", *["x"] * 50)
    monkeypatch.setattr(find_op, "TIME_BUDGET_SECONDS", -1.0)
    first = run(doc, "x", limit=200)
    assert len(first["matches"]) == 1 and first["warnings"][0].startswith("TIME_BUDGET")
    assert first["next"] is not None
    monkeypatch.setattr(find_op, "TIME_BUDGET_SECONDS", 10.0)
    rest = run(doc, "x", limit=200, cursor=first["next"])
    assert len(first["matches"]) + len(rest["matches"]) == 50


def test_find_is_registered_with_a_schema_and_reachable_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = make_docx(tmp_path / "a.docx", "Dijo María, y se fue.")
    assert "text.find" in REGISTRY and REGISTRY["text.find"].kind == "read"
    request = json.dumps(
        {"doc": str(doc), "query": "maria", "ignore_case": True, "ignore_accents": True}
    )
    code = main(["text.find", "--json", request])
    env = json.loads(capsys.readouterr().out)
    assert code == 0 and env["ok"] is True and env["op"] == "text.find"
    assert env["doc"]["paragraphs"] == 1 and len(env["doc"]["sha256"]) == 64
    assert env["result"]["matches"][0]["text"] == "María"
    assert env["next"] is None and env["warnings"] == []
    assert "doc" not in env["result"]

    code = main(["text.find", "--json", json.dumps({"doc": str(doc), "query": "x", "limit": 0})])
    assert code == 2 and json.loads(capsys.readouterr().out)["error"]["code"] == "SCHEMA_INVALID"
    code = main(
        ["text.find", "--json", json.dumps({"doc": str(tmp_path / "no.docx"), "query": "x"})]
    )
    assert code == 1 and json.loads(capsys.readouterr().out)["error"]["code"] == "DOC_NOT_FOUND"
    code = main(["server.help", "text.find"])
    assert code == 0
    assert "ignore_accents" in capsys.readouterr().out


def test_find_on_generated_corpus_matches_oracle(tmp_path: Path) -> None:
    """Every kind of generated document: matches equal a brute-force scan of the index."""
    checked = 0
    for kind in KINDS:
        data = generate(kind, 1)
        path = tmp_path / f"{kind}.docx"
        path.write_bytes(data)
        index = build_index(generate_parts(kind, 1)["word/document.xml"])
        words = {w for p in index.paragraphs for w in re.findall(r"\w{4,}", p.text)}
        for word in sorted(words)[:5]:
            expected = [
                (p.number, m.start(), m.end())
                for p in index.paragraphs
                for m in re.finditer(re.escape(word), p.text)
            ]
            got = run(path, word, limit=200)
            if len(expected) > 200:
                continue
            assert len(got["matches"]) == len(expected)
            for m, (number, s, e) in zip(got["matches"], expected, strict=True):
                assert m["at"].startswith(f"p{number}#") and m["at"].endswith(f":{s}-{e}")
                assert index.paragraph(number).text[s:e] == m["text"] == word
                checked += 1
    assert checked > 50


def _novel(tmp_path: Path) -> Path:
    path = tmp_path / "novel.docx"
    path.write_bytes(generate("novel", 0))
    return path


def test_find_on_novel_folds_accents_and_pages(tmp_path: Path) -> None:
    path = _novel(tmp_path)
    plain = run(path, "capitulo", ignore_case=True, ignore_accents=True, limit=200)
    assert plain["matches"] and all("ap" in m["text"].lower() for m in plain["matches"])
    assert run(path, "capitulo", ignore_case=True)["matches"] == []
    first = run(path, "el", whole_word=True, limit=50)
    assert len(first["matches"]) == 50 and first["next"]
    second = run(path, "el", whole_word=True, limit=50, cursor=first["next"])
    assert second["matches"][0]["at"] != first["matches"][0]["at"]


@pytest.mark.bench
def test_find_100k_words_p95_ms(tmp_path: Path) -> None:
    """Gate: p95 of a full find (open, index, search) over the 100k-word novel."""
    path = _novel(tmp_path)
    queries: list[dict[str, Any]] = [
        {"query": "el", "whole_word": True, "limit": 200},
        {"query": "maria", "ignore_case": True, "ignore_accents": True, "limit": 200},
        {"query": r"\bcap[ií]tulo \d+", "regex": True, "limit": 200},
        {"query": "zzzz-not-there", "ignore_case": True, "ignore_accents": True},
    ]
    times: list[float] = []
    for _ in range(6):
        for q in queries:
            start = time.perf_counter()
            run(
                path,
                q.pop("query") if False else q["query"],
                **{k: v for k, v in q.items() if k != "query"},
            )
            times.append((time.perf_counter() - start) * 1000)
    p95 = statistics.quantiles(times, n=20)[-1]
    print(f"find_100k_words_p95_ms={p95:.0f}")
    assert p95 <= 1000


def test_decomposed_text_and_query_match_composed_ones() -> None:
    nfd, nfc = "x Lucía y", "x Lucía y"
    assert spans("Lucía", nfd) == [(2, 8)]
    assert spans("Lucía", nfc) == [(2, 7)]
    assert spans("Lucía", nfd, whole_word=True) == [(2, 8)]
    assert spans("lucía", nfd, ignore_case=True) == [(2, 8)]
    assert spans("Luci", nfd) == []  # a letter does not match inside its accented form
