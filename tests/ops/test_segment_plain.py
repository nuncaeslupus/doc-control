"""`doc.segment` on plain text: blank-line headings, ``chapter_level: auto``, every break (T50).

Gate: ``plain_structure_mismatches == 0``: an unstyled .docx with headings set off by
blank lines gives its chapters with ``chapter_blank_lines``; ``auto`` finds level-2
chapters; the in-process API lists every suppressed scene break, the CLI the first 20; a
hard-wrapped ``-XIV-`` with its title line right under it is a heading with that title (T51).
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from doc_control import api
from doc_control.envelope import Reply
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import CONTENT_TYPES, RELS

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
BODY = "La lluvia cayó sobre el pueblo durante toda la noche y nadie salió a la calle."


def docx(tmp: Path, texts: list[str]) -> str:
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" if t else "<w:p/>" for t in texts)
    path = tmp / "plain.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
        )
    return str(path)


def run(request: dict[str, Any]) -> dict[str, Any]:
    load_operations()
    reply = REGISTRY["doc.segment"].run(request)
    assert isinstance(reply, Reply)
    return reply.result


def plain(chapters: int) -> list[str]:
    texts: list[str] = []
    for n in range(1, chapters + 1):
        texts += ["", "", "", f"-{n}-", "El camino", "", BODY, "", BODY, "* * *", "* * *", BODY]
    return texts


def test_plain_text_structure(tmp_path: Path) -> None:
    doc = docx(tmp_path, plain(25))
    default = run({"doc": doc})["chapters"]
    found = run({"doc": doc, "profile": {"chapter_blank_lines": 2}})["chapters"]
    auto = run({"doc": doc, "profile": {"chapter_level": "auto"}})
    capped = run({"doc": doc, "unit": "scenes", "profile": {"chapter_blank_lines": 2}})
    with api.open(doc) as opened:
        whole = opened.run("doc.segment", {"unit": "scenes", "profile": {"chapter_blank_lines": 2}})
    title = "Donde se cuenta lo que pasó en la casa del molino"
    hard = [r for n in ("XII", "XIII", "XIV") for r in ("", "", "", f"-{n}-", title, BODY, BODY)]
    wrapped = run({"doc": docx(tmp_path, hard), "profile": {"chapter_blank_lines": 2}})["chapters"]
    checks = [
        default == [],
        len(found) == 25,
        all(c["heading_last"] == c["first"] + 1 for c in found),
        {c["kind"] for c in found} == {"numeral"},
        auto["chapters"] == [],  # no heading styles in use: auto is level 1
        capped["suppressed_total"] == 25 and len(capped["suppressed"]) == 20,
        len(whole["suppressed"]) == 25,
        [(c["kind"], c["heading_last"] - c["first"]) for c in wrapped] == [("numeral", 1)] * 3,
    ]
    mismatches = checks.count(False)
    print(f"plain_structure_mismatches == {mismatches} (of {len(checks)} checks)")
    assert mismatches == 0
