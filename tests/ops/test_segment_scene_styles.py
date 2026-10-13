"""`segment`: scenes opened by a paragraph style and heading subtitles on a real .docx (T49).

Gate: ``scene_heading_mismatches == 0``: an ``After break`` paragraph opens a scene (not
right under a heading), a short line under ``-I-`` is the heading's title, and both can
be switched off.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.ops.styleset import styleset_install
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import CONTENT_TYPES, RELS

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
BODY = "La lluvia cayó sobre el pueblo durante toda la noche y nadie salió a la calle."
STYLES = {
    "styleset": "scenes",
    "version": 1,
    "styles": [
        {"id": "Body", "name": "Body", "type": "paragraph"},
        {"id": "AfterBreak", "name": "After break", "type": "paragraph", "based_on": "Body"},
        {
            "id": "Heading1",
            "name": "heading 1",
            "type": "paragraph",
            "based_on": "Body",
            "paragraph": {"outline_level": 0},
        },
    ],
}


def book(tmp: Path, rows: list[tuple[str, str]]) -> Path:
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="{s}"/></w:pPr><w:r><w:t>{t}</w:t></w:r></w:p>'
        for s, t in rows
    )
    path = tmp / "book.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
        )
    sets = tmp / "scenes.json"
    sets.write_text(json.dumps(STYLES), "utf-8")
    styleset_install({"doc": str(path), "path": str(sets)})
    return path


def run(op: str, request: dict[str, Any]) -> dict[str, Any]:
    load_operations()
    reply = REGISTRY[op].run(request)
    assert isinstance(reply, Reply)
    return reply.result


def rows() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for c in range(1, 4):
        out += [("Heading1", f"-{'I' * c}-"), ("Body", f"El camino {c}")]
        out += [("AfterBreak", f"{BODY} {c}")]  # right under the heading: no scene
        out += [("Body", BODY)] * 8
        out += [("AfterBreak", f"{BODY} {c} b"), ("Body", BODY)]
    return out


def test_after_break_style_opens_a_scene_and_subtitle_joins_the_heading(tmp_path: Path) -> None:
    doc = str(book(tmp_path, rows()))
    scenes = run("segment", {"doc": doc, "unit": "scenes"})["scenes"]
    assert [s["signals"] for s in scenes].count(["scene_style"]) == 3
    assert len(scenes) == 6
    chapters = run("segment", {"doc": doc})["chapters"]
    assert [(c["first"], c["heading_last"]) for c in chapters] == [(1, 2), (14, 15), (27, 28)]
    assert {c["words"] for c in chapters} == {11 * 16 + 3}  # the subtitle is not text


def test_scene_styles_and_subtitles_can_be_switched_off(tmp_path: Path) -> None:
    doc = str(book(tmp_path, rows()))
    profile = {"scene_styles": False, "heading_subtitles": False}
    scenes = run("segment", {"doc": doc, "unit": "scenes", "profile": profile})["scenes"]
    assert len(scenes) == 3
    chapters = run("segment", {"doc": doc, "profile": profile})["chapters"]
    assert [c["heading_last"] for c in chapters] == [1, 14, 27]
    words = {"scene_style_words": ["despues"]}
    scenes = run("segment", {"doc": doc, "unit": "scenes", "profile": words})["scenes"]
    assert len(scenes) == 3


def test_scene_styles_and_heading_subtitles(tmp_path: Path) -> None:
    doc = str(book(tmp_path, rows()))
    off = {"scene_styles": False, "heading_subtitles": False}
    scenes = run("segment", {"doc": doc, "unit": "scenes"})["scenes"]
    chapters = run("segment", {"doc": doc})["chapters"]
    plain = run("segment", {"doc": doc, "unit": "scenes", "profile": off})["scenes"]
    plain_chapters = run("segment", {"doc": doc, "profile": off})["chapters"]
    checks = [
        [s["signals"] for s in scenes].count(["scene_style"]) == 3,
        len(scenes) == 6,
        [(c["first"], c["heading_last"]) for c in chapters] == [(1, 2), (14, 15), (27, 28)],
        {c["words"] for c in chapters} == {11 * 16 + 3},
        len(plain) == 3,
        [c["heading_last"] for c in plain_chapters] == [1, 14, 27],
    ]
    mismatches = checks.count(False)
    print(f"scene_heading_mismatches == {mismatches} (of {len(checks)} checks)")
    assert mismatches == 0
