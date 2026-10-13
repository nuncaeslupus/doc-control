"""Find phrases across paragraph marks and in Word's Find syntax (T54).

The gate ``find_phrase_gaps == 0`` over every corpus kind and the 100k-word novel counts:
non-empty, non-repeated paragraphs left without a find phrase (other than the document's
first and last), phrases that do not occur exactly once in the joined text, and Word Find
strings that do not decode back to their phrase.
"""

from __future__ import annotations

from pathlib import Path

from doc_control.model.document import Document
from doc_control.ops.locate import WORD_FIND_MAX, _indexes, word_find
from tests.corpus.generator import KINDS, generate
from tests.ops.test_locate import find_count

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
        index = Document(str(path)).index
        texts = [index.paragraph(n).text for n in range(1, len(index) + 1)]
        _, finds = _indexes(texts)
        for i, found in enumerate(finds):
            checks += 1
            if found.text is None:
                edge = i in (0, len(texts) - 1)
                gaps += found.reason not in ("empty", "duplicate") and not edge
                continue
            gaps += find_count(texts, found.text) != 1
            word = word_find(found.text)
            gaps += word is not None and unword(word) != found.text
    print(f"find_phrase_gaps == {gaps} (of {checks} paragraphs)")
    assert gaps == 0


def test_word_find_codes_and_limit() -> None:
    assert word_find("a^b\r\tc\u00a0d\u2011e\u00adf") == "a^^b^p^tc^sd^~e^-f"
    assert word_find("x" * WORD_FIND_MAX) == "x" * WORD_FIND_MAX
    assert word_find("^" * 128) is None  # 256 characters once escaped
