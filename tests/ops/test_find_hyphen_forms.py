"""Word Find strings for the two forms of a soft and of a no-break hyphen (t-a910c738).

Word finds its own optional hyphen (``w:softHyphen``) with ``^-`` and a literal U+00AD with
``^u173``; its own no-break hyphen (``w:noBreakHyphen``) with ``^~`` and a literal U+2011
with ``^u8209``. A plain search skips the optional hyphen and a plain ``-`` also finds the
no-break hyphen (measured in Word 16.0.17932). The gate ``hyphen_form_mismatches == 0``
counts find phrases that differ from the expected string, in the whole-document indexes and
in the lazy ones.
"""

from __future__ import annotations

from pathlib import Path

from doc_control import api
from doc_control.model.document import Document
from doc_control.model.wordfind import as_searched, word_find
from doc_control.ops.locate import Forms, LazyIndexes, _indexes, _plain_marks
from tests.helpers import make_docx

SOFT = "\N{SOFT HYPHEN}"
NO_BREAK = "\N{NON-BREAKING HYPHEN}"
TYPED_SOFT = "</w:t><w:softHyphen/><w:t>"
TYPED_NO_BREAK = "</w:t><w:noBreakHyphen/><w:t>"

#: Each hyphen word is the only word of its paragraph the others do not have.
FORMS = (
    (f"same ze{TYPED_SOFT}bra here", "ze^-bra"),
    (f"same wal{SOFT}rus here", "wal^u173rus"),
    (f"same ot{TYPED_NO_BREAK}ter here", "ot^~ter"),
    (f"same bad{NO_BREAK}ger here", "bad^u8209ger"),
)


def forms_docx(path: Path) -> Path:
    return make_docx(path, *(text for text, _ in FORMS))


def phrases(path: Path) -> tuple[list[tuple[str | None, str | None]], ...]:
    """Per paragraph ``(find_phrase, find_phrase_word)``, from the whole-document indexes
    and from the lazy ones."""
    doc = Document(str(path))
    paras = doc.index.paragraphs
    texts = [p.text for p in paras]
    forms = Forms(texts, {p.number - 1: p.typed for p in paras if p.typed})
    marks = _plain_marks(doc)
    lazy = LazyIndexes(texts, marks, forms.search)
    out: list[list[tuple[str | None, str | None]]] = []
    for finds in (
        _indexes(texts, marks, forms.search)[1],
        [lazy.find(i) for i in range(len(texts))],
    ):
        out.append(
            [
                forms.written(i, f) if f.text is not None else (None, None)
                for i, f in enumerate(finds)
            ]
        )
    return tuple(out)


def test_hyphen_form_gate(tmp_path: Path) -> None:
    path = forms_docx(tmp_path / "forms.docx")
    whole, lazy = phrases(path)
    mismatches = sum(row != lazy[i] for i, row in enumerate(whole))
    for rows in (whole, lazy):
        mismatches += sum(row[1] != want for row, (_, want) in zip(rows, FORMS, strict=True))
    # The phrase itself keeps the document's characters, whichever form they have.
    mismatches += [row[0] for row in whole] != [
        f"ze{SOFT}bra",
        f"wal{SOFT}rus",
        f"ot{NO_BREAK}ter",
        f"bad{NO_BREAK}ger",
    ]
    with api.open(path) as doc:
        rows = doc.run("locate", {"structure": False})["paragraphs"]
    mismatches += [r["find_phrase_word"] for r in rows] != [want for _, want in FORMS]
    print(f"hyphen_form_mismatches == {mismatches}")
    assert mismatches == 0


def test_index_tells_word_s_own_hyphens_from_literal_ones(tmp_path: Path) -> None:
    doc = Document(str(forms_docx(tmp_path / "forms.docx")))
    assert [p.typed for p in doc.index.paragraphs] == [(7,), (), (7,), ()]
    hyphens = [[c for c in p.text if c in SOFT + NO_BREAK] for p in doc.index.paragraphs]
    assert hyphens == [[SOFT], [SOFT], [NO_BREAK], [NO_BREAK]]  # the text reads them alike


def test_word_find_writes_the_code_of_each_form() -> None:
    text = f"a{SOFT}b{SOFT}c{NO_BREAK}d{NO_BREAK}e"
    assert word_find(text, {1, 5}) == "a^-b^u173c^~d^u8209e"
    assert word_find(text, ()) == "a^u173b^u173c^u8209d^u8209e"
    assert word_find(text) is None  # form not known
    assert as_searched(text, {1, 5}) == (f"ab{SOFT}c-d{NO_BREAK}e", [0, 2, 3, 4, 5, 6, 7, 8])


def test_a_plain_search_skips_word_s_optional_hyphen(tmp_path: Path) -> None:
    # Word finds "zebra" in both paragraphs, so neither word is a find phrase alone.
    path = make_docx(tmp_path / "skip.docx", f"ze{TYPED_SOFT}bra one", "zebra two")
    for rows in phrases(path):
        assert rows == [("one", "one"), ("two", "two")]
    # A literal U+00AD is not skipped: both words are unique.
    path = make_docx(tmp_path / "literal.docx", f"ze{SOFT}bra one", "zebra one")
    for rows in phrases(path):
        assert rows == [(f"ze{SOFT}bra", "ze^u173bra"), ("zebra", "zebra")]


def test_a_plain_hyphen_finds_word_s_no_break_hyphen(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "typed.docx", f"ot{TYPED_NO_BREAK}ter one", "ot-ter two")
    for rows in phrases(path):
        assert rows == [("one", "one"), ("two", "two")]
    path = make_docx(tmp_path / "literal.docx", f"ot{NO_BREAK}ter one", "ot-ter one")
    for rows in phrases(path):
        assert rows == [(f"ot{NO_BREAK}ter", "ot^u8209ter"), ("ot-ter", "ot-ter")]


def test_a_phrase_across_a_mark_keeps_the_forms(tmp_path: Path) -> None:
    # The repeated paragraph has no phrase of its own: its phrase spans a mark.
    word = f"ze{TYPED_SOFT}bra{TYPED_SOFT}"  # the trailing optional hyphen is left out
    path = make_docx(tmp_path / "across.docx", "first", word, "second", word)
    for rows in phrases(path):
        assert rows[1] == (f"first\rze{SOFT}bra", "first^pze^-bra")
        assert rows[3] == (f"second\rze{SOFT}bra", "second^pze^-bra")
    with api.open(path) as doc:
        rows = doc.run("locate", {"structure": False})["paragraphs"]
    assert rows[1]["find_phrase_across"] == "previous"
    assert rows[1]["find_phrase_word"] == "first^pze^-bra"


def test_a_paragraph_that_is_only_optional_hyphens_is_empty(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "only.docx", "one", TYPED_SOFT.join(("", "")), "two")
    whole, lazy = phrases(path)
    assert whole == lazy == [("one", "one"), (None, None), ("two", "two")]
