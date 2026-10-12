"""Writer-notes scenario: a marker paragraph ``========== Move this after chapter X``.

The gate ``writer_note_scenario_failures == 0``. Applying the note uses only public
operations, called through the CLI: ``find`` locates the marker, ``segment`` gives the
chapters, and one tracked ``batch`` moves the passage after chapter X, deletes the marker
paragraph and (optionally) comments on the chapter heading. Rejecting every revision gives
the original back, accepting them gives the moved order, and every written package passes
the validator.
"""

from __future__ import annotations

import json
import random
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.model.document import Document
from doc_control.registry import load_operations
from tests.corpus.validator import require_soffice, validate_documents
from tests.helpers import make_docx
from tests.ops.revsim import resolved

MARKER = re.compile(r"^=+\s*Move this after chapter (\d+)\s*$")
MARKER_QUERY = r"^=+\s*Move this after chapter \d+\s*$"
BODY = "La lluvia cayó sobre el pueblo durante toda la noche y nadie salió a la calle."
AUTHOR = "Claude"

Capsys = pytest.CaptureFixture[str]


@pytest.fixture(autouse=True)
def _ops() -> None:
    load_operations()


def book(chapters: int, per_chapter: int, marker_in: int, offset: int, note_for: int) -> list[str]:
    """Chapters of ``per_chapter`` paragraphs; the marker sits ``offset`` paragraphs into
    chapter ``marker_in``, followed by the passage (the paragraphs after it)."""
    texts: list[str] = []
    for c in range(1, chapters + 1):
        texts.append(f"Capítulo {c}")
        body = [f"{BODY} Texto {c}.{k}." for k in range(per_chapter)]
        if c == marker_in:
            body.insert(offset, f"========== Move this after chapter {note_for}")
        texts += body
    return texts


def cli(capsys: Capsys, op: str, **request: Any) -> dict[str, Any]:
    """Run one operation through the public CLI and return its JSON envelope."""
    capsys.readouterr()
    main([op, "--json", json.dumps(request)])
    return json.loads(capsys.readouterr().out)  # type: ignore[no-any-return]


def document_xml(path: Path) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml")


def states(path: Path) -> tuple[list[str], list[str], list[str]]:
    data = document_xml(path)
    return (
        resolved(data, AUTHOR, "reject"),
        resolved(data, AUTHOR, "accept"),
        resolved(data, None, "current"),
    )


def apply_note(capsys: Capsys, path: Path, passage_len: int, with_comment: bool) -> dict[str, Any]:
    """Find the marker, resolve chapter X and the passage, apply one tracked batch."""
    found = cli(capsys, "find", doc=str(path), query=MARKER_QUERY, regex=True)
    assert found["ok"] and len(found["result"]["matches"]) == 1
    match = found["result"]["matches"][0]
    marker_at = match["at"].split(":")[0]
    note = MARKER.fullmatch(match["text"])
    assert note is not None
    wanted = int(note.group(1))

    chapters = cli(capsys, "segment", doc=str(path), unit="chapters")["result"]["chapters"]
    target = next(c for c in chapters if c["number"] == wanted)
    doc = Document(str(path))
    marker = next(p for p in doc.index.paragraphs if str(p.address) == marker_at)
    first = str(doc.index.paragraph(marker.number + 1).address)
    last = str(doc.index.paragraph(marker.number + passage_len).address)
    after = str(doc.index.paragraph(target["last"]).address)

    ops: list[dict[str, Any]] = [
        {"op": "para.move", "at": f"{first}..{last}", "after": after},
        {"op": "para.delete", "at": marker_at},
    ]
    if with_comment:
        ops.append({"op": "comment.add", "at": target["at"], "text": "Moved as the writer asked."})
    return cli(capsys, "batch", doc=str(path), ops=ops, author=AUTHOR)


def expected_after(texts: list[str], passage_len: int, wanted: int) -> list[str]:
    m = next(i for i, t in enumerate(texts) if MARKER.fullmatch(t))
    passage = texts[m + 1 : m + 1 + passage_len]
    rest = texts[:m] + texts[m + 1 + passage_len :]
    end = next((i for i, t in enumerate(rest) if t == f"Capítulo {wanted + 1}"), len(rest))
    return rest[:end] + passage + rest[end:]


Case = tuple[
    int, int, int, int, int, int, bool
]  # chapters, per, marker_in, offset, X, len, comment


def check_scenario(capsys: Capsys, tmp_path: Path, name: str, case: Case) -> Path:
    chapters, per, marker_in, offset, wanted, length, with_comment = case
    original = book(chapters, per, marker_in, offset, wanted)
    path = make_docx(tmp_path / f"{name}.docx", *original)
    envelope = apply_note(capsys, path, length, with_comment)
    assert envelope["ok"], envelope
    assert envelope["result"]["applied"] == (3 if with_comment else 2)
    reject, accept, current = states(path)
    assert reject == original
    assert accept == expected_after(original, length, wanted)
    assert not any(MARKER.fullmatch(t) for t in current)  # hidden: its deletion is a revision
    assert any(MARKER.fullmatch(t) for t in reject)
    assert not any(MARKER.fullmatch(t) for t in accept)
    data = document_xml(path)
    assert b"<w:del " in data and b"<w:ins " in data
    return path


def cases() -> list[Case]:
    out: list[Case] = [
        (5, 4, 2, 1, 4, 3, True),  # forward, with a comment on the chapter heading
        (5, 4, 4, 1, 2, 1, False),  # backward
        (4, 3, 2, 0, 1, 3, False),  # the marker opens its chapter; target is the previous one
        (6, 5, 1, 2, 5, 3, False),  # forward over several chapters
    ]
    rng = random.Random("writer-notes")
    for _ in range(24):  # fuzz over chapter numbers, positions and passage lengths
        chapters = rng.randint(3, 9)
        marker_in = rng.randint(1, chapters)
        per = rng.randint(2, 6)
        # The body's last paragraph cannot be deleted by the move: keep one after the passage.
        offset = rng.randint(0, per - (2 if marker_in == chapters else 1))
        wanted = rng.choice([c for c in range(1, chapters) if c != marker_in] or [1])
        out.append(
            (
                chapters,
                per,
                marker_in,
                offset,
                wanted,
                rng.randint(1, per - offset - (marker_in == chapters)),
                rng.random() < 0.4,
            )
        )
    return out


def test_marker_move_after_chapter_applies_as_tracked_batch(tmp_path: Path, capsys: Capsys) -> None:
    failures: list[str] = []
    docs: dict[str, bytes] = {}
    for i, case in enumerate(cases()):
        try:
            docs[f"note{i}"] = check_scenario(capsys, tmp_path, f"note{i}", case).read_bytes()
        except AssertionError as err:
            failures.append(f"case {i} {case}: {str(err)[:200]}")
    assert not failures, f"writer_note_scenario_failures == {len(failures)}: {failures}"
    # Every package the scenario wrote passes the structural lint and LibreOffice.
    require_soffice()
    problems = validate_documents(docs, tmp_path / "validate")
    assert {k: v for k, v in problems.items() if v} == {}


def test_moving_after_the_last_chapter_is_refused_cleanly(tmp_path: Path, capsys: Capsys) -> None:
    path = make_docx(tmp_path / "last.docx", *book(3, 3, 1, 1, 3))
    before = path.read_bytes()
    envelope = apply_note(capsys, path, 1, False)
    assert not envelope["ok"]
    assert path.read_bytes() == before
