"""Plain-text headings (``chapter_blank_lines``) and ``chapter_level: "auto"`` (T50)."""

from __future__ import annotations

from typing import Any

from doc_control.model.structure import Profile, detect
from tests.model.test_structure import BODY, build

PLAIN = Profile(chapter_blank_lines=2)


def plain_book(headings: list[list[Any]]) -> list[Any]:
    """Unstyled paragraphs as converted from a .txt: headings set off by blank lines."""
    rows: list[Any] = []
    for lines in headings:
        rows += ["", "", "", *lines, ""]
        rows += [BODY, "", "—¿Vienes?", "", "", "Era tarde.", "", BODY]
    return rows


def test_blank_lines_make_plain_text_headings() -> None:
    titles = [["STORY OF THE DOOR"], ["SEARCH FOR MR. HYDE"], ["-III-", "Perdido"]]
    paras = build(*plain_book(titles))
    assert detect(paras).chapters == ()
    found = detect(paras, PLAIN)
    assert [c.signals[0] for c in found.chapters] == ["isolated"] * 3
    assert found.chapters[2].kind == "numeral"
    assert found.chapters[2].heading_last == found.chapters[2].first + 1


def test_chapter_level_auto() -> None:
    h1 = {"heading_level": 1, "heading_source": "heading_style"}
    h2 = {"heading_level": 2, "heading_source": "heading_style"}
    only_h2 = build(("Uno", h2), BODY, ("Dos", h2), BODY)
    assert len(detect(only_h2).chapters) == 0
    assert len(detect(only_h2, Profile(chapter_level="auto")).chapters) == 2
    subheads = build(*[r for n in range(6) for r in ((f"Cap {n}", h1), BODY, ("Sub", h2), BODY)])
    assert detect(subheads, Profile(chapter_level="auto")) == detect(subheads)
    parts = [("Primera parte", h1), *[r for n in range(4) for r in ((f"Cap {n}", h2), BODY)]]
    found = detect(build(*parts), Profile(chapter_level="auto"))
    assert (len(found.parts), len(found.chapters)) == (1, 4)


def test_what_blank_lines_do_not_make_a_heading() -> None:
    h2 = {"heading_level": 2, "heading_source": "heading_style"}
    long = "uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece"
    cases: list[list[Any]] = [
        ["Era tarde."],
        ["—Ven aquí"],
        ["Dijo así:"],
        [long],
        [("Interludio", h2)],
    ]
    for lines in cases:
        paras = build(*plain_book([["STORY OF THE DOOR"], lines]))
        assert len(detect(paras, PLAIN).chapters) == 1, lines
    one_above = build("", "Uno", "", BODY, "", "", "Dos", "", BODY)
    assert [c.first for c in detect(one_above, PLAIN).chapters] == [7]
    for word in ("Silencio", "Uno", "3", "1.", "I"):
        none_below = build("", "", word, BODY, "", "", "Dos", "", BODY)
        assert [c.first for c in detect(none_below, PLAIN).chapters] == [7], word


def test_chapter_level_auto_keeps_few_chapters_over_many_subheads() -> None:
    h1 = {"heading_level": 1, "heading_source": "heading_style"}
    h2 = {"heading_level": 2, "heading_source": "heading_style"}
    rows = [r for n in range(3) for r in ((f"Cap {n}", h1), ("A", h2), BODY, ("B", h2), BODY)]
    found = detect(build(*rows), Profile(chapter_level="auto"))
    assert len(found.chapters) == 3
    titled = [("La novela", h1), *[r for n in range(3) for r in ((f"Cap {n}", h2), BODY)]]
    assert len(detect(build(*titled), Profile(chapter_level="auto")).chapters) >= 3


def test_hard_wrapped_numeral_heading_takes_its_title_line() -> None:
    title = "Donde se cuenta lo que pasó en la casa del molino"  # 11 words
    paras = build("", "", "", "", "-XIV-", title, BODY, BODY, "", "", "CAPÍTULO 3", BODY, BODY)
    found = detect(paras, PLAIN)
    assert [(c.first, c.heading_last, c.kind) for c in found.chapters] == [
        (5, 6, "numeral"),
        (11, 11, "chapter"),
    ]
    assert detect(build("", "", "", "", "-XIV-", title, BODY)).chapters == ()
    sentence = build("", "", "", "", "-XIV-", "Era tarde y llovía sobre el pueblo.", BODY)
    assert detect(sentence, PLAIN).chapters[0].heading_last == 5
