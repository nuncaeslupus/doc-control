"""Segmentation: sentences against a gold set, dialogue lines, scene breaks, chapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_control.model.index import build_index
from doc_control.model.segment import (
    ChapterHeading,
    dialogue_lines,
    is_scene_break,
    parse_chapter_heading,
    segment_document,
    sentence_span,
    split_sentences,
)
from tests.model.test_index import _doc, _p, _r, _t

GOLD = Path(__file__).parent / "data" / "segment_gold.txt"
TARGET_F1 = 0.98
MIN_GOLD_SENTENCES = 500


def _load_gold() -> list[tuple[str, str, list[str]]]:
    """``(language, text, sentences)`` per gold paragraph."""
    language = "es"
    out: list[tuple[str, str, list[str]]] = []
    for raw in GOLD.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            language = "es" if line == "@es" else "generic"
            continue
        sentences = line.split(" | ")
        out.append((language, " ".join(sentences), sentences))
    return out


def _boundaries(sentences: list[str]) -> set[int]:
    """Offsets where a sentence ends and another follows, in the joined text."""
    ends: set[int] = set()
    pos = 0
    for sentence in sentences[:-1]:
        pos += len(sentence)
        ends.add(pos)
        pos += 1
    return ends


def _evaluate() -> tuple[float, int, int, list[str]]:
    tp = fp = fn = sentences = 0
    misses: list[str] = []
    for language, text, gold in _load_gold():
        sentences += len(gold)
        want = _boundaries(gold)
        got = {span.end for span in split_sentences(text, language)[:-1]}
        tp += len(want & got)
        fp += len(got - want)
        fn += len(want - got)
        if want != got:
            misses.append(" | ".join(s.text(text) for s in split_sentences(text, language)))
    f1 = 2 * tp / (2 * tp + fp + fn)
    return f1, sentences, tp, misses


def test_spanish_abbreviations_do_not_split_sentences() -> None:
    text = "Sr. García llegó."
    assert [s.text(text) for s in split_sentences(text)] == ["Sr. García llegó."]
    text = "El Dr. Ruiz y la Dra. Pérez llegaron. Hablaron con el Sr. Gil."
    assert [s.text(text) for s in split_sentences(text)] == [
        "El Dr. Ruiz y la Dra. Pérez llegaron.",
        "Hablaron con el Sr. Gil.",
    ]


def test_boundary_f1_on_the_gold_set() -> None:
    f1, sentences, matched, misses = _evaluate()
    print(f"boundary_f1={f1:.4f} (gold sentences={sentences}, boundaries matched={matched})")
    assert sentences >= MIN_GOLD_SENTENCES
    assert f1 >= TARGET_F1, "\n".join(misses)


def test_gold_set_has_no_private_documents_in_tests() -> None:
    assert GOLD.suffix == ".txt"
    assert not list(GOLD.parent.glob("*.docx"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("   ", []),
        ("Sin punto final", ["Sin punto final"]),
        ("  Una frase.  Otra frase.  ", ["Una frase.", "Otra frase."]),
        ("Verso uno\nverso dos\nVerso tres.", ["Verso uno", "verso dos", "Verso tres."]),
        ("¿Qué? ¡Nada! Pues...", ["¿Qué?", "¡Nada!", "Pues..."]),
        ("Pues... no sé.", ["Pues... no sé."]),
        ("Cuesta 3.50 euros. Caro.", ["Cuesta 3.50 euros.", "Caro."]),
        ("«Vamos.» Salió.", ["«Vamos.»", "Salió."]),
        ("«Vamos», dijo. Salió.", ["«Vamos», dijo.", "Salió."]),
        ("—¿Vienes? —preguntó. —Voy.", ["—¿Vienes? —preguntó.", "—Voy."]),
        ("Compró pan, etc. y se fue.", ["Compró pan, etc. y se fue."]),
        ("Compró pan, etc. Luego se fue.", ["Compró pan, etc.", "Luego se fue."]),
        ("Ver pág. 12. Luego volvió.", ["Ver pág. 12.", "Luego volvió."]),
    ],
)
def test_split_sentences_cases(text: str, expected: list[str]) -> None:
    assert [s.text(text) for s in split_sentences(text)] == expected


def test_generic_language_uses_english_abbreviations() -> None:
    text = "Mr. Smith left Acme Inc. Then he slept."
    assert [s.text(text) for s in split_sentences(text, "generic")] == [
        "Mr. Smith left Acme Inc.",
        "Then he slept.",
    ]
    # "Av." is Spanish only: in generic text it ends the sentence.
    text = "Lives on Av. Smith."
    assert len(split_sentences(text, "generic")) == 2
    assert len(split_sentences(text, "es")) == 1


def test_sentence_offsets_are_code_points_of_the_text() -> None:
    text = "Ñandú corre. 😀 Fin."
    first, second = split_sentences(text)
    assert (first.start, first.end) == (0, 12)
    assert text[second.start : second.end] == "😀 Fin."
    assert sentence_span(text, 2) == second
    assert sentence_span(text, 3) is None
    assert sentence_span(text, 0) is None


def test_every_gold_sentence_text_roundtrips_through_its_span() -> None:
    for language, text, gold in _load_gold():
        spans = split_sentences(text, language)
        assert all(text[s.start : s.end] == text[s.start : s.end].strip() for s in spans)
        assert "".join(s.text(text) for s in spans).replace(" ", "") == "".join(gold).replace(
            " ", ""
        )


def test_sentence_address_resolves_to_offsets() -> None:
    index = build_index(_doc(_p(_r(_t("Sr. García llegó. ¿Y tú? Adiós.")))))
    address = index.paragraphs[0].address
    text = index.paragraphs[0].text
    for k, expected in enumerate(["Sr. García llegó.", "¿Y tú?", "Adiós."], 1):
        resolved = index.resolve_range(f"{address}.s{k}")
        assert resolved.start_offset is not None and resolved.end_offset is not None
        assert text[resolved.start_offset : resolved.end_offset] == expected
        assert resolved.sentence == k


def test_dialogue_line_with_narrator_interjection() -> None:
    text = "—Hola —dijo Ana—. Vamos."
    (line,) = dialogue_lines(text)
    assert (line.start, line.end) == (0, len(text))
    assert [(p.kind, text[p.start : p.end]) for p in line.parts] == [
        ("speech", "Hola"),
        ("narration", "dijo Ana"),
        ("speech", ". Vamos."),
    ]


def test_dialogue_new_turn_and_plain_lines() -> None:
    text = "—Vamos. —Ya voy.\nNarración sin raya.\n  —Sí —dijo."
    lines = dialogue_lines(text)
    assert len(lines) == 2
    assert [(p.kind, text[p.start : p.end]) for p in lines[0].parts] == [
        ("speech", "Vamos."),
        ("speech", "Ya voy."),
    ]
    assert [(p.kind, text[p.start : p.end]) for p in lines[1].parts] == [
        ("speech", "Sí"),
        ("narration", "dijo."),
    ]
    assert dialogue_lines("Sin raya — aquí no") == []
    assert dialogue_lines("") == []


@pytest.mark.parametrize("text", ["* * *", "***", "---", "###", " ~ ~ ~ ", "⁂", "— — —", "• • •"])
def test_scene_breaks(text: str) -> None:
    assert is_scene_break(text)


@pytest.mark.parametrize("text", ["", "  ", "—", "**", "* * a", "Fin.", "-", "a a a", "*-*"])
def test_not_scene_breaks(text: str) -> None:
    assert not is_scene_break(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Capítulo 3", ChapterHeading("chapter", 3, "")),
        ("CAPÍTULO PRIMERO", ChapterHeading("chapter", 1, "")),
        ("Capítulo IV. El regreso", ChapterHeading("chapter", 4, "El regreso")),
        ("Capítulo 12: La noche", ChapterHeading("chapter", 12, "La noche")),
        ("Cap. 5", ChapterHeading("chapter", 5, "")),
        ("Chapter Seven", ChapterHeading("chapter", 7, "")),
        ("CHAPTER XIV - The Storm", ChapterHeading("chapter", 14, "The Storm")),
        ("Parte II", ChapterHeading("part", 2, "")),
        ("Prólogo", ChapterHeading("prologue", None, "")),
        ("Epílogo: Años después", ChapterHeading("epilogue", None, "Años después")),
        ("Introduction", ChapterHeading("introduction", None, "")),
        ("XII", ChapterHeading("numeral", 12, "")),
        ("7.", ChapterHeading("numeral", 7, "")),
    ],
)
def test_chapter_headings(text: str, expected: ChapterHeading) -> None:
    assert parse_chapter_heading(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Capítulo",
        "Capítulo aparte, dijo él.",
        "El capítulo 3 es largo.",
        "C",
        "IIII",
        "0",
        "Prólogo " + "de un libro largo " * 10,
        "Cuatro",
        "Partido de fútbol",
    ],
)
def test_not_chapter_headings(text: str) -> None:
    assert parse_chapter_heading(text) is None


def test_segment_document() -> None:
    texts = [
        "Un título suelto.",
        "Capítulo 1",
        "—Hola —dijo Ana. Llegó tarde.",
        "* * *",
        "Pasó el tiempo.",
        "Capítulo 2. Fin",
        "Último párrafo.",
    ]
    seg = segment_document(texts)
    assert [len(s) for s in seg.sentences] == [1, 1, 2, 1, 1, 2, 1]
    assert [n for n, _ in seg.dialogue] == [3]
    assert seg.scene_breaks == (4,)
    assert [(c.first, c.last, c.heading.number) for c in seg.chapters] == [(2, 5, 1), (6, 7, 2)]
    assert segment_document([]).chapters == ()
