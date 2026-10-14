"""Tracked edits and comments in notes, headers and footers (STORIES T6)."""

from __future__ import annotations

import re
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
STYLE = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
ALL = ["body", "notes", "headers"]
#: The stories each corpus kind is edited in: a footnote and an endnote, a header and a footer.
TARGETS = {"notes": ("fn2", "en1"), "headers": ("hdr2.default", "ftr1.default")}
_CHANGE_MARKS = (TEXT + "change", TEXT + "change-start", TEXT + "change-end")


def _run(name: str, request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = api.run(name, request).result
    return result


def _story(doc: Path, key: str) -> list[dict[str, Any]]:
    rows = _run("paragraphs.list", {"doc": str(doc), "stories": ALL})["paragraphs"]
    return [r for r in rows if r["at"].startswith(key + "/")]


def _batch(doc: Path, ops: list[dict[str, Any]]) -> dict[str, Any]:
    return _run("batch", {"doc": str(doc), "ops": ops})


def _revisions(doc: Path, key: str) -> list[dict[str, Any]]:
    rows = _run("revisions.list", {"doc": str(doc), "stories": ALL})["revisions"]
    return [r for r in rows if r["at"].startswith(key + "/")]


def _odt(doc: Path, work: Path) -> tuple[ET.Element, ET.Element]:
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
        return ET.fromstring(zf.read("content.xml")), ET.fromstring(zf.read("styles.xml"))


def _containers(content: ET.Element, styles: ET.Element, kind: str) -> list[ET.Element]:
    """Where LibreOffice keeps a story of ``kind``: note bodies, or master-page areas."""
    if kind in ("footnote", "endnote"):
        return [
            note.find(TEXT + "note-body")  # type: ignore[misc]
            for note in content.iter(TEXT + "note")
            if note.get(TEXT + "note-class") == kind
        ]
    tags = (STYLE + kind, STYLE + kind + "-left", STYLE + kind + "-first")
    return [e for e in styles.iter() if e.tag in tags]


def _text(elem: ET.Element) -> str:
    return "".join(elem.itertext())


def _marks_in(content: ET.Element, styles: ET.Element, kind: str, needle: str) -> dict[str, int]:
    """Tracked-change marks and comments inside the story of ``kind`` that holds ``needle``."""
    found = {"changes": 0, "comments": 0}
    for box in _containers(content, styles, kind):
        if needle not in _text(box):
            continue
        found["changes"] += sum(1 for e in box.iter() if e.tag in _CHANGE_MARKS)
        found["comments"] += sum(1 for _ in box.iter(OFFICE + "annotation"))
    return found


def _kind(key: str) -> str:
    return {"fn": "footnote", "en": "endnote", "hd": "header", "ft": "footer"}[key[:2]]


@pytest.mark.skipif(soffice_path() is None, reason="LibreOffice not installed")
def test_story_edits_round_trip_through_libreoffice(tmp_path: Path) -> None:
    failures: list[str] = []
    with soffice_dir(tmp_path) as work:
        for corpus, keys in TARGETS.items():
            doc = work / f"{corpus}.docx"
            doc.write_bytes(generate(corpus, 0))
            original = doc.read_bytes()

            # text.replace in each story; undo gives the file back byte for byte.
            replace_ops = [
                {"op": "text.replace", "at": _story(doc, k)[0]["at"] + ":1-4", "text": "ZZZ"}
                for k in keys
            ]
            _batch(doc, replace_ops)
            _run("undo", {"doc": str(doc)})
            if doc.read_bytes() != original:
                failures.append(f"{corpus}: undo of text.replace did not restore the file")
            _batch(doc, replace_ops)

            # para.insert after each story's first paragraph.
            _batch(
                doc,
                [
                    {"op": "para.insert", "after": _story(doc, k)[0]["at"], "text": f"New in {k}"}
                    for k in keys
                ],
            )

            # comment.add: footnotes take comments; headers and footers do not (Word's rule),
            # nor endnotes (LibreOffice 24.2 then drops the next endnote's text).
            for k in keys:
                at = _story(doc, k)[0]["at"] + ":5-9"
                if _kind(k) != "footnote":
                    before = doc.read_bytes()
                    with pytest.raises(DocControlError) as err:
                        _batch(doc, [{"op": "comment.add", "at": at, "text": "Check"}])
                    if err.value.code is not ErrorCode.UNSUPPORTED_CONTENT:
                        failures.append(f"{k}: comment.add gave {err.value.code}")
                    if doc.read_bytes() != before:
                        failures.append(f"{k}: a refused comment changed the file")
                    continue
                _batch(doc, [{"op": "comment.add", "at": at, "text": f"Note on {k}"}])
                rows = [
                    c for c in _run("comments.list", {"doc": str(doc)})["comments"]
                    if c["text"] == f"Note on {k}"
                ]  # fmt: skip
                if not rows or not rows[0].get("at", "").startswith(k + "/"):
                    failures.append(f"{k}: comments.list does not place the comment there")
                elif rows[0].get("story") != _kind(k):
                    failures.append(f"{k}: comments.list story is {rows[0].get('story')}")

            for k in keys:
                types = {r["type"] for r in _revisions(doc, k)}
                if not {"insert", "delete", "mark_insert"} <= types:
                    failures.append(f"{k}: revisions.list shows {sorted(types)}")

            content, styles = _odt(doc, work)
            for k in keys:
                got = _marks_in(content, styles, _kind(k), f"New in {k}")
                if got["changes"] < 3:  # deletion, insertion, inserted paragraph
                    failures.append(f"{k}: LibreOffice shows {got['changes']} change marks")
                if got["comments"] != (_kind(k) == "footnote"):
                    failures.append(f"{k}: LibreOffice shows {got['comments']} comments")

            # revision.accept over the story's range: the edits become plain text.
            for k in keys:
                rows = _story(doc, k)
                _batch(doc, [{"op": "revision.accept", "at": f"{rows[0]['at']}..{rows[1]['at']}"}])
                rows = _story(doc, k)
                if _revisions(doc, k) or rows[0]["text"][1:4] != "ZZZ":
                    failures.append(f"{k}: accepting left {_revisions(doc, k)}")
                if rows[1]["text"] != f"New in {k}":
                    failures.append(f"{k}: accepting lost the inserted paragraph")

            # para.delete of a middle paragraph (a note's first one holds its mark, the last
            # one of a story cannot lose its mark); undo, then redo and reject.
            _batch(
                doc,
                [
                    {"op": "para.insert", "after": _story(doc, k)[0]["at"], "text": f"Gone {k}"}
                    for k in keys
                ],
            )
            _batch(doc, [{"op": "revision.accept", "all": True}])
            accepted = {k: [r["text"] for r in _story(doc, k)] for k in keys}
            before_delete = doc.read_bytes()
            delete_ops = [{"op": "para.delete", "at": _story(doc, k)[1]["at"]} for k in keys]
            _batch(doc, delete_ops)
            content, styles = _odt(doc, work)
            for k in keys:
                if "delete" not in {r["type"] for r in _revisions(doc, k)}:
                    failures.append(f"{k}: no deletion listed after para.delete")
                # ponytail: LibreOffice 24.2 also pulls the next paragraph of a footnote into
                # a deleted paragraph's region, so the story is found by its edited first line.
                deleted = [
                    _text(region)
                    for tree in (content, styles)
                    for region in tree.iter(TEXT + "changed-region")
                    if region.find(TEXT + "deletion") is not None
                ]
                if _marks_in(content, styles, _kind(k), "ZZZ")["changes"] < 1 or not any(
                    f"Gone {k}" in text for text in deleted
                ):
                    failures.append(f"{k}: LibreOffice shows no deletion")
            _run("undo", {"doc": str(doc)})
            if doc.read_bytes() != before_delete:
                failures.append(f"{corpus}: undo of para.delete did not restore the file")
            _batch(doc, delete_ops)
            for k in keys:
                ids = [r["id"] for r in _revisions(doc, k)]
                _batch(doc, [{"op": "revision.reject", "ids": ids}])
                if [r["text"] for r in _story(doc, k)] != accepted[k]:
                    failures.append(f"{k}: rejecting the deletion did not bring it back")
    assert failures == []


def _addresses(doc: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in _run("paragraphs.list", {"doc": str(doc), "stories": ALL})["paragraphs"]:
        out.setdefault(row["at"].split("/")[0] if "/" in row["at"] else "body", row["at"])
    return out


def test_batch_across_parts_is_all_or_nothing(tmp_path: Path) -> None:
    doc = tmp_path / "notes.docx"
    doc.write_bytes(generate("notes", 0))
    original = doc.read_bytes()
    at = _addresses(doc)
    good = [
        {"op": "text.replace", "at": at["body"] + ":0-1", "text": "X"},
        {"op": "text.replace", "at": at["fn2"] + ":1-4", "text": "Y"},
    ]
    stale = re.sub(r"#[0-9a-f]{8}", "#00000000", at["en1"])
    for bad, code in (
        ({"op": "text.replace", "at": stale + ":1-2", "text": "Z"}, ErrorCode.STALE_ADDRESS),
        ({"op": "text.replace", "at": "fn99/p1#00000000", "text": "Z"}, ErrorCode.ANCHOR_NOT_FOUND),
        ({"op": "para.delete", "at": at["en1"]}, ErrorCode.UNSUPPORTED_CONTENT),  # its last
        ({"op": "para.move", "at": at["fn2"], "after": at["fn1"]}, ErrorCode.UNSUPPORTED_CONTENT),
    ):
        with pytest.raises(DocControlError) as err:
            _batch(doc, [*good, bad])
        assert err.value.code is code, bad
        assert doc.read_bytes() == original
    result = _batch(doc, good)
    assert set(result["footprint"]) == {"word/document.xml", "word/footnotes.xml"}
    assert len(set(result["revision_ids"])) == len(result["revision_ids"]) == 4


def test_two_notes_in_one_part_and_undo(tmp_path: Path) -> None:
    doc = tmp_path / "notes.docx"
    doc.write_bytes(generate("notes", 0))
    original = doc.read_bytes()
    at = _addresses(doc)
    result = _batch(
        doc,
        [
            {"op": "text.replace", "at": at["fn1"] + ":1-5", "text": "First"},
            {"op": "para.insert", "after": at["fn3"], "text": "Added to fn3"},
            {"op": "format.apply", "at": at["fn4"], "bold": True},
        ],
    )
    assert result["footprint"]["word/footnotes.xml"] == ["fn1/p1", "fn3/p1", "fn3/p2", "fn4/p1"]
    assert [r["text"] for r in _story(doc, "fn3")][1] == "Added to fn3"
    assert {r["type"] for r in _revisions(doc, "fn4")} == {"format"}
    _run("undo", {"doc": str(doc)})
    assert doc.read_bytes() == original


def test_live_batch_refuses_story_addresses(tmp_path: Path) -> None:
    doc = tmp_path / "notes.docx"
    doc.write_bytes(generate("notes", 0))
    at = _addresses(doc)["fn2"]
    with pytest.raises(DocControlError) as err:
        api.run(
            "batch",
            {
                "doc": str(doc),
                "engine": "live",
                "ops": [{"op": "text.replace", "at": at + ":1-2", "text": "x"}],
            },
        )
    assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE
    assert "file engine only" in err.value.message
