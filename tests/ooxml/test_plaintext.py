"""Plain text and Markdown read as documents: paragraph rules, read ops, refused writes."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.plaintext import decode, paragraphs


def test_blocks_join_wrapped_lines_and_keep_extra_blank_lines() -> None:
    text = "Title\n\n\n\nCHAPTER I\n\n\nIt was a dark\nand stormy night.\n\nNext.\n"
    assert paragraphs(text, markdown=False) == [
        ("Title", None),
        ("", None),
        ("", None),
        ("CHAPTER I", None),
        ("", None),
        ("It was a dark and stormy night.", None),
        ("Next.", None),
    ]


def test_without_empty_lines_each_line_is_a_paragraph() -> None:
    assert paragraphs(decode(b"One.\nTwo.\r\nThree.\rFour."), markdown=False) == [
        ("One.", None),
        ("Two.", None),
        ("Three.", None),
        ("Four.", None),
    ]


def test_leading_empty_line_is_a_paragraph() -> None:
    assert paragraphs("\nA\n\nB", markdown=False) == [("", None), ("A", None), ("B", None)]


def test_markdown_headings_breaks_and_inline_markers() -> None:
    text = (
        "# Part *One* #\n\n## 1\nShe *said* **no**, \\*really\\*.\n"
        "snake_case and_this stay.\n\n* * *\n\n---\n\n***x***\n#nospace\n"
    )
    assert paragraphs(text, markdown=True) == [
        ("Part One", 1),
        ("1", 2),
        ("She said no, *really*. snake_case and_this stay.", None),
        ("* * *", None),
        ("---", None),
        ("x #nospace", None),
    ]


def test_txt_is_not_markdown(tmp_path: Path) -> None:
    doc = tmp_path / "a.txt"
    doc.write_text("# not a heading\n*kept*\n", encoding="utf-8")
    with api.open(doc) as d:
        rows = d.paragraphs()
    assert [(r["text"], r["heading_level"]) for r in rows] == [
        ("# not a heading", None),
        ("*kept*", None),
    ]


def _novel(tmp_path: Path) -> Path:
    body = " ".join(["word"] * 400)
    text = "\n\n".join(
        ["# Part One", "## 1", body, "* * *", body, "## 2", body, "# Part Two", "## 3", body]
    )
    path = tmp_path / "novel.md"
    path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode())
    return path


def test_read_ops_see_the_text_as_a_document(tmp_path: Path) -> None:
    path = _novel(tmp_path)
    with api.open(path) as d:
        reply = d.reply("info")
        assert reply.doc is not None
        assert reply.doc["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        rows = d.paragraphs()
        assert rows[0]["style_name"] == "heading 1"
        assert rows[0]["text"] == "Part One"  # the byte order mark is gone
        result = d.run("segment", {"unit": "chapters", "profile": {"chapter_level": "auto"}})
        chapters = result["chapters"]
        assert [c["text"] for c in chapters] == ["1", "2", "3"]
        assert chapters[0]["scenes"] == 2
        assert len({c["part"] for c in chapters}) == 2
        hits = d.run("find", {"query": "Part Two"})["matches"]
        assert len(hits) == 1
        anchor = d.run("anchor.create", {"at": hits[0]["at"]})["anchor"]
        assert d.run("anchor.resolve", {"anchor": anchor})["match"] == "exact"
    assert not any(p.name != path.name for p in tmp_path.iterdir())


@pytest.mark.parametrize(
    ("op", "request_"),
    [
        ("open", {}),
        ("batch", {"ops": [{"op": "para.delete", "at": "p1"}]}),
        ("recipe.run", {"recipe": "empty-headings"}),
    ],
)
def test_writes_are_refused_and_leave_nothing(
    tmp_path: Path, op: str, request_: dict[str, object]
) -> None:
    path = _novel(tmp_path)
    before = path.read_bytes()
    with api.open(path, store=tmp_path / "store") as d, pytest.raises(DocControlError) as err:
        d.run(op, request_)
    assert err.value.code in (ErrorCode.UNSUPPORTED_CONTENT, ErrorCode.SCHEMA_INVALID)
    if err.value.code is ErrorCode.SCHEMA_INVALID:
        pytest.fail(f"request for {op} is malformed: {err.value}")
    assert path.read_bytes() == before
    assert not (tmp_path / "store").exists()


def test_non_utf8_text_is_not_a_document(tmp_path: Path) -> None:
    path = tmp_path / "latin.txt"
    path.write_bytes("año".encode("latin-1"))
    with api.open(path) as d, pytest.raises(DocControlError) as err:
        d.run("info")
    assert err.value.code is ErrorCode.NOT_A_DOCX


def test_text_source_gate(tmp_path: Path) -> None:
    """The gate ``text_source_mismatches == 0``: reads of .txt/.md agree with the rules."""
    path = _novel(tmp_path)
    txt = tmp_path / "plain.txt"
    txt.write_text("TITLE\n\n\n\nCHAPTER I\n\n\nIt was a dark\nand stormy night.\n", "utf-8")
    checks: list[bool] = []
    with api.open(path) as d:
        rows = d.paragraphs()
        chapters = d.run("segment", {"unit": "chapters", "profile": {"chapter_level": "auto"}})
        checks += [
            rows[0]["text"] == "Part One" and rows[0]["heading_level"] == 1,
            rows[1]["heading_level"] == 2,
            [c["text"] for c in chapters["chapters"]] == ["1", "2", "3"],
            chapters["chapters"][0]["scenes"] == 2,
        ]
        for op in ("open", "recipe.run"):
            req = {"recipe": "empty-headings"} if op == "recipe.run" else {}
            try:
                d.run(op, req)
                checks.append(False)
            except DocControlError as exc:
                checks.append(exc.code is ErrorCode.UNSUPPORTED_CONTENT)
    with api.open(txt) as d:
        texts = [r["text"] for r in d.paragraphs()]
        found = d.run("segment", {"unit": "chapters", "profile": {"chapter_blank_lines": 2}})
        checks += [
            texts == ["TITLE", "", "", "CHAPTER I", "", "It was a dark and stormy night."],
            [c["text"] for c in found["chapters"]] == ["CHAPTER I"],
        ]
    checks.append(sorted(p.name for p in tmp_path.iterdir()) == ["novel.md", "plain.txt"])
    mismatches = checks.count(False)
    print(f"text_source_mismatches == {mismatches} (of {len(checks)} checks)")
    assert mismatches == 0
