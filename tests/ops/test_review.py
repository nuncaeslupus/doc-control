"""Reading a reviewed document: comment quotes (`comment.list`), paired replacements
(`revision.list`) and both by paragraph (`review.list`)."""

from __future__ import annotations

import contextlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from doc_control import api
from doc_control.cli import main
from doc_control.mcp import handle, tool_name
from doc_control.ops.paging import RESPONSE_BUDGET
from doc_control.registry import load_operations
from tests.helpers import CONTENT_TYPES, RELS

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"'
)
DATE = "2026-01-02T03:04:05Z"


def r(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def ins(rid: int, author: str, text: str) -> str:
    return f'<w:ins w:id="{rid}" w:author="{author}" w:date="{DATE}">{r(text)}</w:ins>'


def dele(rid: int, author: str, text: str) -> str:
    run = f'<w:r><w:delText xml:space="preserve">{text}</w:delText></w:r>'
    return f'<w:del w:id="{rid}" w:author="{author}" w:date="{DATE}">{run}</w:del>'


def start(cid: int) -> str:
    return f'<w:commentRangeStart w:id="{cid}"/>'


def end(cid: int) -> str:
    return f'<w:commentRangeEnd w:id="{cid}"/><w:r><w:commentReference w:id="{cid}"/></w:r>'


def comment(cid: int, author: str, text: str, para_id: str) -> str:
    return (
        f'<w:comment w:id="{cid}" w:author="{author}" w:date="{DATE}" w:initials="X">'
        f'<w:p w14:paraId="{para_id}">{r(text)}</w:p></w:comment>'
    )


#: The body: a reviewer's (Editor A) comments and changes, plus one change by Editor B.
BODY = [
    # p1: a replacement inside a commented range, and a reply to that comment
    f"<w:p>{r('Alpha ')}{start(1)}{r('beta ')}{dele(10, 'Editor A', 'gamma ')}"
    f"{ins(11, 'Editor A', 'delta ')}{r('epsilon')}{end(1)}{r(' omega.')}</w:p>",
    # p2-p3: a range across two paragraphs
    f"<w:p>{r('Zeta ')}{start(2)}{r('eta')}</w:p>",
    f"<w:p>{r('theta')}{end(2)}{r(' iota ')}{ins(12, 'Editor B', 'kappa')}"
    f"{dele(13, 'Editor A', 'lambda')}{r(' mu ')}{ins(14, 'Editor A', 'nu')}</w:p>",
    # p4: a comment on a point; insertion then deletion with a bookmark between; a
    # deletion followed by another author's insertion
    f'<w:p>{r("Xi ")}<w:r><w:commentReference w:id="3"/></w:r>{ins(15, "Editor A", "omicron")}'
    f'<w:bookmarkStart w:id="0" w:name="mark"/><w:bookmarkEnd w:id="0"/>'
    f"{dele(16, 'Editor A', 'pi')}{r(' rho ')}{dele(17, 'Editor A', 'sigma')}"
    f"{ins(18, 'Editor B', 'tau')}</w:p>",
    # a range opened between paragraphs, closed inside p5
    start(4),
    f"<w:p>{r('Upsilon phi')}{end(4)}{r(' chi.')}</w:p>",
    f"<w:p>{r('Nothing to see here.')}</w:p>",
]

COMMENTS = (
    f"<w:comments {NS}>"
    + comment(1, "Editor A", "Is this right?", "00000001")
    + comment(2, "Editor A", "Split?", "00000002")
    + comment(3, "Editor B", "A point.", "00000003")
    + comment(4, "Editor A", "From outside.", "00000004")
    + comment(5, "Editor B", "Yes, it is.", "00000005")
    + "</w:comments>"
)
COMMENTS_EXTENDED = (
    f"<w15:commentsEx {NS}>"
    '<w15:commentEx w15:paraId="00000001" w15:done="0"/>'
    '<w15:commentEx w15:paraId="00000002" w15:done="1"/>'
    '<w15:commentEx w15:paraId="00000003" w15:done="0"/>'
    '<w15:commentEx w15:paraId="00000004" w15:done="0"/>'
    '<w15:commentEx w15:paraId="00000005" w15:paraIdParent="00000001" w15:done="0"/>'
    "</w15:commentsEx>"
)


OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml"
#: The comment parts' content types and relationships: without them a comment reference
#: points nowhere and LibreOffice refuses the file.
PARTS_TYPES = CONTENT_TYPES.replace(
    "</Types>",
    f'<Override PartName="/word/comments.xml" ContentType="{WORD}.comments+xml"/>'
    f'<Override PartName="/word/commentsExtended.xml" ContentType="{WORD}.commentsExtended+xml"/>'
    "</Types>",
)
DOCUMENT_RELS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{OFFICE}/comments" Target="comments.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.microsoft.com/office/2011/relationships/'
    'commentsExtended" Target="commentsExtended.xml"/>'
    "</Relationships>"
)


