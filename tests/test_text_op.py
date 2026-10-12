"""`text.normalized` as a document read operation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import build_index
from doc_control.model.normalized import decode_runs, normalize
from doc_control.ooxml.zipio import Package
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import KINDS
from tests.corpus.generator import generate as generate_docx
from tests.helpers import make_docx

CAP = 8000


def run(capsys: pytest.CaptureFixture[str], **request: Any) -> tuple[int, dict[str, Any]]:
    code = main(["text.normalized", "--json", json.dumps(request)])
    return code, json.loads(capsys.readouterr().out)


def collect(
    capsys: pytest.CaptureFixture[str], pages: int = 1000, **request: Any
) -> tuple[list[dict[str, Any]], bool]:
    """Follow `next` cursors to the end (or `pages` replies); every reply obeys the cap."""
    out: list[dict[str, Any]] = []
    for _ in range(pages):
        code, env = run(capsys, **request)
        assert code == 0, env
        assert len(json.dumps(env, ensure_ascii=False).encode()) <= CAP
        out += env["result"]["paragraphs"]
        if env["next"] is None:
            return out, True
        request["cursor"] = env["next"]
    return out, False


def assemble(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join the windows of each paragraph back into one text and two full maps."""
    merged: list[dict[str, Any]] = []
    for e in entries:
        key = e.get("at")
        if not merged or merged[-1]["at"] != key:
            merged.append({"at": key, "start": e["start"], "text": "", "to_o": {}, "to_n": {}})
        cur = merged[-1]
        assert e["from"] == len(cur["text"])
        cur["text"] += e["text"]
        for name, key2 in (("to_original", "to_o"), ("to_normalized", "to_n")):
            part = decode_runs(e["offset_map"][name])
            assert not part.keys() & cur[key2].keys()
            cur[key2].update(part)
    return merged


def check_against(merged: dict[str, Any], text: str) -> None:
    expected = normalize(text)
    assert merged["text"] == expected.text
    assert merged["to_o"] == dict(enumerate(expected.to_original))
    assert merged["to_n"] == dict(enumerate(expected.to_normalized))


def document_xml(path: Path) -> bytes:
    return Package(path).read("word/document.xml")


@pytest.mark.parametrize("kind", KINDS)
def test_whole_document_matches_the_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    path = tmp_path / f"{kind}.docx"
    path.write_bytes(generate_docx(kind, 1))
    index = build_index(document_xml(path))
    entries, complete = collect(capsys, pages=4, doc=str(path))  # a novel has thousands
    merged = assemble(entries)
    if not complete:
        merged.pop()  # the last paragraph may continue on the next page
    assert [m["at"] for m in merged] == [str(p.address) for p in index.paragraphs][: len(merged)]
    for m, para in zip(merged, index.paragraphs, strict=False):
        check_against(m, para.text)


def test_long_paragraph_is_split_and_every_reply_obeys_the_cap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    messy = "caf\u00e9\u00ad au\u00a0lait \u200bx" * 400  # 2,000+ characters, many removals
    clean = "plain words go here " * 120
    path = make_docx(tmp_path / "long.docx", messy, clean, *["short one"] * 300)
    entries, complete = collect(capsys, doc=str(path))
    assert complete
    merged = assemble(entries)
    assert len(merged) == 302
    check_against(merged[0], messy)
    check_against(merged[1], clean)
    assert sum(1 for e in entries if e["at"] == merged[0]["at"]) > 1  # split into windows


def test_clean_text_has_one_run_per_map(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = make_docx(tmp_path / "c.docx", "x" * 1500)
    code, env = run(capsys, doc=str(path))
    entry = env["result"]["paragraphs"][0]
    assert code == 0
    assert entry["offset_map"] == {
        "to_original": [[0, 0, 1501]],
        "to_normalized": [[0, 0, 1501]],
    }


def test_text_mode_is_capped_and_paged(capsys: pytest.CaptureFixture[str]) -> None:
    text = "ab\u00ad\u200b c\u0301  " * 1500
    entries, complete = collect(capsys, text=text)
    assert complete
    (merged,) = assemble(entries)
    check_against(merged, text)
    assert len(entries) > 1
    code, env = run(capsys, text="a\u00adb  c")
    assert code == 0 and env["result"]["paragraphs"][0]["text"] == "ab c"


def test_limit_and_stale_cursor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = make_docx(tmp_path / "a.docx", "one", "two", "three")
    code, env = run(capsys, doc=str(path), limit=2)
    assert code == 0 and len(env["result"]["paragraphs"]) == 2
    cursor = env["next"]
    assert cursor.startswith("c:text.normalized:")
    make_docx(path, "one", "two", "changed")
    code, env = run(capsys, doc=str(path), cursor=cursor)
    assert code == 1 and env["error"]["code"] == ErrorCode.DOC_CHANGED_ON_DISK
    code, env = run(capsys, doc=str(path), cursor="nonsense")
    assert code == 2 and env["error"]["code"] == ErrorCode.SCHEMA_INVALID


def test_paragraph_and_range_addresses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = make_docx(tmp_path / "r.docx", "alpha  beta", "gamma\u00adx", "delta")
    index = build_index(document_xml(path))
    p1, p2, p3 = (str(p.address) for p in index.paragraphs)

    code, env = run(capsys, doc=str(path), at=p2)
    assert code == 0 and [e["text"] for e in env["result"]["paragraphs"]] == ["gammax"]

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


def test_missing_document_is_doc_not_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, env = run(capsys, doc=str(tmp_path / "missing.docx"))
    assert code == 1 and env["error"]["code"] == ErrorCode.DOC_NOT_FOUND


def test_registered_read_only_with_schema() -> None:
    op = load_operations()["text.normalized"]
    assert op is REGISTRY["text.normalized"] and op.kind == "read"
    assert "to_normalized" in op.schema["description"]
    with pytest.raises(DocControlError):
        op.run({"doc": "a.docx", "limit": 0})


def test_a_page_of_a_novel_is_built_in_under_a_fifth_of_a_second(tmp_path: Path) -> None:
    path = tmp_path / "novel.docx"
    path.write_bytes(generate_docx("novel", 1))
    op = load_operations()["text.normalized"]
    first = op.run({"doc": str(path)})
    assert not isinstance(first, dict)
    cursor = first.next_cursor
    assert cursor is not None
    best = float("inf")
    for request in ({"doc": str(path)}, {"doc": str(path), "cursor": cursor}):
        started = time.perf_counter()
        op.run(request)
        best = min(best, time.perf_counter() - started)
    print(f"text_normalized_page_seconds={best:.3f}")
    assert best < 0.2
