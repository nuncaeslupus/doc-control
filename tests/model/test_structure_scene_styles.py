"""Scenes opened by a paragraph style, heading subtitles and decorated numerals."""

from __future__ import annotations

from typing import Any

from doc_control.model.structure import Profile, detect
from tests.model.test_structure import BODY, build

AFTER: dict[str, Any] = {"style_name": "Normal tras pausa"}
HEAD: dict[str, Any] = {"heading_level": 1, "heading_source": "heading_style"}


def chapter(heading: str, *rest: str | tuple[str, dict[str, Any]]) -> list[Any]:
    return [(heading, HEAD), *rest]


def test_scene_style_opens_a_scene_except_after_a_heading_or_a_marker() -> None:
    paras = build(
        *chapter("Capítulo 1", (BODY, AFTER), BODY, BODY, (BODY, AFTER), BODY, "* * *"),
        (BODY, AFTER),
        BODY,
        *chapter("Capítulo 2", BODY, BODY, BODY, BODY, BODY, BODY, BODY, BODY),
    )
    found = detect(paras)
    assert [s.signals for s in found.scenes] == [
        ("chapter_start",),
        ("scene_style",),
        ("scene_marker",),
        ("chapter_start",),
    ]


def test_a_scene_style_on_a_third_of_the_text_is_a_body_style() -> None:
    paras = build(*chapter("Capítulo 1", *[(BODY, AFTER), BODY] * 4))
    assert len(detect(paras).scenes) == 1


def test_scene_style_words_match_at_a_word_start() -> None:
    extra = {"style_name": "Extras"}
    paras = build(*chapter("Capítulo 1", *[BODY] * 9, (BODY, extra), BODY))
    assert len(detect(paras).scenes) == 1
    custom = Profile(scene_style_words=("extra",))
    assert len(detect(paras, custom).scenes) == 2


def test_subtitle_joins_an_untitled_heading() -> None:
    paras = build(*chapter("-IV-", "El camino del molino", BODY, BODY))
    found = detect(paras)
    assert found.chapters[0].heading_last == 2
    assert found.chapters[0].number == 4
    assert found.chapters[0].words == 32


def test_no_subtitle_when_titled_long_punctuated_or_dialogue() -> None:
    for heading, line in [
        ("Capítulo 3. La partida", "El camino"),
        ("Capítulo 3", "Era de noche y nadie en todo el pueblo dormía"),
        ("Capítulo 3", "Era de noche."),
        ("Capítulo 3", "—Ven aquí"),
        ("La partida", "El camino"),
    ]:
        found = detect(build(*chapter(heading, line, BODY)))
        assert found.chapters[0].heading_last == 1, (heading, line)


def test_decorated_numerals_are_headings_when_laid_out() -> None:
    centred = {"centred": True}
    for text in ("-I-", "— 3 —", "[IV]"):
        found = detect(build((text, centred), BODY, BODY))
        assert [c.kind for c in found.chapters] == ["numeral"], text
    assert detect(build("[IV]", BODY)).chapters == ()
