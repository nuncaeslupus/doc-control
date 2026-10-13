"""Find phrases across paragraph marks and in Word's Find syntax (T54).

The gate ``find_phrase_gaps == 0`` over every corpus kind and the 100k-word novel counts:
non-empty, non-repeated paragraphs left without a find phrase although a plain paragraph
mark is next to them, phrases that do not occur exactly once in the text as Word searches
it (paragraphs joined by marks, the last one ending in a mark too), and Word Find strings
that do not decode back to their phrase.
"""

from __future__ import annotations

from pathlib import Path

from doc_control.model.document import Document
from doc_control.ops.locate import (
    WORD_FIND_MAX,
    LazyIndexes,
    _indexes,
    _plain_marks,
    across_marks,
    word_find,
)
from tests.corpus.generator import KINDS, generate


def word_count(texts: list[str], phrase: str) -> int:
    """Occurrences (overlaps included) of ``phrase`` where every paragraph ends in a mark."""
    joined = "".join(t.casefold() + "\r" for t in texts)
    needle, count = phrase.casefold(), 0
    at = joined.find(needle)
    while at >= 0:
        count += 1
        at = joined.find(needle, at + 1)
    return count


_DECODE = (("^p", "\r"), ("^t", "\t"), ("^s", "\u00a0"), ("^~", "\u2011"), ("^-", "\u00ad"))


def unword(text: str) -> str:
    out, k = [], 0
    while k < len(text):
        if text[k] == "^":
            pair = text[k : k + 2]
            out.append("^" if pair == "^^" else dict(_DECODE)[pair])
            k += 2
        else:
            out.append(text[k])
            k += 1
    return "".join(out)


def test_find_phrase_gate(tmp_path: Path) -> None:
    gaps = checks = 0
    for kind in (*KINDS, "novel"):
        path = tmp_path / f"{kind}.docx"
        path.write_bytes(generate(kind, 1))
        doc = Document(str(path))
        texts = [doc.index.paragraph(n).text for n in range(1, len(doc.index) + 1)]
        marks = _plain_marks(doc)
        _, finds = _indexes(texts, marks)
        for i, found in enumerate(finds):
            checks += 1
            if found.text is None:
                beside = (i > 0 and marks[i - 1]) or (i + 1 < len(texts) and marks[i])
                gaps += found.reason not in ("empty", "duplicate") and beside
                continue
            gaps += word_count(texts, found.text) != 1
            word = word_find(found.text)
            gaps += word is not None and unword(word) != found.text
    print(f"find_phrase_gaps == {gaps} (of {checks} paragraphs)")
    assert gaps == 0


def test_word_find_codes_and_limit() -> None:
    assert word_find("a^b\r\tc\u00a0d\u2011e\u00adf") == "a^^b^p^tc^sd^~e^-f"
    assert word_find("x" * WORD_FIND_MAX) == "x" * WORD_FIND_MAX
    assert word_find("^" * 128) is None  # 256 characters once escaped


def test_a_mark_ends_the_last_paragraph_too() -> None:
    # "b^p" occurs twice in Word: after "b" and after the last paragraph "a b".
    texts = ["b", "a b"]
    for finds in (_indexes(texts)[1], [LazyIndexes(texts).find(i) for i in range(2)]):
        assert finds[0].text == "b\ra"
        assert word_count(texts, finds[0].text or "") == 1


def test_no_phrase_spans_a_mark_that_is_not_plain() -> None:
    texts = ["he went", "she went", "x"]

    def spans(j: int) -> list[tuple[int, int]]:
        return [(0, 2), (3, 7)] if j < 2 else [(0, 1)]

    found = across_marks(texts, spans, lambda key: True, 0)
    assert found is not None and found.across == "next"
    assert across_marks(texts, spans, lambda key: True, 0, lambda j: False) is None