def reviewed(path: Path, body: list[str] = BODY, comments: str = COMMENTS) -> Path:
    document = f"<w:document {NS}><w:body>{''.join(body)}</w:body></w:document>"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", PARTS_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/comments.xml", comments)
        zf.writestr("word/commentsExtended.xml", COMMENTS_EXTENDED)
    return path


def result(op: str, doc: Path, **request: Any) -> dict[str, Any]:
    out: dict[str, Any] = api.run(op, {"doc": str(doc), **request}).result
    return out


def texts(doc: Path) -> list[str]:
    return [p["text"] for p in result("para.list", doc)["paragraphs"]]


# -- comment.list: quote -------------------------------------------------------------


def test_comment_list_quotes_the_text_each_comment_covers(tmp_path: Path) -> None:
    doc = reviewed(tmp_path / "r.docx")
    quotes = {c["id"]: c["quote"] for c in result("comment.list", doc)["comments"]}
    assert quotes == {
        "1": "beta delta epsilon",  # the insertion in, the deletion out
        "5": "",  # a reply with no range of its own
        "2": "eta\ntheta",  # across paragraphs
        "3": "",  # on a point
        "4": "Upsilon phi",  # opened between paragraphs: from the next one's start
    }


def test_a_long_quote_is_clipped_and_says_so(tmp_path: Path) -> None:
    long = "word " * 100
    doc = reviewed(tmp_path / "r.docx", [f"<w:p>{start(1)}{r(long)}{end(1)}</w:p>"])
    row = result("comment.list", doc)["comments"][0]
    assert row["quote"] == long[:200] and row["quote_truncated"] is True
    short = result("comment.list", reviewed(tmp_path / "s.docx"))["comments"][0]
    assert "quote_truncated" not in short


# -- revision.list: replacements -----------------------------------------------------


def test_revision_list_pairs_a_deletion_and_insertion_into_a_replacement(tmp_path: Path) -> None:
    doc = reviewed(tmp_path / "r.docx")
    revs = {r["id"]: r for r in result("revision.list", doc)["revisions"]}
    assert revs["11"]["replaces"] == "10" and revs["10"]["replaced_by"] == "11"
    # insertion then deletion, only a bookmark between them
    assert revs["15"]["replaces"] == "16" and revs["16"]["replaced_by"] == "15"
    for lone in ("12", "13", "14", "17", "18"):  # text between, or another author
        assert "replaces" not in revs[lone] and "replaced_by" not in revs[lone], lone


def test_both_halves_of_a_replacement_still_accept_by_id(tmp_path: Path) -> None:
    doc = reviewed(tmp_path / "r.docx")
    api.run("revision.accept", {"doc": str(doc), "ids": ["10", "11"]})
    left = {r["id"] for r in result("revision.list", doc)["revisions"]}
    assert not left & {"10", "11"} and "12" in left
    assert texts(doc)[0] == "Alpha beta delta epsilon omega."


# -- review.list -----------------------------------------------------------------------


