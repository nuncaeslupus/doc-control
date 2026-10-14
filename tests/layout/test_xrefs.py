"""`caption.add`, `xref.add`, `index.mark` and `index.insert` (LAYOUT T8).

Captions, cross-references and index entries are tracked insertions whose results
`fields.update` fills in from the layout LibreOffice renders; each field type it relies on is
recomputed from a wrong cached value.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from tests.corpus.validator import REQUIRE_ENV
from tests.layout.test_toc import (
    NS,
    REL,
    _build,
    _heading,
    _need_soffice,
    _page_break,
    _para,
    render_pages,
)


def _texts(doc: Path) -> list[str]:
    return [r["text"] for r in api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]]


def _at(doc: Path, prefix: str) -> str:
    rows = api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]
    return str(next(r["at"] for r in rows if r["text"].startswith(prefix)))


def _fields(doc: Path, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = api.run("fields.list", {"doc": str(doc), "type": kind}).result[
        "fields"
    ]
    return out


def _accept(doc: Path, tmp: Path) -> Path:
    """A copy of ``doc`` with every tracked change accepted, as Word shows it settled."""
    settled = tmp / "settled.docx"
    shutil.copyfile(doc, settled)
    api.run("batch", {"doc": str(settled), "ops": [{"op": "revision.accept", "all": True}]})
    return settled


def report(pages: list[str], needle: str, exact: bool = False) -> list[int]:
    """The pages (1-based) with a line that is, or starts with, ``needle``."""
    return [
        n + 1
        for n, text in enumerate(pages)
        if any(
            line.strip() == needle if exact else line.strip().startswith(needle)
            for line in text.splitlines()
        )
    ]


def story() -> str:
    """Four pages: a front page, a page with a heading and a table, a page with a figure and
    a second table, and a page for the references and the index."""
    body = _para("Front matter") + _page_break()
    body += _heading("Alpha") + _para("Alpha text.") + _para("Quarterly results")
    body += _page_break()
    body += _para("Sunrise picture") + _para("Second table body") + _page_break()
    for line in ("Closing remarks", "To table one:", "To table one page:", "To figure one:"):
        body += _para(line)
    for line in ("To table two:", "To heading:", "To heading page:", "Index goes here"):
        body += _para(line)
    return body


def make(path: Path) -> Path:
    return _build(path, story())


def need_pdftotext() -> None:
    if shutil.which("pdftotext") is None:
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but pdftotext is not available")
        pytest.skip("pdftotext is not available")


def test_captions_xrefs_and_index_resolve_after_update(tmp_path: Path) -> None:
    _need_soffice()
    need_pdftotext()
    doc = make(tmp_path / "doc.docx")

    def add(op: str, **request: Any) -> dict[str, Any]:
        out: dict[str, Any] = api.run(op, {"doc": str(doc), **request}).result
        return out

    add("caption.add", at=_at(doc, "Quarterly"), label="Table", text="Quarterly results")
    add("caption.add", at=_at(doc, "Sunrise"), label="Figure", text="Sunrise")
    add("caption.add", at=_at(doc, "Second table"), label="Table", text="Second table")
    assert [t for t in _texts(doc) if t.startswith(("Table ", "Figure "))] == [
        "Table 1: Quarterly results",
        "Figure 1: Sunrise",
        "Table 2: Second table",
    ]
    t1, f1, t2 = _at(doc, "Table 1"), _at(doc, "Figure 1"), _at(doc, "Table 2")
    heading = _at(doc, "Alpha")
    for prefix, to, show in (
        ("To table one:", t1, "number"),
        ("To table one page:", t1, "page"),
        ("To figure one:", f1, "number"),
        ("To table two:", t2, "number"),
        ("To heading:", heading, "text"),
        ("To heading page:", heading, "page"),
    ):
        add("xref.add", at=_at(doc, prefix), to=to, show=show)
    # flat entries: with a Term:Sub mark fields.update would leave the index as it is
    for prefix, entry in (("Alpha text", "Alpha"), ("Sunrise", "Sunrise")):
        add("index.mark", at=_at(doc, prefix), entry=entry)
    add("index.mark", at=_at(doc, "Closing"), entry="Closing")
    add("index.insert", after=_at(doc, "Index goes here"))
    assert all(r["stale"] for r in _fields(doc, "INDEX"))

    updated = add("fields.update")
    assert updated["skipped"] == [] and updated["written"] is True
    assert [u["type"] for u in updated["updated"]].count("INDEX") == 1
    assert not [
        r for k in ("REF", "PAGEREF", "SEQ", "INDEX") for r in _fields(doc, k) if r["stale"]
    ]

    settled = _accept(doc, tmp_path)
    pages = render_pages(settled, tmp_path)
    mismatches: list[str] = []

    def page_of(needle: str, exact: bool = False) -> int:
        found = report(pages, needle, exact)
        assert len(found) == 1, f"{needle!r} rendered on {found}"
        return found[0]

    # what the page text says: the captions are numbered as the cross-references read
    assert [page_of("Table 1:"), page_of("Figure 1:"), page_of("Table 2:")] == [2, 3, 3]
    refs = [r["result"] for r in _fields(settled, "REF")]
    paged = [r["result"] for r in _fields(settled, "PAGEREF")]
    wanted = ["Table 1", "Figure 1", "Table 2", "Alpha"]
    mismatches += [f"REF {g!r} != {w!r}" for g, w in zip(refs, wanted, strict=True) if g != w]
    wanted_pages = [str(page_of("Table 1:")), str(page_of("Alpha", exact=True))]
    mismatches += [
        f"PAGEREF {g} != {w}" for g, w in zip(paged, wanted_pages, strict=True) if g != w
    ]
    assert [r["result"] for r in _fields(settled, "SEQ")] == ["1", "1", "2"]

    (index,) = _fields(settled, "INDEX")
    listed = dict(re.findall(r"([A-Za-z:]+)\t?(\d+)", "\n".join(_texts(settled))))
    for entry, needle in (("Alpha", "Alpha text."), ("Sunrise", "Sunrise picture")):
        if listed.get(entry) != str(page_of(needle)):
            mismatches.append(f"index {entry}: {listed.get(entry)} != {page_of(needle)}")
    if listed.get("Closing") != str(page_of("Closing remarks")):
        mismatches.append(f"index Closing: {listed.get('Closing')}")
    assert index["result"] != "" and mismatches == []  # xref_page_mismatches == 0


def _field(code: str, result: str) -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> {code} </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f"<w:r><w:t>{result}</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def _xe(term: str) -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> XE "{term}" </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def _bookmarked(bid: int, name: str, inner: str) -> str:
    return f'<w:bookmarkStart w:id="{bid}" w:name="{name}"/>{inner}<w:bookmarkEnd w:id="{bid}"/>'


def test_update_recomputes_each_field_type_from_a_wrong_cached_value(tmp_path: Path) -> None:
    """LibreOffice keeps the cached result of some fields (NOTEREF, SECTION, SECTIONPAGES);
    REF, PAGEREF, SEQ and INDEX must really be recomputed, or the cross-references would
    show whatever was cached. (A SEQ without ``\\* ARABIC``, which `caption.add` always
    writes, is not a number to LibreOffice, and a REF to it echoes the cached number.)"""
    _need_soffice()
    caption = _bookmarked(
        1,
        "_Ref100",
        '<w:r><w:t xml:space="preserve">Table </w:t></w:r>' + _field("SEQ Table \\* ARABIC", "42"),
    )
    body = _para("Intro") + _page_break()
    body += (
        '<w:p><w:r><w:t xml:space="preserve">See </w:t></w:r>'
        + _field("REF _Ref100 \\h", "WRONG")
        + _field("PAGEREF _Ref100 \\h", "77")
        + "</w:p>"
        + _page_break()
        + f"<w:p>{caption}<w:r><w:t>: cap</w:t></w:r></w:p>"
        + "<w:p><w:r><w:t>Banana</w:t></w:r>"
        + _xe("Banana")
        + _xe("Apple")
        + "</w:p>"
        + _page_break()
        + _para("Index")
        + '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> INDEX </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>Zebra\t99</w:t></w:r></w:p>'
        '<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    doc = _build(tmp_path / "wrong.docx", body)
    out = api.run("fields.update", {"doc": str(doc)}).result
    assert out["skipped"] == []
    changed = {u["type"]: u for u in out["updated"]}
    assert changed["REF"]["before"] == ["WRONG"] and changed["REF"]["after"] == ["Table 1"]
    assert changed["PAGEREF"]["before"] == ["77"] and changed["PAGEREF"]["after"] == ["3"]
    assert changed["SEQ"]["before"] == ["42"] and changed["SEQ"]["after"] == ["1"]
    assert changed["INDEX"]["before"] == ["Zebra\t99"]
    assert changed["INDEX"]["after"] == ["Apple\t3", "Banana\t3"]


def _index_xml(doc: Path) -> str:
    """The document XML from the paragraph holding the INDEX code to the end of the body."""
    with zipfile.ZipFile(doc) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    start = xml.rindex("<w:p>", 0, xml.index(" INDEX"))
    return xml[start : xml.index("</w:body>", start)]


@pytest.mark.parametrize(
    ("code", "marks", "why"),
    [
        ("INDEX", ("Apple:Pie", "Apple:Tart", "Banana"), "sub-entries"),
        ('INDEX \\e "\t" \\c "1"', ("Apple", "Banana"), "switches"),
    ],
)
def test_update_leaves_an_index_it_cannot_rebuild_alone(
    tmp_path: Path, code: str, marks: tuple[str, ...], why: str
) -> None:
    """LibreOffice lists Term:Sub entries flat and ignores the INDEX switches, so an index
    Word built that way would be overwritten with a wrong one: it is skipped, untouched,
    while the other fields are still updated."""
    _need_soffice()
    word_built = "".join(
        f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in ("Apple", "Pie\t2", "Tart\t2")
    )
    body = (
        '<w:p><w:r><w:t xml:space="preserve">Table </w:t></w:r>'
        + _field("SEQ Table \\* ARABIC", "42")
        + "</w:p><w:p><w:r><w:t>Terms</w:t></w:r>"
        + "".join(_xe(m) for m in marks)
        + "</w:p>"
        + '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> {code} </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>Apple</w:t></w:r></w:p>'
        + word_built
        + '<w:p><w:r><w:t>Banana\t2</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    doc = _build(tmp_path / "word.docx", body)
    before_xml = _index_xml(doc)
    (before,) = _fields(doc, "INDEX")
    out = api.run("fields.update", {"doc": str(doc)}).result
    assert [u["type"] for u in out["updated"]] == ["SEQ"] and out["written"] is True
    (skipped,) = out["skipped"]
    assert skipped["type"] == "INDEX" and why in skipped["reason"]
    assert _index_xml(doc) == before_xml
    assert _fields(doc, "INDEX") == [before]


def test_xref_to_a_simple_field_caption_number_is_refused(tmp_path: Path) -> None:
    body = (
        '<w:p><w:r><w:t xml:space="preserve">Table </w:t></w:r>'
        '<w:fldSimple w:instr=" SEQ Table \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple>'
        "<w:r><w:t>: cap</w:t></w:r></w:p>" + _para("See")
    )
    doc = _build(tmp_path / "simple.docx", body)
    original = doc.read_bytes()
    request = {"doc": str(doc), "at": _at(doc, "See"), "to": _at(doc, "Table")}
    for show in ("number", "page"):
        with pytest.raises(DocControlError) as err:
            api.run("xref.add", {**request, "show": show})
        assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT
        assert "fldSimple" in err.value.message
        assert doc.read_bytes() == original
    api.run("xref.add", {**request, "show": "text"})
    (row,) = _fields(doc, "REF")
    assert row["code"].startswith("REF _Ref")


def test_caption_and_xref_are_tracked_insertions_with_a_bookmark(tmp_path: Path) -> None:
    doc = make(tmp_path / "doc.docx")
    plain = _texts(doc)
    cap = api.run(
        "caption.add",
        {"doc": str(doc), "at": _at(doc, "Quarterly"), "label": "Table", "text": "Results"},
    ).result
    assert cap["position"] == "above" and cap["number"] == 1
    assert _at(doc, "Table 1") == cap["at"] and _texts(doc)[5] == "Quarterly results"
    assert cap["bookmark"].startswith("_Ref")
    with zipfile.ZipFile(doc) as zf:
        xml = zf.read("word/document.xml").decode()
    assert f'w:name="{cap["bookmark"]}"' in xml and "SEQ Table" in xml
    assert xml.count("<w:ins ") >= 5 and 'w:dirty="true"' in xml
    figure = api.run(
        "caption.add",
        {"doc": str(doc), "at": _at(doc, "Sunrise"), "label": "Figure", "text": ""},
    ).result
    assert figure["position"] == "below" and _at(doc, "Figure 1") == figure["at"]
    ref = api.run(
        "xref.add", {"doc": str(doc), "at": _at(doc, "To table one:"), "to": cap["at"]}
    ).result
    assert ref["code"] == f"REF {cap['bookmark']} \\h" and ref["bookmark"] == cap["bookmark"]
    # the caption already had a bookmark over its label and number: the target is untouched
    assert ref["footprint"]["word/document.xml"] == [ref["at"]]
    assert _texts(doc)[_texts(doc).index("Table 1: Results")] == "Table 1: Results"
    assert _fields(doc, "REF")[0]["result"] == "Table 1" and _fields(doc, "REF")[0]["stale"]
    # a heading has no bookmark yet: it gets one, over its text
    page = api.run(
        "xref.add",
        {
            "doc": str(doc),
            "at": _at(doc, "To table one page:"),
            "to": _at(doc, "Alpha"),
            "show": "page",
        },
    ).result
    assert (
        page["code"].startswith("PAGEREF _Ref") and len(page["footprint"]["word/document.xml"]) == 2
    )
    mark = api.run(
        "index.mark", {"doc": str(doc), "at": _at(doc, "Alpha text"), "entry": 'Say "hi":Sub'}
    ).result
    assert mark["code"] == 'XE "Say \\"hi\\":Sub"'
    inserted = api.run(
        "index.insert", {"doc": str(doc), "after": _at(doc, "To heading page:")}
    ).result
    assert inserted["code"] == "INDEX" and "Index" in _texts(doc)
    (row,) = _fields(doc, "INDEX")
    assert row["stale"]
    api.run("batch", {"doc": str(doc), "ops": [{"op": "revision.reject", "all": True}]})
    assert _texts(doc) == plain and not _fields(doc, "REF") + _fields(doc, "INDEX")


def test_dry_run_is_byte_identical(tmp_path: Path) -> None:
    doc = make(tmp_path / "doc.docx")
    cap = api.run(
        "caption.add",
        {"doc": str(doc), "at": _at(doc, "Quarterly"), "label": "Table", "text": "R"},
    ).result
    before = doc.read_bytes()
    requests = [
        (
            "caption.add",
            {"at": _at(doc, "Sunrise"), "label": "Figure", "text": "S", "position": "above"},
        ),
        ("xref.add", {"at": _at(doc, "To table one:"), "to": cap["at"], "show": "text"}),
        ("xref.add", {"at": _at(doc, "To table one:"), "to": _at(doc, "Alpha"), "show": "page"}),
        ("index.mark", {"at": _at(doc, "Alpha text"), "entry": "Alpha"}),
        ("index.insert", {"after": _at(doc, "Index goes"), "title": ""}),
    ]
    for op, request in requests:
        dry = api.run(op, {"doc": str(doc), "dry_run": True, **request}).result
        assert dry["dry_run"] is True and doc.read_bytes() == before, op
        real = api.run(op, {"doc": str(doc), **request}).result
        assert real["footprint"] == dry["footprint"] and real["revision_ids"] == dry["revision_ids"]
        api.run("undo", {"doc": str(doc)})
        assert doc.read_bytes() == before, op
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".lock"]


def test_every_write_is_undone_byte_for_byte(tmp_path: Path) -> None:
    doc = make(tmp_path / "doc.docx")
    original = doc.read_bytes()
    steps: list[tuple[str, dict[str, str]]] = [
        ("caption.add", {"at": "Quarterly", "label": "Table", "text": "R"}),
        ("xref.add", {"at": "To table one:", "to": "Table 1", "show": "number"}),
        ("index.mark", {"at": "Alpha text", "entry": "Alpha"}),
        ("index.insert", {"after": "Index goes"}),
    ]
    seen = [original]
    for op, request in steps:
        resolved = {
            key: _at(doc, value) if key in ("at", "to", "after") else value
            for key, value in request.items()
        }
        api.run(op, {"doc": str(doc), **resolved})
        seen.append(doc.read_bytes())
        assert seen[-1] != seen[-2]
    for want in reversed(seen[:-1]):
        api.run("undo", {"doc": str(doc)})
        assert doc.read_bytes() == want


NUMBERING = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f"<w:numbering {NS}>"
    '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/>'
    '<w:lvl w:ilvl="0"><w:start w:val="3"/><w:numFmt w:val="decimal"/>'
    '<w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
)


def number_headings(doc: Path) -> None:
    """Number the Heading 1 paragraphs of ``doc``, counting from 3."""
    with zipfile.ZipFile(doc) as zf:
        members = {n: zf.read(n).decode() for n in zf.namelist()}
    members["word/document.xml"] = members["word/document.xml"].replace(
        '<w:pStyle w:val="Heading1"/>',
        '<w:pStyle w:val="Heading1"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>',
    )
    members["word/numbering.xml"] = NUMBERING
    members["[Content_Types].xml"] = members["[Content_Types].xml"].replace(
        "</Types>",
        '<Override PartName="/word/numbering.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.numbering+xml"/></Types>',
    )
    members["word/_rels/document.xml.rels"] = members["word/_rels/document.xml.rels"].replace(
        "</Relationships>",
        f'<Relationship Id="rId9" Type="{REL}/numbering" Target="numbering.xml"/></Relationships>',
    )
    with zipfile.ZipFile(doc, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_a_paragraph_without_a_number_has_none_to_refer_to(tmp_path: Path) -> None:
    doc = make(tmp_path / "doc.docx")
    before = doc.read_bytes()
    request = {"doc": str(doc), "at": _at(doc, "To heading:"), "to": _at(doc, "Alpha")}
    with pytest.raises(DocControlError) as err:
        api.run("xref.add", {**request, "show": "number"})
    assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT and "no number" in err.value.message
    assert doc.read_bytes() == before


def test_numbered_headings_are_referred_to_by_number(tmp_path: Path) -> None:
    _need_soffice()
    doc = make(tmp_path / "doc.docx")
    number_headings(doc)
    out = api.run(
        "xref.add",
        {"doc": str(doc), "at": _at(doc, "To heading:"), "to": _at(doc, "Alpha")},
    ).result
    assert out["code"].endswith("\\r \\h") and out["bookmark"].startswith("_Ref")
    assert api.run("fields.update", {"doc": str(doc)}).result["updated"][0]["after"] == ["3"]
    (row,) = _fields(doc, "REF")
    assert row["result"] == "3" and not row["stale"]


def test_refusals(tmp_path: Path) -> None:
    doc = make(tmp_path / "doc.docx")
    before = doc.read_bytes()
    table, closing, alpha = _at(doc, "Quarterly"), _at(doc, "To table one:"), _at(doc, "Alpha")

    def refuse(op: str, code: ErrorCode, **request: Any) -> DocControlError:
        with pytest.raises(DocControlError) as err:
            api.run(op, {"doc": str(doc), **request})
        assert err.value.code is code, err.value.message
        assert doc.read_bytes() == before
        return err.value

    good = {"at": table, "label": "Table", "text": "x"}
    refuse("caption.add", ErrorCode.SCHEMA_INVALID, **{**good, "label": "Chart"})
    refuse("caption.add", ErrorCode.SCHEMA_INVALID, **{**good, "position": "beside"})
    refuse("caption.add", ErrorCode.SCHEMA_INVALID, **{**good, "text": "two\nlines"})
    refuse("caption.add", ErrorCode.STALE_ADDRESS, **{**good, "at": "p3#00000000"})
    refuse("caption.add", ErrorCode.UNSUPPORTED_CONTENT, **{**good, "at": "fn1/p1#00000000"})
    refuse("caption.add", ErrorCode.DOC_CHANGED_ON_DISK, **good, expect={"sha256": "0" * 64})
    refuse("xref.add", ErrorCode.SCHEMA_INVALID, at=closing, to=alpha, show="all")
    refuse("xref.add", ErrorCode.SCHEMA_INVALID, at=f"{closing}:0-3", to=alpha, show="page")
    refuse("xref.add", ErrorCode.SCHEMA_INVALID, at=f"{closing}.s1", to=alpha, show="page")
    refuse("xref.add", ErrorCode.UNSUPPORTED_CONTENT, at=closing, to=closing, show="page")
    refuse("xref.add", ErrorCode.UNSUPPORTED_CONTENT, at=closing, to=alpha, show="number")
    refuse("xref.add", ErrorCode.UNSUPPORTED_CONTENT, at="en1/p1#00000000", to=alpha)
    refuse("xref.add", ErrorCode.STALE_ADDRESS, at=closing, to="p9#00000000", show="page")
    for entry in ("", ":Sub", "Term:", "a:b:c:d", "two\nlines"):
        refuse("index.mark", ErrorCode.SCHEMA_INVALID, at=alpha, entry=entry)
    refuse("index.mark", ErrorCode.UNSUPPORTED_CONTENT, at="hdr1.default/p1#00000000", entry="x")
    refuse("index.insert", ErrorCode.UNSUPPORTED_CONTENT, after="fn1/p1#00000000")
    refuse("index.insert", ErrorCode.STALE_ADDRESS, after="p2#00000000")
    missing = tmp_path / "missing.docx"
    with pytest.raises(DocControlError) as err:
        api.run("index.insert", {"doc": str(missing), "after": alpha})
    assert err.value.code is ErrorCode.DOC_NOT_FOUND


def test_a_text_reference_to_a_caption_shows_the_whole_caption(tmp_path: Path) -> None:
    _need_soffice()
    doc = make(tmp_path / "doc.docx")
    cap = api.run(
        "caption.add",
        {"doc": str(doc), "at": _at(doc, "Quarterly"), "label": "Table", "text": "Results"},
    ).result
    out = api.run(
        "xref.add",
        {"doc": str(doc), "at": _at(doc, "To table one:"), "to": cap["at"], "show": "text"},
    ).result
    # the caption's own bookmark covers its label and number only: this one covers it all
    assert out["bookmark"] != cap["bookmark"]
    api.run("fields.update", {"doc": str(doc)})
    (row,) = _fields(doc, "REF")
    assert row["result"] == "Table 1: Results" and not row["stale"]
