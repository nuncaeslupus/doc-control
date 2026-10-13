"""`segment` unit ``parts``, ``front`` and `locate`'s ``part`` on a real .docx (T40)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control.envelope import Reply
from doc_control.ops.styleset import styleset_install
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import CONTENT_TYPES, RELS

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
BODY = "La lluvia cayó sobre el pueblo durante toda la noche y nadie salió a la calle."
STYLES = {
    "styleset": "parts",
    "version": 1,
    "styles": [
        {"id": "Body", "name": "Body", "type": "paragraph"},
        {"id": "Opener", "name": "Normal inicio capítulo", "type": "paragraph", "based_on": "Body"},
        {
            "id": "PartTitle",
            "name": "Part title",
            "type": "paragraph",
            "based_on": "Body",
            "paragraph": {"outline_level": 0, "alignment": "center"},
        },
    ],
}


def book(tmp: Path) -> Path:
    rows = [("Body", "Cuentos de la lluvia"), ("Body", "A mi madre.")]
    for part in ("El invierno", "La huida"):
        rows.append(("PartTitle", part))
        for n in range(3):
            rows.append(("Opener", f"{BODY} {part} {n}"))
            rows += [("Body", BODY)] * 3
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
    sets = tmp / "parts.json"
    sets.write_text(json.dumps(STYLES), "utf-8")
    styleset_install({"doc": str(path), "path": str(sets)})
    return path


def run(op: str, request: dict[str, Any]) -> dict[str, Any]:
    load_operations()
    reply = REGISTRY[op].run(request)
    assert isinstance(reply, Reply)
    return reply.result


def test_segment_parts_front_and_locate(tmp_path: Path) -> None:
    doc = str(book(tmp_path))
    parts = run("segment", {"doc": doc, "unit": "parts"})
    assert [(p["text"], p["chapters"]) for p in parts["parts"]] == [
        ("El invierno", 3),
        ("La huida", 3),
    ]
    assert (parts["front"]["first"], parts["front"]["last"]) == (1, 2)
    chapters = run("segment", {"doc": doc})["chapters"]
    assert len(chapters) == 6
    assert {c["part"] for c in chapters} == {p["id"] for p in parts["parts"]}
    assert all(c["signals"][0] == "chapter_style" for c in chapters)
    scenes = run("segment", {"doc": doc, "unit": "scenes"})["scenes"]
    assert all(s["words"] > 0 for s in scenes)  # no empty scene for a bare part heading
    located = run("locate", {"doc": doc})["paragraphs"]  # the first page of rows
    assert located[0]["part"] is None  # front matter
    assert located[2]["part"]["title"] == "El invierno"  # the part heading itself
    assert located[3]["part"]["id"] == chapters[0]["part"]


@pytest.mark.parametrize(("words", "count"), [(["apertura"], 0), (["  ", "CAPÍTULO"], 6)])
def test_style_words_from_json(tmp_path: Path, words: list[str], count: int) -> None:
    doc = str(book(tmp_path))
    found = run("segment", {"doc": doc, "profile": {"chapter_style_words": words}})
    assert sum(1 for c in found["chapters"] if "chapter_style" in c["signals"]) == count
