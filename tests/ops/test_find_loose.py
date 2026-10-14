"""`find` with `match_level` (FIND T7): folded literal search, each hit tagged.

The gate ``symbol_fold_violations == 0`` counts symbol rows that fold where they must not
(``5$`` meeting ``5`` at ``default``) or do not fold where they must (at ``loose``), and
hits whose tag is missing or wrong. ``regression_failures == 0`` counts find replies, cursor
and ``query_word`` included, that differ from the golden file taken before ``match_level``
existed. Regenerate it on purpose with ``uv run python -m tests.ops.test_find_loose``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.registry import REGISTRY, load_operations
from tests.conftest import write_corpus_doc
from tests.helpers import CONTENT_TYPES, RELS, document_xml

GOLDEN = Path(__file__).parent / "data" / "find_golden.json"

SPANISH = (
    "-¿Estás ahí? -dijo María.",
    "--Estas ahi, Maria, ¿verdad?",
    "Cuesta 5$ el kilo y 5 la docena.",
    "Precio: 5 euros; el total, 5$ + 3.",
    "Línea\tcon tabulador y a^pb literal.",
)

#: Golden cases: (document, request without ``doc``), each run as a first page and, when it
#: has a ``next``, as the page that cursor names.
CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("plain", {"query": "the"}),
    ("plain", {"query": "the", "limit": 3}),
    ("plain", {"query": "THE", "ignore_case": True, "limit": 2}),
    ("plain", {"query": "the", "whole_word": True, "limit": 4}),
    ("plain", {"query": r"\bq\w+", "regex": True, "limit": 3}),
    ("kitchen_sink", {"query": "board", "limit": 5}),
    ("kitchen_sink", {"query": "Board", "ignore_case": True, "whole_word": True}),
    ("spanish", {"query": "Estas ahi"}),
    ("spanish", {"query": "estas ahi", "ignore_case": True, "ignore_accents": True}),
    ("spanish", {"query": "5$"}),
    ("spanish", {"query": "a^pb"}),
    ("spanish", {"query": "^t"}),
    ("spanish", {"query": "maria", "ignore_case": True, "ignore_accents": True, "limit": 1}),
)


@pytest.fixture(autouse=True)
def _ops() -> None:
    load_operations()


def spanish_docx(path: Path) -> Path:
    """A docx of ``SPANISH`` with fixed zip dates, so its bytes (and cursors) never change."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in (
            ("[Content_Types].xml", CONTENT_TYPES),
            ("_rels/.rels", RELS),
            ("word/document.xml", document_xml(*SPANISH)),
        ):
            zf.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    return path


def run(doc: Path, query: str, **options: Any) -> dict[str, Any]:
    reply = REGISTRY["find"].run({"doc": str(doc), "query": query, **options})
    assert isinstance(reply, Reply)
    return {**reply.result, "next": reply.next_cursor, "warnings": reply.warnings}


def hits(result: dict[str, Any]) -> list[tuple[str, str]]:
    return [(m["text"], m["match_level"]) for m in result["matches"]]


def test_default_level_keeps_symbols(tmp_path: Path) -> None:
    doc = spanish_docx(tmp_path / "s.docx")
    violations = 0
    default = run(doc, "5$", match_level="default")
    # `$` is a symbol: kept at `default`, so the bare `5`s are not hits.
    violations += hits(default) != [("5$", "exact"), ("5$", "exact")]
    loose = run(doc, "5$", match_level="loose")
    # Dropped at `loose`: every `5` is a hit, the bare ones tagged `loose`.
    violations += hits(loose) != [
        ("5$", "exact"),
        ("5", "loose"),
        ("5", "loose"),
        ("5$", "exact"),
    ]
    violations += sum("match_level" not in m for m in default["matches"] + loose["matches"])
    assert violations == 0  # symbol_fold_violations == 0
    assert default["query_word"] is None and loose["query_word"] is None


def test_default_level_folds_accents_case_and_punctuation(tmp_path: Path) -> None:
    doc = spanish_docx(tmp_path / "s.docx")
    found = run(doc, "Estas ahi", match_level="default")
    assert hits(found) == [("Estás ahí", "default"), ("Estas ahi", "exact")]
    assert found["matches"][0]["at"].endswith(":2-11")
    # Punctuation the phrase opens with widens the span over the text's own punctuation.
    assert hits(run(doc, "¿estas ahi", match_level="default"))[0] == ("-¿Estás ahí", "default")
    assert hits(run(doc, "MARIA", match_level="default")) == [
        ("María", "default"),
        ("Maria", "case"),
    ]


