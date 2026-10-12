"""Read operations: contract, token caps and cursors, and that nothing is ever written."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.model.document import Document
from doc_control.ops import read
from doc_control.ops.paging import RESPONSE_BUDGET
from doc_control.registry import load_operations
from tests.corpus.generator import KINDS
from tests.corpus.private import private_documents, short_id
from tests.helpers import CONTENT_TYPES, RELS
from tests.ops.bigdoc import write_big_doc

READ_OPS = (
    "info",
    "outline",
    "stats",
    "para.get",
    "styles.list",
    "style.get",
    "comments.list",
    "revisions.list",
)
Run = Callable[..., tuple[int, dict[str, Any], int]]
CorpusWriter = Callable[..., Path]  # corpus_doc(kind, seed=0)


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Run:
    """``run(op, **request)`` -> (exit code, envelope, bytes printed)."""

    def call(op: str, **request: Any) -> tuple[int, dict[str, Any], int]:
        code = main([op, "--json", json.dumps(request)])
        out = capsys.readouterr().out
        return code, json.loads(out), len(out.rstrip("\n").encode("utf-8"))

    return call


def ok(run: Run, op: str, **request: Any) -> dict[str, Any]:
    code, env, _ = run(op, **request)
    assert code == 0, env
    return env


def pages(run: Run, op: str, key: str, **request: Any) -> list[dict[str, Any]]:
    """Every item of a list operation, following `next` to the end."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(10_000):
        extra = {"cursor": cursor} if cursor else {}
        env = ok(run, op, **request, **extra)
        items += env["result"][key]
        cursor = env["next"]
        if cursor is None:
            return items
    raise AssertionError("cursor never ended")


