"""Plain-text headings (``chapter_blank_lines``) and ``chapter_level: "auto"`` (T50)."""

from __future__ import annotations

from typing import Any

from doc_control.model.structure import Profile, detect
from tests.model.test_structure import BODY, build

PLAIN = Profile(chapter_blank_lines=2)


def plain_book(headings: list[list[str]]) -> list[Any]:
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
