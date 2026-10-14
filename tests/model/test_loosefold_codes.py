"""Word find codes in agent phrases (FIND T3).

Gate: ``word_code_failures == 0``. ``translate_codes`` turns each code Word's Find box takes
into its character, left to right, and leaves any other ``^x`` as written.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

from doc_control.model.index import build_index
from doc_control.model.wordcodes import translate_codes
from doc_control.model.wordfind import two_forms, word_find
from tests.corpus.generator import generate

CASES = [
    ("^^", "^"),
    ("^p", "\r"),
    ("^l", "\n"),
    ("^t", "\t"),
    ("^s", "\N{NO-BREAK SPACE}"),
    ("^-", "\N{SOFT HYPHEN}"),
    ("^~", "\N{NON-BREAKING HYPHEN}"),
    ("^u173", "\N{SOFT HYPHEN}"),
    ("^u8209", "\N{NON-BREAKING HYPHEN}"),
    ("^u65", "A"),
    ("^^p", "^p"),
    ("^^^p", "^\r"),
    ("a^pb^tc", "a\rb\tc"),
    ("^x", "^x"),
    ("^", "^"),
    ("a^", "a^"),
    ("^u", "^u"),
    ("^ux", "^ux"),
    ("^^u65", "^u65"),
    ("plain", "plain"),
    ("", ""),
]


def test_word_codes_table() -> None:
    failures = [(src, want) for src, want in CASES if translate_codes(src) != want]
    print(f"word_code_failures == {len(failures)}")
    assert not failures, failures


def test_find_phrase_word_round_trips() -> None:
    texts = [
        p.text
        for kind in ("novel", "kitchen_sink")
        for p in build_index(
            zipfile.ZipFile(BytesIO(generate(kind, 11))).read("word/document.xml")
        ).paragraphs
    ]
    texts += ["a^p b\rc\nd\te\N{NO-BREAK SPACE}f", "^^ ^u65 ^"]
    checked = [t for t in texts if not two_forms(t) and word_find(t) is not None]
    failures = [t for t in checked if translate_codes(str(word_find(t))) != t]
    print(f"word_code_roundtrip_failures == {len(failures)} of {len(checked)}")
    assert checked
    assert not failures, failures[:3]
