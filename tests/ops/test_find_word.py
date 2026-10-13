"""``find`` gives the query as typed in Word's Find box, and repeated paragraphs get a find
phrase across a mark (T58).

The gate ``find_word_mismatches == 0``: for queries holding every character Word's Find box
writes as a code, ``query_word`` decodes back to the query; a regex
gets no ``query_word``; and the find-phrase gate (repeated paragraphs included) has no gap.
"""

from __future__ import annotations

from pathlib import Path

from doc_control import api
from tests.helpers import make_docx
from tests.ops import test_locate_across as across

QUERIES = [
    "a^b",
    "x\ty",
    "line\nbreak",
    "no\u00a0break",
    "plain",
]
TWO_FORMS = ["non\u2011breaking", "soft\u00adhyphen"]  # no Find string matches both forms


def test_find_word_gate(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", *(f"Before {q} after." for q in QUERIES if "\t" not in q))
    mismatches = checks = 0
    with api.open(path) as doc:
        for query in QUERIES:
            result = doc.run("find", {"query": query})
            checks += 1
            word = result.get("query_word")
            mismatches += word is None or across.unword(word) != query
        for query in TWO_FORMS:
            checks += 1
            mismatches += doc.run("find", {"query": query})["query_word"] is not None
        checks += 1
        mismatches += "query_word" in doc.run("find", {"query": "a.b", "regex": True})
        options = doc.run("find", {"query": "plain", "ignore_case": True, "whole_word": True})
        checks += 1
        mismatches += options["query_word_options"] != {"match_case": False, "whole_words": True}
        checks += 1
        accents = doc.run("find", {"query": "plain", "ignore_accents": True})
        mismatches += accents["query_word"] is not None  # Word's Find cannot ignore accents
        long = doc.run("find", {"query": "^" * 200})
        checks += 1
        mismatches += long["query_word"] is not None  # 400 characters: past the box
    (tmp_path / "phrases").mkdir()
    across.test_find_phrase_gate(tmp_path / "phrases")
    print(f"find_word_mismatches == {mismatches} (of {checks} checks; find-phrase gate passed)")
    assert mismatches == 0


def test_many_repeated_paragraphs_stay_fast() -> None:
    import random
    import time

    from doc_control.ops.locate import _indexes

    rng = random.Random(7)
    text = [" ".join(f"w{rng.randint(0, 20000)}" for _ in range(15)) for _ in range(3000)]
    text += [" ".join(f"z{k}" for k in range(30))] * 1500 + ["\u2014S\u00ed."] * 1500
    start = time.perf_counter()
    _indexes(text)
    assert time.perf_counter() - start < 5.0  # ~0.5 s here; was minutes before MarkIndex