def test_review_list_groups_comments_and_changes_by_paragraph(tmp_path: Path) -> None:
    doc = reviewed(tmp_path / "r.docx")
    out = result("review.list", doc)
    paragraphs = out["paragraphs"]
    assert out["total"] == len(paragraphs) == 5  # the last paragraph has nothing
    first = paragraphs[0]
    assert first["text"] == "Alpha beta delta epsilon omega."
    [thread] = first["comments"]
    assert thread["id"] == "1" and thread["quote"] == "beta delta epsilon"
    assert thread["resolved"] is False
    assert [(c["id"], c["author"], c["text"]) for c in thread["replies"]] == [
        ("5", "Editor B", "Yes, it is.")
    ]
    assert first["changes"] == [
        {
            "type": "replace",
            "ids": ["10", "11"],
            "author": "Editor A",
            "date": DATE,
            "old": "gamma ",
            "new": "delta ",
        }
    ]
    assert paragraphs[1]["comments"][0]["resolved"] is True
    assert "changes" not in paragraphs[1]
    assert [(c["type"], c.get("id"), c.get("ids")) for c in paragraphs[2]["changes"]] == [
        ("insert", "12", None),
        ("delete", "13", None),
        ("insert", "14", None),
    ]
    fourth = paragraphs[3]
    assert fourth["comments"][0]["quote"] == ""
    assert [c["type"] for c in fourth["changes"]] == ["replace", "delete", "insert"]
    assert fourth["changes"][0]["ids"] == ["16", "15"]  # [deletion, insertion]
    assert paragraphs[4]["comments"][0]["quote"] == "Upsilon phi"
    totals = out["totals"]
    assert {k: totals[k] for k in ("comments", "insert", "delete", "replace")} == {
        "comments": 5,
        "insert": 3,
        "delete": 2,
        "replace": 2,
    }
    assert totals["by_author"] == {
        "Editor A": {"comments": 3, "insert": 1, "delete": 2, "replace": 2},
        "Editor B": {"comments": 2, "insert": 2, "delete": 0, "replace": 0},
    }


def test_review_list_filters_by_author(tmp_path: Path) -> None:
    doc = reviewed(tmp_path / "r.docx")
    out = result("review.list", doc, author="Editor B")
    changes = [c for p in out["paragraphs"] for c in p.get("changes", [])]
    assert {(c["type"], c["id"]) for c in changes} == {("insert", "12"), ("insert", "18")}
    threads = [c["id"] for p in out["paragraphs"] for c in p.get("comments", [])]
    assert threads == ["1", "3"]  # a thread they replied in comes whole
    assert out["totals"]["replace"] == 0 and out["totals"]["insert"] == 2


def test_review_list_pages_with_a_cursor(tmp_path: Path) -> None:
    body = [
        f"<w:p>{r(f'Line {n} ')}{dele(2 * n, 'Editor A', 'old ' * 30)}"
        f"{ins(2 * n + 1, 'Editor A', 'new ' * 30)}</w:p>"
        for n in range(1, 120)
    ]
    doc = reviewed(tmp_path / "big.docx", body, f"<w:comments {NS}></w:comments>")
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        printed, env = cli_page(doc, cursor)
        assert printed <= RESPONSE_BUDGET
        seen += [p["at"] for p in env["result"]["paragraphs"]]
        assert env["result"]["totals"]["replace"] == 119  # every page: the whole list's
        pages += 1
        cursor = env["next"]
        if cursor is None:
            break
    assert pages > 1 and len(seen) == len(set(seen)) == 119


def cli_page(doc: Path, cursor: str | None) -> tuple[int, dict[str, Any]]:
    """One review.list page as the CLI prints it: (bytes printed, envelope)."""
    request: dict[str, Any] = {"doc": str(doc), **({"cursor": cursor} if cursor else {})}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["review.list", "--json", json.dumps(request)]) == 0
    text = out.getvalue().rstrip("\n")
    return len(text.encode("utf-8")), json.loads(text)


def test_review_list_is_a_read_tool() -> None:
    registry = load_operations()
    assert registry["review.list"].kind == "read"
    listed = handle(registry, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed is not None
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert tools[tool_name("review.list")]["annotations"]["readOnlyHint"] is True
