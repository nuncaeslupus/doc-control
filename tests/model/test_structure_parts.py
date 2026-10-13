"""Parts, style-marked chapters and front matter (T40).

The gate ``structure_part_chapter_errors == 0``: generated books whose chapters are untitled
body paragraphs in a chapter-opening style (``Normal inicio capítulo``), grouped under
heading-titled parts or ``Parte`` headings, with front matter and an epilogue, are split
exactly: every part, chapter and front boundary is where the generator put it.
"""

from __future__ import annotations

import random
from typing import Any

from doc_control.model.address import fingerprint
from doc_control.model.structure import Para, Profile, Structure, detect

BODY = "La lluvia cayó sobre el pueblo durante toda la noche y nadie salió a la calle."
OPEN_STYLE = "Normal inicio capítulo"
HEADING: dict[str, Any] = {"heading_level": 1, "heading_source": "heading_style", "centred": True}


def _book(rng: random.Random) -> tuple[list[Para], dict[str, Any]]:
    """A book and where its units start (1-based paragraph numbers)."""
    rows: list[tuple[str, dict[str, Any]]] = []
    want: dict[str, Any] = {"parts": [], "chapters": [], "front": None, "part_of": {}}
    if rng.random() < 0.7:
        want["front"] = 1
        rows += [("Cuentos de la lluvia", {"centred": True}), ("", {}), ("A mi madre.", {})]
    word_parts = rng.random() < 0.3
    n_parts = rng.randint(1, 4)
    n = 0
    for part in range(1, n_parts + 1):
        title = f"Parte {part}" if word_parts else rng.choice(["El invierno", "La huida", "Mar"])
        rows.append(
            (f"{title} {part}" if not word_parts else title, {**HEADING, "page_break_before": True})
        )
        part_at = len(rows)
        want["parts"].append(part_at)
        # one chapter under every heading reads as titled chapters with an opening style
        for _ in range(rng.randint(2 if part == 1 else 1, 6)):
            n += 1
            if word_parts:
                rows.append((f"Capítulo {n}", {**HEADING, "page_break_before": True}))
            else:
                rows.append(
                    (f"{BODY} ({n})", {"style_name": OPEN_STYLE, "page_break_before": True})
                )
            want["chapters"].append(len(rows))
            want["part_of"][len(rows)] = part_at
            for k in range(rng.randint(1, 5)):
                rows.append((f"{BODY} [{n}.{k}]", {"style_name": "Normal"}))
                if rng.random() < 0.2:
                    rows.append(("* * *", {"centred": True}))
    if rng.random() < 0.5:
        rows.append(("Epílogo", {**HEADING, "page_break_before": True}))
        want["chapters"].append(len(rows))
        rows.append((BODY, {}))
    paras = [
        Para(i, text, fingerprint(text), empty=not text, **layout)
        for i, (text, layout) in enumerate(rows, 1)
    ]
    return paras, want


def _errors(found: Structure, want: dict[str, Any]) -> int:
    errors: int = len(set(want["parts"]) ^ {p.first for p in found.parts})
    errors += len(set(want["chapters"]) ^ {c.first for c in found.chapters})
    errors += (found.front.first if found.front else None) != want["front"]
    pid = {p.first: p.id for p in found.parts}
    for c in found.chapters:
        errors += c.part != (
            pid.get(want["part_of"][c.first]) if c.first in want["part_of"] else None
        )
    return errors


def test_parts_and_style_chapters_found_exactly() -> None:
    rng = random.Random(40)
    errors = books = 0
    for _ in range(300):
        paras, want = _book(rng)
        errors += _errors(detect(paras), want)
        books += 1
    print(f"structure_part_chapter_errors == {errors} (of {books} books)")
    assert errors == 0


def styled(n: int, text: str, style: str = OPEN_STYLE) -> Para:
    return Para(n, text, fingerprint(text), style_name=style, page_break_before=True)


def test_style_chapter_counts_its_first_paragraph() -> None:
    paras = [
        Para(1, "Mar", fingerprint("Mar"), **HEADING),
        styled(2, BODY),
        styled(3, BODY + "!"),
        Para(4, BODY + ".", fingerprint(BODY + "."), style_name="Normal"),
    ]
    found = detect(paras)
    chapter = found.chapters[1]
    assert chapter.signals[0] == "chapter_style"
    assert chapter.words == 2 * len(BODY.split())
    assert found.parts[0].words == 0 and found.parts[0].chapters == 2
    assert chapter.part == found.parts[0].id and found.parts[0].id.startswith("pt-")


def test_style_words_are_configurable_and_can_be_off() -> None:
    paras = [styled(n, f"{BODY} {n}", "Apertura") for n in (1, 2)]
    assert detect(paras).chapters == ()
    assert len(detect(paras, Profile(chapter_style_words=("apertura",))).chapters) == 2
    marked = [styled(1, BODY)]
    assert detect(marked, Profile(chapter_styles=False)).chapters == ()


def test_a_prologue_heading_is_not_a_part() -> None:
    paras = [
        Para(1, "Prólogo", fingerprint("Prólogo"), **HEADING),
        Para(2, BODY, fingerprint(BODY), style_name="Normal"),
        styled(3, BODY + " 1"),
        styled(4, BODY + " 2"),
    ]
    found = detect(paras)
    assert found.parts == ()
    assert [c.first for c in found.chapters] == [1, 3, 4]
    assert found.front is None


def test_titled_chapters_with_an_opening_style_stay_chapters() -> None:
    """``Capítulo N`` headings, each followed by one paragraph in the opener style."""
    rows: list[Para] = []
    for n in (1, 2, 3):
        rows.append(Para(len(rows) + 1, f"Capítulo {n}", fingerprint(f"c{n}"), **HEADING))
        rows.append(styled(len(rows) + 1, f"{BODY} {n}"))
        rows.append(Para(len(rows) + 1, BODY, fingerprint(f"x{n}")))
    found = detect(rows)
    assert found.parts == ()
    assert [c.first for c in found.chapters] == [1, 4, 7]
    assert {r for _, _, r in found.suppressed} == {"opening_paragraph_style"}


def test_a_body_style_naming_chapters_marks_none() -> None:
    rows = [
        Para(n, f"{BODY} {n}", fingerprint(f"t{n}"), style_name="Texto capítulo")
        for n in range(1, 6)
    ]
    found = detect(rows)
    assert found.chapters == ()
    assert {r for _, _, r in found.suppressed} == {"body_style"}


def test_a_parte_holds_its_chapters_and_their_styled_chapters_flat() -> None:
    rows = [
        Para(1, "Parte I", fingerprint("p1"), **HEADING),
        Para(2, "Capítulo 1", fingerprint("c1"), **HEADING),
        styled(3, BODY),
        styled(4, BODY + "!"),
        Para(5, "Parte II", fingerprint("p2"), **HEADING),
        styled(6, BODY + "?"),
        *(Para(n, f"{BODY} {n}", fingerprint(f"b{n}")) for n in range(7, 11)),
    ]
    found = detect(rows)
    assert [p.first for p in found.parts] == [1, 5]
    assert [(c.first, c.part) for c in found.chapters] == [
        (2, found.parts[0].id),
        (3, found.parts[0].id),
        (4, found.parts[0].id),
        (6, found.parts[1].id),
    ]
