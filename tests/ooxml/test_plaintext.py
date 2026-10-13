"""Plain text and Markdown read as documents: paragraph rules, read ops, refused writes."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.plaintext import decode, paragraphs

_WRAPPED = (
    "Title\n\n\n\nCHAPTER I\n\n\n"
    "It was a dark and stormy night; the rain fell in\n"
    "torrents, except at occasional intervals, when it\n"
    "was checked by a violent gust of wind which swept\n"
    "up the streets.\n\nNext.\n"
)


def test_blocks_join_blocks_and_keeps_extra_blank_lines() -> None:
    paras, mode = paragraphs(_WRAPPED, markdown=False)
    assert mode == "blocks"
    assert [t for t, _ in paras] == [
        "Title",
        "",
        "",
        "CHAPTER I",
        "",
        "It was a dark and stormy night; the rain fell in torrents, except at occasional "
        "intervals, when it was checked by a violent gust of wind which swept up the streets.",
        "Next.",
    ]


def test_text_without_empty_lines_has_a_paragraph_per_line() -> None:
    verse = "".join(f"Line {n} of a poem whose lines are all alike\r\n" for n in range(14))
    text, encoding = decode(verse.encode())
    assert encoding == "utf-8"
    paras, mode = paragraphs(text, markdown=False)
    assert mode == "lines"
    assert len(paras) == 14 and paras[13] == ("Line 13 of a poem whose lines are all alike", None)


def test_markdown_lists_front_matter_and_fences() -> None:
    text = (
        "---\nnot: yaml? it is\n---\nIt began in\n1914. The war\n\n1. one\n2. two\n"
        "\n```py\n```x\n# code\n```\n"
    )
    paras, _ = paragraphs(text, markdown=True)
    assert [t for t, _ in paras] == [
        "It began in 1914. The war",
        "1. one",
        "2. two",
        "```py",
        "```x",
        "# code",
        "```",
    ]
    kept, _ = paragraphs("---\nIntro text.\n\n---\nMore.\n", markdown=True)
    assert [t for t, _ in kept] == ["---", "Intro text.", "---", "More."]


@pytest.mark.parametrize(
    ("raw", "encoding"),
    [
        (b"\xef\xbb\xbfa\xc3\xb1o", "utf-8-sig"),
        ("﻿año".encode("utf-16-le"), "utf-16"),
        ("año".encode("cp1252"), "cp1252"),
        ("año".encode("utf-32"), "utf-32"),
    ],
)
def test_encodings(raw: bytes, encoding: str) -> None:
    assert decode(raw) == ("año", encoding)


def test_one_stray_byte_garbles_only_its_line() -> None:
    raw = "canción\n".encode() + "año\n".encode("cp1252") + b"x\x81\n"
    assert decode(raw) == ("canción\naño\nx\x81\n", "utf-8+cp1252")


def test_utf16_without_bom_is_refused() -> None:
    with pytest.raises(ValueError, match="UTF-16"):
        decode("año".encode("utf-16-le"))


def test_markdown_headings_breaks_and_inline_markers() -> None:
    text = (
        "---\ntitle: x\n---\n# Part *One* #\n\n## 1\nShe *said* **no**, \\*really\\*.\n"
        "snake_case stays.\n\n* * *\n\n---\n\n~~~\n\n***x***\n# C#\n"
        "- item one\n- item two\n\n```\n# not a heading *x*\n```\nA \\_b\\_ \U000f0041.\n"
    )
    paras, mode = paragraphs(text, markdown=True)
    assert mode == "markdown"
    assert paras == [
        ("Part One", 1),
        ("1", 2),
        ("She said no, *really*. snake_case stays.", None),
        ("* * *", None),
        ("---", None),
        ("~~~", None),
        ("x", None),
        ("C#", 1),
        ("- item one", None),
        ("- item two", None),
        ("```", None),
        ("# not a heading *x*", None),
        ("```", None),
        ("A _b_ \U000f0041.", None),
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


def test_bomless_utf16_is_not_a_document(tmp_path: Path) -> None:
    path = tmp_path / "wide.txt"
    path.write_bytes("año".encode("utf-16-le"))
    with api.open(path) as d, pytest.raises(DocControlError) as err:
        d.run("info")
    assert err.value.code is ErrorCode.NOT_A_DOCX


def test_text_source_gate(tmp_path: Path) -> None:
    """The gate ``text_source_mismatches == 0``: reads of .txt/.md agree with the rules."""
    path = _novel(tmp_path)
    txt = tmp_path / "plain.txt"
    txt.write_text(_WRAPPED, "utf-8")
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
        doc = d.reply("info").doc or {}
        checks += [
            texts == [t for t, _ in paragraphs(_WRAPPED, markdown=False)[0]],
            texts[3] == "CHAPTER I" and texts[5].endswith("swept up the streets."),
            [c["text"] for c in found["chapters"]] == ["CHAPTER I"],
            doc.get("source") == {"format": "txt", "encoding": "utf-8", "mode": "blocks"},
        ]
    # compare reads text sources; its output is never a text file, and none is left behind.
    api.run("compare", {"old": str(txt), "new": str(path), "output": str(tmp_path / "r.docx")})
    checks.append((tmp_path / "r.docx").is_file())
    try:
        api.run("compare", {"old": str(path), "new": str(txt), "output": str(tmp_path / "r.md")})
        checks.append(False)
    except DocControlError as exc:
        checks.append(exc.code is ErrorCode.UNSUPPORTED_CONTENT)
    checks.append(sorted(p.name for p in tmp_path.iterdir()) == ["novel.md", "plain.txt", "r.docx"])
    mismatches = checks.count(False)
    print(f"text_source_mismatches == {mismatches} (of {len(checks)} checks)")
    assert mismatches == 0


def test_edge_empty_lines_keep_line_mode() -> None:
    paras, mode = paragraphs("\na\nb\nc\n\n", markdown=False)
    assert mode == "lines"
    assert [t for t, _ in paras] == ["", "a", "b", "c", ""]


def test_emphasis_is_linear_and_unwinds_nesting() -> None:
    paras, _ = paragraphs("a *b " * 60000 + "\n\n*a \\* b* and **a *b* c**\n", markdown=True)
    assert paras[-1] == ("a * b and a b c", None)