def test_match_level_exact_tags_hits_and_keeps_flags(tmp_path: Path) -> None:
    doc = spanish_docx(tmp_path / "s.docx")
    assert hits(run(doc, "Maria", match_level="exact")) == [("Maria", "exact")]
    folded = run(doc, "maria", match_level="exact", ignore_case=True, ignore_accents=True)
    assert hits(folded) == [("María", "default"), ("Maria", "case")]
    assert folded["query_word"] is None  # ignore_accents, as today
    assert run(doc, "Maria", match_level="exact")["query_word"] == "Maria"


def test_whole_word_checks_the_original_text(tmp_path: Path) -> None:
    doc = spanish_docx(tmp_path / "s.docx")
    assert hits(run(doc, "ahi", match_level="default", whole_word=True)) == [
        ("ahí", "default"),
        ("ahi", "exact"),
    ]
    assert run(doc, "ah", match_level="default", whole_word=True)["matches"] == []
    assert run(doc, "ah", match_level="default")["total"] == 2


def test_word_codes_translated_only_with_match_level(tmp_path: Path) -> None:
    doc = spanish_docx(tmp_path / "s.docx")
    assert run(doc, "a^pb")["total"] == 1  # plain find: the caret is literal
    assert run(doc, "a^pb", match_level="exact")["total"] == 0  # ^p is a paragraph mark
    assert hits(run(doc, "a^^pb", match_level="exact")) == [("a^pb", "exact")]
    assert hits(run(doc, "Línea^tcon", match_level="exact")) == [("Línea\tcon", "exact")]


def test_cursor_carries_match_level(tmp_path: Path) -> None:
    doc = spanish_docx(tmp_path / "s.docx")
    first = run(doc, "5", match_level="loose", limit=2)
    assert first["total"] == 4 and first["next"] is not None
    assert first["next"] != run(doc, "5", limit=2)["next"]
    rest = run(doc, "5", match_level="loose", limit=2, cursor=first["next"])
    assert len(rest["matches"]) == 2 and rest["next"] is None
    with pytest.raises(DocControlError) as err:
        run(doc, "5", limit=2, cursor=first["next"])
    assert err.value.details["path"] == "$.cursor"


@pytest.mark.parametrize(
    "options",
    [
        {"regex": True, "match_level": "default"},
        {"regex": True, "match_level": "loose"},
    ],
)
def test_regex_with_match_level_refused(tmp_path: Path, options: dict[str, Any]) -> None:
    doc = spanish_docx(tmp_path / "s.docx")
    with pytest.raises(DocControlError) as err:
        run(doc, "5", **options)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert err.value.details["path"] == "$.match_level"
    assert run(doc, "5", regex=True, match_level="exact")["total"] == 4


@pytest.mark.parametrize(
    ("level", "flag"),
    [("default", "ignore_case"), ("default", "ignore_accents"), ("loose", "ignore_case")],
)
def test_contradicting_flags_refused(tmp_path: Path, level: str, flag: str) -> None:
    doc = spanish_docx(tmp_path / "s.docx")
    with pytest.raises(DocControlError) as err:
        run(doc, "Maria", match_level=level, **{flag: False})
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert err.value.details["path"] == f"$.{flag}"
    assert run(doc, "Maria", match_level=level, **{flag: True})["total"] == 2


def test_phrase_empty_after_folding_refused(tmp_path: Path) -> None:
    doc = spanish_docx(tmp_path / "s.docx")
    with pytest.raises(DocControlError) as err:
        run(doc, "--", match_level="default")
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert run(doc, "--", match_level="exact")["total"] == 1


def snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        docs = {
            "plain": write_corpus_doc(Path(tmp), "plain", 1),
            "kitchen_sink": write_corpus_doc(Path(tmp), "kitchen_sink", 1),
            "spanish": spanish_docx(Path(tmp) / "spanish.docx"),
        }
        for n, (kind, request) in enumerate(CASES):
            options = dict(request)
            query = options.pop("query")
            pages = [run(docs[kind], query, **options)]
            if pages[0]["next"] is not None:
                pages.append(run(docs[kind], query, **options, cursor=pages[0]["next"]))
            out[f"{n}:{kind}:{json.dumps(request, ensure_ascii=False)}"] = pages
    return out


def test_find_without_match_level_unchanged() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    current = snapshot()
    failures = [case for case in golden if current.get(case) != golden[case]]
    assert sorted(golden) == sorted(current)
    assert failures == []  # regression_failures == 0


if __name__ == "__main__":
    load_operations()
    GOLDEN.write_text(
        json.dumps(snapshot(), ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sys.exit(0)
