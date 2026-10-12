"""`text.normalized` as a document read operation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import build_index
from doc_control.model.normalized import normalize
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import KINDS
from tests.corpus.generator import generate as generate_docx
from tests.helpers import make_docx


def run(capsys: pytest.CaptureFixture[str], **request: Any) -> tuple[int, dict[str, Any]]:
    code = main(["text.normalized", "--json", json.dumps(request)])
    return code, json.loads(capsys.readouterr().out)


def collect(
    capsys: pytest.CaptureFixture[str], pages: int = 1000, **request: Any
) -> list[dict[str, Any]]:
    """Follow `next` cursors to the end (or `pages` replies); returns the paragraph entries."""
    out: list[dict[str, Any]] = []
    for _ in range(pages):
        code, env = run(capsys, **request)
        assert code == 0, env
        out += env["result"]["paragraphs"]
        cursor = env["next"]
        if cursor is None:
            break
        request["cursor"] = cursor
    return out


def write_corpus(tmp_path: Path, kind: str, seed: int) -> Path:
    path = tmp_path / f"{kind}-{seed}.docx"
    path.write_bytes(generate_docx(kind, seed))
    return path


@pytest.mark.parametrize("kind", KINDS)
def test_whole_document_matches_the_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    path = write_corpus(tmp_path, kind, 1)
    index = build_index(_document_xml(path))
    entries = collect(capsys, pages=4, doc=str(path))  # a novel is thousands of paragraphs
    assert [e["at"] for e in entries] == [str(p.address) for p in index.paragraphs][: len(entries)]
    for entry, para in zip(entries, index.paragraphs, strict=False):
        expected = normalize(para.text)
        assert entry["text"] == expected.text
        assert entry["offset_map"]["to_original"] == list(expected.to_original)
        assert entry["offset_map"]["to_normalized"] == list(expected.to_normalized)


def _document_xml(path: Path) -> bytes:
    from doc_control.ooxml.zipio import Package

    return Package(path).read("word/document.xml")


def test_cap_and_cursor_cover_every_paragraph_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "caf\u00e9\u00ad au\u00a0lait " * 6
    path = make_docx(tmp_path / "long.docx", *[text] * 120)
    code, env = run(capsys, doc=str(path))
    assert code == 0
    assert (
        len(
            json.dumps(
                env["result"]["paragraphs"], ensure_ascii=False, separators=(",", ":")
            ).encode()
        )
        <= 8000
    )
    cursor = env["next"]
    assert cursor is not None
    assert cursor.startswith("c:text.normalized:") and cursor.count(":") == 4
    assert len(collect(capsys, doc=str(path))) == 120


def test_limit_and_stale_cursor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = make_docx(tmp_path / "a.docx", "one", "two", "three")
    code, env = run(capsys, doc=str(path), limit=2)
    assert code == 0 and len(env["result"]["paragraphs"]) == 2
    cursor = env["next"]
    make_docx(path, "one", "two", "changed")
    code, env = run(capsys, doc=str(path), cursor=cursor)
    assert code == 1 and env["error"]["code"] == ErrorCode.DOC_CHANGED_ON_DISK
    code, env = run(capsys, doc=str(path), cursor="nonsense")
    assert code == 2 and env["error"]["code"] == ErrorCode.SCHEMA_INVALID


def test_paragraph_and_range_addresses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = make_docx(tmp_path / "r.docx", "alpha  beta", "gamma­x", "delta")
    index = build_index(_document_xml(path))
    p1, p2, p3 = (str(p.address) for p in index.paragraphs)

    code, env = run(capsys, doc=str(path), at=p2)
    only = env["result"]["paragraphs"]
    assert code == 0 and [e["text"] for e in only] == ["gammax"]

    code, env = run(capsys, doc=str(path), at=f"{p1}:2-9")
    entry = env["result"]["paragraphs"][0]
    assert entry["start"] == 2 and entry["text"] == normalize("pha  be").text

    code, env = run(capsys, doc=str(path), at=f"{p1}:6..{p3}:3")
    got = env["result"]["paragraphs"]
    assert [e["text"] for e in got] == [" beta", "gammax", "del"]
    assert [e["start"] for e in got] == [6, 0, 0]


def test_stale_and_invalid_addresses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = make_docx(tmp_path / "s.docx", "alpha", "beta")
    code, env = run(capsys, doc=str(path), at="p1#00000000")
    assert code == 1 and env["error"]["code"] == ErrorCode.STALE_ADDRESS
    code, env = run(capsys, doc=str(path), at="nope")
    assert env["error"]["code"] == ErrorCode.ADDRESS_INVALID


def test_exactly_one_of_doc_and_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = make_docx(tmp_path / "x.docx", "alpha")
    for request in ({}, {"doc": str(path), "text": "a"}, {"text": "a", "at": "p1#00000000"}):
        code, env = run(capsys, **request)
        assert code == 2 and env["error"]["code"] == ErrorCode.SCHEMA_INVALID
    code, env = run(capsys, text="a­b  c")
    assert code == 0 and env["result"]["text"] == "ab c"


def test_missing_document_is_doc_not_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, env = run(capsys, doc=str(tmp_path / "missing.docx"))
    assert code == 1 and env["error"]["code"] == ErrorCode.DOC_NOT_FOUND


def test_registered_read_only_with_schema() -> None:
    op = load_operations()["text.normalized"]
    assert op is REGISTRY["text.normalized"] and op.kind == "read"
    with pytest.raises(DocControlError):
        op.run({"doc": "a.docx", "limit": 0})
