"""note.add: a tracked footnote or endnote at a body offset (STORIES T7)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from tests.corpus.generator import generate
from tests.corpus.validator import soffice_path
from tests.helpers import soffice_dir

TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NEEDLE = "Added by the test"
ALL = ["body", "notes"]


def _run(name: str, request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = api.run(name, request).result
    return result


def _body_point(doc: Path, *, last: bool = True) -> str:
    """A point in a body paragraph. LibreOffice pairs marks and notes by id order, so the
    default is the end of the last long paragraph, after every existing mark."""
    rows = _run("paragraphs.list", {"doc": str(doc)})["paragraphs"]
    long = [r for r in rows if len(r["text"]) > 20]
    row = long[-1] if last else long[0]
    offset = len(row["text"]) if last else 9
    return f"{row['at']}:{offset}-{offset}"


def _odt(doc: Path, work: Path) -> ET.Element:
    soffice = soffice_path()
    assert soffice is not None
    subprocess.run(
        [
            soffice,
            f"-env:UserInstallation={(work / 'profile').resolve().as_uri()}",
            "--headless",
            "--norestore",
            "--convert-to",
            "odt",
            "--outdir",
            str(work),
            str(doc),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    with zipfile.ZipFile(work / f"{doc.stem}.odt") as zf:
        return ET.fromstring(zf.read("content.xml"))


def _notes_with(content: ET.Element, kind: str) -> list[ET.Element]:
    return [
        note
        for note in content.iter(TEXT + "note")
        if note.get(TEXT + "note-class") == kind and NEEDLE in "".join(note.itertext())
    ]


def _tracked_in_lo(content: ET.Element, note: ET.Element) -> bool:
    """LibreOffice puts the note between the start and end of one tracked insertion."""
    insertions = {
        r.get(TEXT + "id")
        for r in content.iter(TEXT + "changed-region")
        if r.find(TEXT + "insertion") is not None
    }
    parents = {child: parent for parent in content.iter() for child in parent}
    node = note
    while node in parents:  # LibreOffice may wrap the note in a span first
        kids = list(parents[node])
        at = kids.index(node)
        if 0 < at < len(kids) - 1 and kids[at - 1].tag == TEXT + "change-start":
            ident = kids[at - 1].get(TEXT + "change-id")
            end = kids[at + 1]
            return (
                end.tag == TEXT + "change-end"
                and end.get(TEXT + "change-id") == ident
                and ident in insertions
            )
        node = parents[node]
    return False


def _lo_python() -> str | None:
    """A Python that can drive LibreOffice (``import uno``), if the machine has one."""
    # The project's own interpreter has no uno; LibreOffice's goes with the system Python.
    for exe in (shutil.which("python3"), "/usr/bin/python3"):
        if exe and subprocess.run([exe, "-c", "import uno"], capture_output=True).returncode == 0:
            return exe
    return None


def _lo_rejected(doc: Path, work: Path) -> ET.Element | None:
    """The document after LibreOffice rejects every tracked change, or None without uno."""
    python = _lo_python()
    if python is None:
        return None
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]
    dest = work / "rejected.odt"
    subprocess.run(
        [python, str(Path(__file__).with_name("lo_reject_all.py")), str(doc), str(dest),
         str(work / "reject-profile"), str(port)],
        check=True,
        capture_output=True,
        timeout=300,
    )  # fmt: skip
    with zipfile.ZipFile(dest) as zf:
        return ET.fromstring(zf.read("content.xml"))


def _package_xml(doc: Path, name: str) -> ET.Element | None:
    with zipfile.ZipFile(doc) as zf:
        return ET.fromstring(zf.read(name)) if name in zf.namelist() else None


@pytest.mark.skipif(soffice_path() is None, reason="LibreOffice not installed")
@pytest.mark.parametrize("corpus", ["notes", "plain"])
@pytest.mark.parametrize("kind", ["footnote", "endnote"])
def test_note_add_tracked_and_rejectable(tmp_path: Path, corpus: str, kind: str) -> None:
    doc = tmp_path / f"{corpus}.docx"
    doc.write_bytes(generate(corpus, 0))
    original = doc.read_bytes()
    part = f"word/{kind}s.xml"
    assert (part in zipfile.ZipFile(doc).namelist()) == (corpus == "notes")

    request = {"doc": str(doc), "at": _body_point(doc), "kind": kind, "text": NEEDLE}
    note = _run("note.add", request)["note"]
    prefix = "fn" if kind == "footnote" else "en"
    assert note["at"].startswith(f"{prefix}{note['id']}/p1#")
    assert note["number"]
    assert part in zipfile.ZipFile(doc).namelist()

    # The mark (in the body) and the text (in the note) are both listed as tracked insertions.
    rows = _run("revisions.list", {"doc": str(doc), "stories": ALL})["revisions"]
    ours = [r for r in rows if r["at"].startswith(f"{prefix}{note['id']}/")]
    assert {r["type"] for r in ours} == {"insert"}
    assert any("/" not in r["at"] and r["type"] == "insert" for r in rows)
    listed = _run("paragraphs.list", {"doc": str(doc), "stories": ALL})["paragraphs"]
    assert any(r["at"] == note["at"] and NEEDLE in r["text"] for r in listed)

    with soffice_dir(tmp_path) as work:
        content = _odt(doc, work)
        shown = _notes_with(content, kind)
        assert len(shown) == 1
        assert _tracked_in_lo(content, shown[0])
        # LibreOffice rejecting the insertion takes out the mark and the note.
        rejected = _lo_rejected(doc, work)
    if rejected is not None:
        assert _notes_with(rejected, kind) == []
        assert NEEDLE not in "".join(rejected.itertext())
        before = sum(1 for n in content.iter(TEXT + "note") if n.get(TEXT + "note-class") == kind)
        left = sum(1 for n in rejected.iter(TEXT + "note") if n.get(TEXT + "note-class") == kind)
        assert left == before - 1

    # Our own reject-all takes out the mark and the note's text too.
    _run("batch", {"doc": str(doc), "ops": [{"op": "revision.reject", "all": True}]})
    body = _package_xml(doc, "word/document.xml")
    assert body is not None
    marks = [e for e in body.iter() if e.tag == W + f"{kind}Reference"]
    assert note["id"] not in {e.get(W + "id") for e in marks}
    listed = _run("paragraphs.list", {"doc": str(doc), "stories": ALL})["paragraphs"]
    assert not any(NEEDLE in r["text"] for r in listed)
    _run("undo", {"doc": str(doc)})

    # Undo restores the file byte for byte, also when the write created the notes part.
    _run("undo", {"doc": str(doc)})
    assert doc.read_bytes() == original


def test_note_add_refusals(tmp_path: Path) -> None:
    doc = tmp_path / "plain.docx"
    doc.write_bytes(generate("plain", 0))
    original = doc.read_bytes()
    at = _body_point(doc)
    story = "fn1/p1#00000000:0-0"
    cases = (
        ({"at": story, "kind": "footnote", "text": "x"}, ErrorCode.UNSUPPORTED_CONTENT),
        ({"at": "p1", "kind": "footnote", "text": "x"}, ErrorCode.ADDRESS_INVALID),
        ({"at": at, "kind": "footnote", "text": "  "}, ErrorCode.SCHEMA_INVALID),
        ({"at": at, "kind": "footnote", "text": "two\nlines"}, ErrorCode.SCHEMA_INVALID),
    )
    for extra, code in cases:
        with pytest.raises(DocControlError) as err:
            api.run("note.add", {"doc": str(doc), **extra})
        assert err.value.code is code, extra
        assert doc.read_bytes() == original


def test_note_add_before_existing_marks_warns_and_undo_needs_an_untouched_file(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "notes.docx"
    doc.write_bytes(generate("notes", 0))
    original = doc.read_bytes()
    request = {"doc": str(doc), "at": _body_point(doc, last=False), "kind": "footnote"}
    request["text"] = NEEDLE

    preview = api.run("note.add", {**request, "dry_run": True})
    assert doc.read_bytes() == original
    assert preview.result["note"]["at"].startswith("fn5/")
    assert [w.split(":")[0] for w in preview.warnings] == ["NOTE_ORDER"]

    # Notes are found by id, so the new one takes the next free id wherever its mark is.
    assert api.run("note.add", request).result["note"]["id"] == 5
    after_add = doc.read_bytes()
    listed = _run("paragraphs.list", {"doc": str(doc), "stories": ALL})["paragraphs"]
    assert [r["text"] for r in listed if r["at"].startswith("fn5/")] == [" " + NEEDLE]

    # Undo of the note refuses once the file was edited since; undoing the edit first works.
    body = _run("paragraphs.list", {"doc": str(doc)})["paragraphs"]
    edit = {"op": "text.replace", "at": body[1]["at"] + ":0-3", "text": "Zzz"}
    _run("batch", {"doc": str(doc), "ops": [edit]})
    with pytest.raises(DocControlError) as err:
        _run("undo", {"doc": str(doc), "op_id": "h-000001"})
    assert err.value.code is ErrorCode.BATCH_CONFLICT
    _run("undo", {"doc": str(doc)})
    assert doc.read_bytes() == after_add
    _run("undo", {"doc": str(doc)})
    assert doc.read_bytes() == original