@pytest.fixture(scope="module")
def big_doc(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_big_doc(tmp_path_factory.mktemp("big") / "big.docx")


def test_read_ops_large_doc_responses_stay_under_budget(
    big_doc: Path, run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    """On the 100k-word document every default response is <= 8,000 bytes, with `next` when cut."""
    doc = Document(str(big_doc))
    assert sum(len(p.text.split()) for p in doc.index.paragraphs) >= 100_000
    longest = max(doc.index.paragraphs, key=lambda p: len(p.text))
    requests: list[tuple[str, str | None, dict[str, Any]]] = [
        ("info", None, {}),
        ("stats", None, {}),
        ("para.get", None, {"at": str(longest.address)}),
        ("style.get", None, {"style": "Heading1"}),
        ("outline", "headings", {}),
        ("styles.list", "styles", {}),
        ("comments.list", "comments", {}),
        ("revisions.list", "revisions", {}),
    ]
    sizes: list[int] = []
    cut_lists = 0
    for op, key, extra in requests:
        cursor: str | None = None
        collected = 0
        while True:
            req = {"doc": str(big_doc), **extra, **({"cursor": cursor} if cursor else {})}
            code, env, nbytes = run(op, **req)
            assert code == 0, env
            assert nbytes <= RESPONSE_BUDGET, (op, nbytes)
            sizes.append(nbytes)
            if key is not None:
                collected += len(env["result"][key])
                assert env["result"][key], "a page always advances"
                if collected < env["result"]["total"]:
                    assert env["next"] is not None, f"{op} truncated without next"
                    cut_lists += 1
                else:
                    assert env["next"] is None
            cursor = env["next"]
            if cursor is None:
                break
        if key is not None:
            assert collected == env["result"]["total"]
    assert cut_lists >= 3  # outline, comments and revisions all had to page
    tokens = sorted(math.ceil(n / 4) for n in sizes)
    p95 = tokens[math.ceil(0.95 * len(tokens)) - 1]
    with capsys.disabled():
        print(f"\ndefault_response_tokens_p95={p95}")
    assert p95 <= 2000


def test_read_ops_are_registered_as_read_with_schemas() -> None:
    registry = load_operations()
    for name in READ_OPS:
        assert registry[name].kind == "read"
        assert registry[name].schema["properties"]["doc"]["type"] == "string"
        assert "doc" in registry[name].schema["required"]


def test_read_ops_never_write_the_document(
    corpus_doc: CorpusWriter, run: Run, tmp_path: Path
) -> None:
    path = corpus_doc("kitchen_sink", 3)
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    listing = sorted(p.name for p in tmp_path.iterdir())
    para = ok(run, "outline", doc=str(path))["result"]["headings"][0]["at"]
    for op, extra in [
        ("info", {}),
        ("outline", {}),
        ("stats", {}),
        ("para.get", {"at": para}),
        ("styles.list", {}),
        ("style.get", {"style": "Normal"}),
        ("comments.list", {}),
        ("revisions.list", {}),
    ]:
        ok(run, op, doc=str(path), **extra)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in tmp_path.iterdir()) == listing  # no store, no backup


def test_info_reports_file_facts(corpus_doc: CorpusWriter, run: Run) -> None:
    path = corpus_doc("kitchen_sink")
    env = ok(run, "info", doc=str(path))
    result = env["result"]
    assert env["doc"]["paragraphs"] == len(Document(str(path)).index)
    assert len(env["doc"]["sha256"]) == 64
    assert result["size_bytes"] == path.stat().st_size
    assert result["properties"]["title"] == "synthetic kitchen_sink 0"
    assert result["has"]["comments"] and result["has"]["footnotes"] and result["has"]["styles"]
    assert result["has"]["headers"] == 0
    assert result["word_lock"] is False


def test_info_reports_word_lock(corpus_doc: CorpusWriter, run: Run) -> None:
    path = corpus_doc("plain")
    path.with_name("~$" + path.name).write_bytes(b"")
    assert ok(run, "info", doc=str(path))["result"]["word_lock"] is True


def test_outline_lists_headings_with_levels(corpus_doc: CorpusWriter, run: Run) -> None:
    path = corpus_doc("headings")
    headings = ok(run, "outline", doc=str(path))["result"]["headings"]
    assert [h["level"] for h in headings] == [1, 2, 3, 2]
    assert headings[0]["style"] == "Heading1"
    index = Document(str(path)).index
    for h in headings:  # the address resolves back to the same text
        assert index.resolve_address(h["at"]).text.startswith(h["text"][:20])
    shallow = ok(run, "outline", doc=str(path), max_level=2)["result"]
    assert [h["level"] for h in shallow["headings"]] == [1, 2, 2]


def _custom_docx(
    path: Path, document_body: str, styles: str, extra: dict[str, str] | None = None
) -> Path:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{document_body}</w:body></w:document>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"{styles}</w:styles>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles_xml)
        for name, xml in (extra or {}).items():
            zf.writestr(name, xml)
    return path


def _style(style_id: str, name: str, based: str = "", ppr: str = "", rpr: str = "") -> str:
    base = f'<w:basedOn w:val="{based}"/>' if based else ""
    default = ' w:default="1"' if style_id == "Normal" else ""
    return (
        f'<w:style w:type="paragraph"{default} w:styleId="{style_id}"><w:name w:val="{name}"/>'
        f"{base}<w:pPr>{ppr}</w:pPr><w:rPr>{rpr}</w:rPr></w:style>"
    )


def _para(text: str, ppr: str = "", runs: str | None = None) -> str:
    props = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    body = runs if runs is not None else f"<w:r><w:t>{text}</w:t></w:r>"
    return f"<w:p>{props}{body}</w:p>"


def test_outline_heading_level_follows_basedon_and_outline_levels(tmp_path: Path, run: Run) -> None:
    styles = "".join(
        [
            _style("Normal", "Normal"),
            _style("H1", "heading 1", "Normal", '<w:outlineLvl w:val="0"/>'),
            _style("Chapter", "Chapter", "H1"),  # level comes from its base
            _style("Part", "Part", "Chapter", '<w:outlineLvl w:val="1"/>'),
            _style("Body9", "Body", "H1", '<w:outlineLvl w:val="9"/>'),  # body text again
            _style("Heading3", "heading 3", "Normal"),  # by name, no outline level
            _style("Loop1", "Loop1", "Loop2"),
            _style("Loop2", "Loop2", "Loop1"),
        ]
    )
    body = "".join(
        [
            _para("Plain"),
            _para("Direct", '<w:outlineLvl w:val="3"/>'),
            _para("A chapter", '<w:pStyle w:val="Chapter"/>'),
            _para("A part", '<w:pStyle w:val="Part"/>'),
            _para("Not a heading", '<w:pStyle w:val="Body9"/>'),
            _para("By name", '<w:pStyle w:val="Heading3"/>'),
            _para("Cycle", '<w:pStyle w:val="Loop1"/>'),
            _para("Direct beats style", '<w:pStyle w:val="Chapter"/><w:outlineLvl w:val="4"/>'),
            _para("", '<w:pStyle w:val="H1"/>'),  # empty headings are not listed
        ]
    )
    path = _custom_docx(tmp_path / "levels.docx", body, styles)
    got = [(h["text"], h["level"]) for h in ok(run, "outline", doc=str(path))["result"]["headings"]]
    assert got == [
        ("Direct", 4),
        ("A chapter", 1),
        ("A part", 2),
        ("By name", 3),
        ("Direct beats style", 5),
    ]
    assert ok(run, "style.get", doc=str(path), style="Part")["result"]["chain"] == [
        "Part",
        "Chapter",
        "H1",
        "Normal",
    ]


def test_stats_counts_match_the_document(corpus_doc: CorpusWriter, run: Run) -> None:
    path = corpus_doc("kitchen_sink")
    doc = Document(str(path))
    result = ok(run, "stats", doc=str(path))["result"]
    assert result["paragraphs"] == len(doc.index)
    assert result["words"] == sum(len(p.text.split()) for p in doc.index.paragraphs)
    assert result["characters"] == sum(len(p.text) for p in doc.index.paragraphs)
    assert result["headings"] == 7
    assert result["tables"] == 3  # 3x3, 2x2 and the one nested in it
    assert result["comments"] == 6
    assert result["footnotes"] == 2
    assert result["revisions"]["total"] == 7
    assert result["revisions"]["by_type"]["insert"] == 2
    assert result["styles_in_use"] >= 4


def test_stats_lists_fonts_in_use(tmp_path: Path, run: Run) -> None:
    runs = (
        '<w:r><w:rPr><w:rFonts w:ascii="Garamond" w:hAnsi="Garamond"/></w:rPr><w:t>a</w:t></w:r>'
        '<w:r><w:rPr><w:rFonts w:ascii="Garamond"/></w:rPr><w:t>b</w:t></w:r>'
        '<w:r><w:rPr><w:rFonts w:hAnsi="Calibri"/></w:rPr><w:t>c</w:t></w:r>'
    )
    path = _custom_docx(tmp_path / "fonts.docx", _para("", runs=runs), _style("Normal", "Normal"))
    fonts = ok(run, "stats", doc=str(path))["result"]["fonts"]
    assert fonts == [{"font": "Garamond", "uses": 2}, {"font": "Calibri", "uses": 1}]


@pytest.mark.parametrize("kind", [k for k in KINDS if k != "novel"])
def test_para_get_returns_every_paragraph(kind: str, corpus_doc: CorpusWriter, run: Run) -> None:
    path = corpus_doc(kind)
    doc = Document(str(path))
    for para in doc.index.paragraphs:
        result = ok(run, "para.get", doc=str(path), at=str(para.address))["result"]
        assert result["text"] == para.text
        assert result["number"] == para.number
        assert result["length"] == len(para.text)
        assert all(0 <= r["start"] < r["end"] <= len(para.text) for r in result["runs"])


def test_para_get_reports_style_and_direct_formatting(tmp_path: Path, run: Run) -> None:
    runs = (
        "<w:r><w:t>plain </w:t></w:r>"
        "<w:r><w:rPr><w:b/><w:i/></w:rPr><w:t>bold</w:t></w:r>"
        "<w:r><w:rPr><w:b/><w:i/></w:rPr><w:t> italic </w:t></w:r>"
        '<w:r><w:rPr><w:b w:val="0"/><w:rFonts w:ascii="Garamond"/><w:sz w:val="26"/>'
        '<w:u w:val="single"/></w:rPr><w:t>tail</w:t></w:r>'
    )
    ppr = '<w:pStyle w:val="Quote"/><w:jc w:val="center"/><w:ind w:left="720" w:firstLine="360"/>'
    styles = _style("Normal", "Normal") + _style("Quote", "Quote", "Normal")
    path = _custom_docx(tmp_path / "fmt.docx", _para("", ppr, runs), styles)
    doc = Document(str(path))
    (para,) = doc.index.paragraphs
    result = ok(run, "para.get", doc=str(path), at=str(para.address))["result"]
    assert result["text"] == "plain bold italic tail"
    assert result["style"] == "Quote"
    assert result["style_name"] == "Quote"
    assert result["format"] == {
        "alignment": "center",
        "indent": {"left": 720, "first_line": 360},
    }
    assert result["runs"] == [
        {"start": 6, "end": 18, "bold": True, "italic": True},
        {
            "start": 18,
            "end": 22,
            "bold": False,
            "underline": "single",
            "font": "Garamond",
            "size_pt": 13.0,
        },
    ]


def test_para_get_without_style_reports_the_default_style(tmp_path: Path, run: Run) -> None:
    path = _custom_docx(tmp_path / "d.docx", _para("hello"), _style("Normal", "Normal"))
    (para,) = Document(str(path)).index.paragraphs
    result = ok(run, "para.get", doc=str(path), at=str(para.address))["result"]
    assert (result["style"], result["style_name"], result["runs"]) == ("Normal", "Normal", [])


def test_para_get_pages_a_paragraph_longer_than_the_budget(tmp_path: Path, run: Run) -> None:
    text = "ñandú " * 6000 + "fin"  # 2-byte characters, far beyond 8,000 bytes
    runs = (
        f"<w:r><w:rPr><w:b/></w:rPr><w:t>{text[:9000]}</w:t></w:r>"
        f"<w:r><w:t>{text[9000:]}</w:t></w:r>"
    )
    path = _custom_docx(tmp_path / "long.docx", _para("", runs=runs), _style("Normal", "Normal"))
    (para,) = Document(str(path)).index.paragraphs
    at = str(para.address)
    got = ""
    bold_end = 0
    cursor: str | None = None
    count = 0
    while True:
        extra = {"cursor": cursor} if cursor else {}
        code, env, nbytes = run("para.get", doc=str(path), at=at, **extra)
        assert code == 0 and nbytes <= RESPONSE_BUDGET
        result = env["result"]
        assert result["offset"] == len(got)
        got += result["text"]
        for span in result["runs"]:
            assert 0 <= span["start"] < span["end"] <= len(got)
            bold_end = max(bold_end, span["end"])
        count += 1
        cursor = env["next"]
        if cursor is None:
            break
    assert got == para.text
    assert bold_end == 9000
    assert count > 1
    small = ok(run, "para.get", doc=str(path), at=at, limit=10)
    assert small["result"]["text"] == para.text[:10] and small["next"] is not None


def test_para_get_rejects_bad_and_stale_addresses(corpus_doc: CorpusWriter, run: Run) -> None:
    path = corpus_doc("plain")
    (first, *_) = Document(str(path)).index.paragraphs
    code, env, _ = run("para.get", doc=str(path), at="paragraph one")
    assert (code, env["error"]["code"]) == (1, "ADDRESS_INVALID")
    code, env, _ = run("para.get", doc=str(path), at=f"p{first.number}#00000000")
    assert (code, env["error"]["code"]) == (1, "STALE_ADDRESS")
    code, env, _ = run("para.get", doc=str(path), at="p9999#00000000")
    assert (code, env["error"]["code"]) == (1, "STALE_ADDRESS")


def test_styles_list_and_style_get(corpus_doc: CorpusWriter, run: Run) -> None:
    path = corpus_doc("headings")
    listed = pages(run, "styles.list", "styles", doc=str(path))
    by_id = {s["id"]: s for s in listed}
    assert by_id["Heading2"]["name"] == "heading 2"
    assert by_id["Heading2"]["based_on"] == "Normal"
    assert by_id["Heading2"]["used"] == 2 and by_id["Heading3"]["used"] == 1
    assert by_id["Normal"]["default"] is True
    only_char = pages(run, "styles.list", "styles", doc=str(path), type="character")
    assert {s["type"] for s in only_char} == {"character"}
    used = pages(run, "styles.list", "styles", doc=str(path), used_only=True)
    assert used and all(s["used"] > 0 for s in used)
    got = ok(run, "style.get", doc=str(path), style="heading 2")["result"]  # by name
    assert got["id"] == "Heading2" and got["heading_level"] == 2
    assert got["chain"] == ["Heading2", "Normal"]
    assert got["own"]["paragraph"]["outline_level"] == 1
    assert got["effective"]["run"]["bold"] is True
    code, env, _ = run("style.get", doc=str(path), style="Nope")
    assert (code, env["error"]["code"]) == (1, "STYLE_NOT_FOUND")


def test_comments_list_threads_resolved_state_and_filters(
    corpus_doc: CorpusWriter, run: Run
) -> None:
    path = corpus_doc("comments")
    comments = pages(run, "comments.list", "comments", doc=str(path))
    assert [c["id"] for c in comments] == ["0", "1", "2", "3", "4", "5"]
    replies = {c["id"]: c["reply_to"] for c in comments if "reply_to" in c}
    assert replies == {"2": "1", "3": "1", "5": "4"}
    assert [c["id"] for c in comments if c["resolved"]] == ["4"]
    index = Document(str(path)).index
    for c in comments:  # every comment sits in a real paragraph
        assert index.resolve_address(c["at"]).text
    assert all(c["text"] and c["author"] and c["date"] for c in comments)
    done = pages(run, "comments.list", "comments", doc=str(path), resolved=True)
    assert [c["id"] for c in done] == ["4"]
    author = comments[1]["author"]
    mine = pages(run, "comments.list", "comments", doc=str(path), author=author)
    assert mine and all(c["author"] == author for c in mine)


def test_comments_list_is_empty_without_comments(corpus_doc: CorpusWriter, run: Run) -> None:
    result = ok(run, "comments.list", doc=str(corpus_doc("plain")))["result"]
    assert result == {"total": 0, "comments": []}


def test_revisions_list_types_authors_and_text(corpus_doc: CorpusWriter, run: Run) -> None:
    path = corpus_doc("revisions")
    revs = pages(run, "revisions.list", "revisions", doc=str(path))
    assert [r["type"] for r in revs] == [
        "delete",
        "insert",
        "mark_delete",
        "delete",
        "mark_insert",
        "insert",
        "format",
    ]
    assert revs[0]["text"] == "old wording " and revs[1]["text"] == "new wording "
    assert all(r["author"] and r["date"] and r["id"] for r in revs)
    index = Document(str(path)).index
    assert index.resolve_address(revs[0]["at"]).text.startswith("Before the change, new wording")
    only = pages(run, "revisions.list", "revisions", doc=str(path), type="insert")
    assert [r["type"] for r in only] == ["insert", "insert"]
    author = revs[0]["author"]
    by_author = pages(run, "revisions.list", "revisions", doc=str(path), author=author)
    assert by_author and all(r["author"] == author for r in by_author)


def test_cursors_page_lists_and_resist_misuse(
    corpus_doc: CorpusWriter, run: Run, tmp_path: Path
) -> None:
    path = corpus_doc("comments")
    first = ok(run, "comments.list", doc=str(path), limit=2)
    assert [c["id"] for c in first["result"]["comments"]] == ["0", "1"]
    assert first["result"]["total"] == 6
    cursor = first["next"]
    assert cursor.startswith("c:comments.list:")
    second = ok(run, "comments.list", doc=str(path), limit=10, cursor=cursor)
    assert [c["id"] for c in second["result"]["comments"]] == ["2", "3", "4", "5"]
    assert second["next"] is None
    # a cursor belongs to one operation
    code, env, _ = run("revisions.list", doc=str(path), cursor=cursor)
    assert (code, env["error"]["code"]) == (2, "SCHEMA_INVALID")
    for bad in ("c:comments.list:zz:1", "nonsense", "c:comments.list:" + "0" * 8 + ":-1"):
        code, env, _ = run("comments.list", doc=str(path), cursor=bad)
        assert (code, env["error"]["code"]) == (2, "SCHEMA_INVALID")
    # a cursor from another version of the file
    other = corpus_doc("comments", 1)
    code, env, _ = run("comments.list", doc=str(other), cursor=cursor)
    assert (code, env["error"]["code"]) == (1, "DOC_CHANGED_ON_DISK")
    # past the end: an empty page, not an error
    far = ok(run, "comments.list", doc=str(path), cursor=cursor.rsplit(":", 1)[0] + ":50")
    assert far["result"]["comments"] == [] and far["next"] is None


def test_requests_are_validated_against_the_schema(corpus_doc: CorpusWriter, run: Run) -> None:
    path = str(corpus_doc("plain"))
    bad_requests: list[tuple[str, dict[str, Any]]] = [
        ("outline", {}),
        ("outline", {"doc": path, "limit": 0}),
        ("outline", {"doc": path, "limit": 1001}),
        ("outline", {"doc": path, "surprise": 1}),
        ("para.get", {"doc": path}),
        ("style.get", {"doc": path}),
        ("revisions.list", {"doc": path, "type": "nonsense"}),
    ]
    for op, request in bad_requests:
        code, env, _ = run(op, **request)
        assert (code, env["error"]["code"]) == (2, "SCHEMA_INVALID"), (op, request)


def test_missing_and_foreign_files_are_reported(tmp_path: Path, run: Run) -> None:
    code, env, _ = run("info", doc=str(tmp_path / "absent.docx"))
    assert (code, env["error"]["code"]) == (1, "DOC_NOT_FOUND")
    text = tmp_path / "notes.docx"
    text.write_text("not a zip")
    code, env, _ = run("outline", doc=str(text))
    assert (code, env["error"]["code"]) == (1, "NOT_A_DOCX")


def test_fact_hooks_extend_para_get_and_outline(
    corpus_doc: CorpusWriter, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layout facts (T31) plug in through PARA_FACTS and OUTLINE_FACTS."""
    path = corpus_doc("headings")
    monkeypatch.setattr(read, "PARA_FACTS", [lambda ctx: {"number_fact": ctx.paragraph.number}])
    monkeypatch.setattr(read, "OUTLINE_FACTS", [lambda ctx: {"seen": True}])
    heading = ok(run, "outline", doc=str(path))["result"]["headings"][0]
    assert heading["seen"] is True
    got = ok(run, "para.get", doc=str(path), at=heading["at"])["result"]
    assert got["number_fact"] == 1


def test_cli_process_serves_read_operations(corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("headings")
    request = json.dumps({"doc": str(path)})
    out = subprocess.run(
        [sys.executable, "-m", "doc_control", "outline", "--json", request],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    env = json.loads(out)
    assert env["ok"] is True and env["op"] == "outline"
    assert len(env["result"]["headings"]) == 4


def _private_ids() -> list[str]:
    return [doc_id for doc_id, _ in private_documents()]


@pytest.mark.parametrize("doc_id", _private_ids() or ["unset"])
def test_private_corpus_read_ops_stay_in_budget(doc_id: str, run: Run, tmp_path: Path) -> None:
    """Every read op answers within the budget on a private document (named only by hash)."""
    data = dict(private_documents()).get(doc_id)
    if data is None:
        pytest.skip("DOC_CONTROL_PRIVATE_CORPUS is not set")
    path = tmp_path / f"{short_id(data)}.docx"
    path.write_bytes(data)
    for op in ("info", "outline", "stats", "styles.list", "comments.list", "revisions.list"):
        code, _, nbytes = run(op, doc=str(path))
        assert code == 0 and nbytes <= RESPONSE_BUDGET, op


_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _rev_attrs(rid: int, author: str) -> str:
    return f'w:id="{rid}" w:author="{author}" w:date="2026-01-01T00:00:00Z"'


def _revisions_of(run: Run, path: Path) -> list[dict[str, Any]]:
    return pages(run, "revisions.list", "revisions", doc=str(path))


def test_revisions_nested_inside_insertions_are_listed(tmp_path: Path, run: Run) -> None:
    """Deleting another author's insertion, and formatting inserted text, are both revisions."""
    runs = (
        f"<w:ins {_rev_attrs(1, 'A')}><w:del {_rev_attrs(2, 'B')}>"
        "<w:r><w:delText>gone</w:delText></w:r></w:del></w:ins>"
        f"<w:ins {_rev_attrs(3, 'A')}><w:r><w:rPr><w:b/>"
        f"<w:rPrChange {_rev_attrs(4, 'C')}><w:rPr/></w:rPrChange></w:rPr><w:t>bold</w:t></w:r>"
        "</w:ins>"
        f'<w:del {_rev_attrs(5, "D")}><w:r><w:rPr><w:rStyle w:val="Strong"/>'
        f"<w:rPrChange {_rev_attrs(6, 'E')}><w:rPr/></w:rPrChange></w:rPr>"
        "<w:delText>old</w:delText></w:r></w:del>"
        f"<w:moveTo {_rev_attrs(7, 'F')}><w:ins {_rev_attrs(8, 'G')}>"
        "<w:r><w:t>moved</w:t></w:r></w:ins></w:moveTo>"
    )
    ppr = (
        f"<w:rPr><w:ins {_rev_attrs(9, 'H')}/>"
        f"<w:rPrChange {_rev_attrs(10, 'I')}><w:rPr/></w:rPrChange></w:rPr>"
    )
    path = _custom_docx(tmp_path / "nested.docx", _para("", ppr, runs), _style("Normal", "Normal"))
    got = [(r["id"], r["type"], r.get("text")) for r in _revisions_of(run, path)]
    assert got == [
        ("9", "mark_insert", None),  # the paragraph mark comes first in the markup
        ("10", "mark_format", None),
        ("1", "insert", "gone"),
        ("2", "delete", "gone"),
        ("3", "insert", "bold"),
        ("4", "format", None),
        ("5", "delete", "old"),
        ("6", "format", None),
        ("7", "move_to", "moved"),
        ("8", "insert", "moved"),
    ]
    stats = ok(run, "stats", doc=str(path))["result"]["revisions"]
    assert stats["total"] == 10
    assert stats["by_type"] == {
        "delete": 2,
        "format": 2,
        "insert": 3,
        "mark_format": 1,
        "mark_insert": 1,
        "move_to": 1,
    }


def test_table_row_and_cell_revisions_are_listed(tmp_path: Path, run: Run) -> None:
    cells = (
        f"<w:tc><w:tcPr><w:cellIns {_rev_attrs(2, 'B')}/></w:tcPr>{_para('one')}</w:tc>"
        f"<w:tc><w:tcPr><w:cellDel {_rev_attrs(3, 'B')}/>"
        f'<w:cellMerge {_rev_attrs(4, "B")} w:vMerge="rest"/></w:tcPr>{_para("two")}</w:tc>'
    )
    table = (
        f"<w:tbl><w:tr><w:trPr><w:cantSplit/></w:trPr><w:tc>{_para('plain')}</w:tc></w:tr>"
        f"<w:tr><w:trPr><w:ins {_rev_attrs(1, 'A')}/></w:trPr>{cells}</w:tr>"
        f"<w:tr><w:trPr><w:del {_rev_attrs(5, 'A')}/></w:trPr><w:tc>{_para('gone')}</w:tc></w:tr>"
        "</w:tbl>"
    )
    path = _custom_docx(
        tmp_path / "rows.docx", _para("before") + table + _para("after"), _style("Normal", "Normal")
    )
    index = Document(str(path)).index
    revs = _revisions_of(run, path)
    assert [(r["id"], r["type"]) for r in revs] == [
        ("1", "row_insert"),
        ("2", "cell_insert"),
        ("3", "cell_delete"),
        ("4", "cell_merge"),
        ("5", "row_delete"),
    ]
    assert [index.resolve_address(r["at"]).text for r in revs] == [
        "one",
        "one",
        "two",
        "two",
        "gone",
    ]
    assert ok(run, "stats", doc=str(path))["result"]["revisions"]["total"] == 5


def test_cursors_are_bound_to_filters_and_address(
    corpus_doc: CorpusWriter, run: Run, tmp_path: Path
) -> None:
    path = corpus_doc("comments")
    cursor = ok(run, "comments.list", doc=str(path), limit=1, resolved=False)["next"]
    assert ok(run, "comments.list", doc=str(path), resolved=False, cursor=cursor)["result"]
    for other in ({"resolved": True}, {}, {"resolved": False, "author": "Claude"}):
        code, env, _ = run("comments.list", doc=str(path), cursor=cursor, **other)
        assert (code, env["error"]["code"]) == (2, "SCHEMA_INVALID"), other
    long = _custom_docx(
        tmp_path / "long.docx", _para("a" * 20000) + _para("b" * 20000), _style("Normal", "Normal")
    )
    one, two = Document(str(long)).index.paragraphs
    page = ok(run, "para.get", doc=str(long), at=str(one.address))
    code, env, _ = run("para.get", doc=str(long), at=str(two.address), cursor=page["next"])
    assert (code, env["error"]["code"]) == (2, "SCHEMA_INVALID")
    assert ok(run, "para.get", doc=str(long), at=str(one.address), cursor=page["next"])


def test_comment_location_is_the_range_start_not_the_reference(tmp_path: Path, run: Run) -> None:
    def comment(cid: int, text: str) -> str:
        return (
            f'<w:comment w:id="{cid}" w:author="A" w:date="2026-01-01T00:00:00Z">'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:comment>"
        )

    comments = f"<w:comments {_W}>{comment(0, 'body level')}{comment(1, 'inside')}</w:comments>"
    body = (
        _para("first")
        + '<w:commentRangeStart w:id="0"/>'  # between paragraphs: belongs to the next one
        + _para("second", runs='<w:commentRangeStart w:id="1"/><w:r><w:t>second</w:t></w:r>')
        + _para(
            "third",
            runs='<w:commentRangeEnd w:id="0"/><w:commentRangeEnd w:id="1"/>'
            '<w:r><w:commentReference w:id="0"/></w:r><w:r><w:commentReference w:id="1"/></w:r>',
        )
    )
    path = _custom_docx(
        tmp_path / "c.docx", body, _style("Normal", "Normal"), {"word/comments.xml": comments}
    )
    index = Document(str(path)).index
    listed = pages(run, "comments.list", "comments", doc=str(path))
    got = {c["id"]: index.resolve_address(c["at"]).text for c in listed}
    assert got == {"0": "second", "1": "second"}


def test_style_usage_ignores_deleted_runs_and_old_properties(tmp_path: Path, run: Run) -> None:
    styles = _style("Normal", "Normal") + (
        '<w:style w:type="character" w:styleId="Strong"><w:name w:val="Strong"/></w:style>'
        '<w:style w:type="table" w:styleId="Grid"><w:name w:val="Grid"/></w:style>'
    )
    runs = (
        '<w:r><w:rPr><w:rStyle w:val="Strong"/></w:rPr><w:t>live</w:t></w:r>'
        f'<w:del {_rev_attrs(1, "A")}><w:r><w:rPr><w:rStyle w:val="Strong"/></w:rPr>'
        "<w:delText>dead</w:delText></w:r></w:del>"
        '<w:r><w:rPr><w:b/><w:rPrChange w:id="2" w:author="A">'
        '<w:rPr><w:rStyle w:val="Strong"/></w:rPr></w:rPrChange></w:rPr><w:t>x</w:t></w:r>'
    )
    table = (
        '<w:tbl><w:tblPr><w:tblStyle w:val="Grid"/>'
        '<w:tblPrChange w:id="3" w:author="A"><w:tblPr><w:tblStyle w:val="Grid"/></w:tblPr>'
        f"</w:tblPrChange></w:tblPr><w:tr><w:tc>{_para('cell')}</w:tc></w:tr></w:tbl>"
    )
    path = _custom_docx(tmp_path / "u.docx", _para("", runs=runs) + table, styles)
    used = {s["id"]: s["used"] for s in pages(run, "styles.list", "styles", doc=str(path))}
    assert used["Strong"] == 1
    assert used["Grid"] == 1
