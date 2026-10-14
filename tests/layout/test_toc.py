"""`toc.insert` and `fields.update` (LAYOUT T7): a table of contents filled through headless
LibreOffice lists the pages its headings render on; without LibreOffice the update is refused
and the file is left alone."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from tests.corpus.validator import REQUIRE_ENV, soffice_path
from tests.helpers import CONTENT_TYPES, RELS, soffice_dir

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.{}+xml"
HEADINGS = ["Alpha", "Beta", "Beta sub", "Gamma", "Delta"]
#: heading -> page, with one page for the contents, then a page each (Gamma two pages on).
EXPECTED_PAGES = {"Alpha": 2, "Beta": 3, "Beta sub": 3, "Gamma": 5, "Delta": 6}


def _heading(text: str, level: int = 1) -> str:
    return (
        f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
    )


def _para(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def _heading_style(level: int) -> str:
    return (
        f'<w:style w:type="paragraph" w:styleId="Heading{level}"><w:name w:val="heading {level}"/>'
        f'<w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="{level - 1}"/></w:pPr></w:style>'
    )


def _build(path: Path, body: str, settings: bool = True) -> Path:
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:styles {NS}><w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/></w:style>' + _heading_style(1) + _heading_style(2) + "</w:styles>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{REL}/styles" Target="styles.xml"/>'
        f'<Relationship Id="rId2" Type="{REL}/settings" Target="settings.xml"/></Relationships>'
    )
    types = CONTENT_TYPES.replace(
        "</Types>",
        f'<Override PartName="/word/styles.xml" ContentType="{CT.format("styles")}"/>'
        f'<Override PartName="/word/settings.xml" ContentType="{CT.format("settings")}"/></Types>',
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {NS}><w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", types)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/_rels/document.xml.rels", rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles)
        zf.writestr(
            "word/settings.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:settings {NS}><w:zoom w:percent="100"/><w:compat/></w:settings>',
        )
    return path


def book(path: Path) -> Path:
    """A front paragraph (the contents go after it), then headings on pages 2, 3, 3, 5, 6."""
    body = _para("Front matter") + _page_break()
    body += _heading("Alpha") + _para("First chapter text.") + _page_break()
    body += _heading("Beta") + _para("Second chapter text.")
    body += _heading("Beta sub", 2) + _para("A subsection.") + _page_break()
    body += _page_break()  # page 4 stays empty
    body += _heading("Gamma") + _para("Third chapter text.") + _page_break()
    body += _heading("Delta") + _para("Last chapter text.")
    return _build(path, body)


def _need_soffice() -> None:
    if soffice_path() is None:
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but LibreOffice (soffice) is not available")
        pytest.skip("LibreOffice (soffice) is not available")


def _front(doc: Path) -> str:
    rows = api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]
    return str(next(r["at"] for r in rows if r["text"] == "Front matter"))


def _toc_rows(doc: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = api.run("fields.list", {"doc": str(doc), "type": "TOC"}).result[
        "fields"
    ]
    return rows


def _pages(pdf: Path) -> list[str]:
    text = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True
    ).stdout
    return text.split("\f")[:-1] if text.endswith("\f") else text.split("\f")


def render_pages(doc: Path, tmp: Path) -> list[str]:
    """The text of every page of ``doc`` as LibreOffice lays it out."""
    soffice = soffice_path()
    assert soffice is not None
    with soffice_dir(tmp / "lo") as work:
        work.mkdir(parents=True, exist_ok=True)
        source = work / "doc.docx"
        shutil.copyfile(doc, source)
        subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={(work / 'profile').resolve().as_uri()}",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                str(work),
                str(source),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        return _pages(work / "doc.pdf")


def toc_page_mismatches(pages: list[str]) -> list[str]:
    """Headings whose TOC entry names another page than the one the heading renders on."""
    bad: list[str] = []
    for title in HEADINGS:
        rendered = [
            n + 1
            for n, text in enumerate(pages)
            if any(line.strip() == title for line in text.splitlines())
        ]
        listed = [
            int(m[1])
            for text in pages
            for line in text.splitlines()
            if (m := re.match(rf"^(?:.*contents\.)?\s*{re.escape(title)}[\s.]*?(\d+)\s*$", line))
        ]
        if rendered != [EXPECTED_PAGES[title]] or listed != rendered:
            bad.append(f"{title}: rendered on {rendered}, listed as {listed}")
    return bad


def test_toc_pages_match_rendered_pages(tmp_path: Path) -> None:
    _need_soffice()
    if shutil.which("pdftotext") is None:
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but pdftotext is not available")
        pytest.skip("pdftotext is not available")
    doc = book(tmp_path / "book.docx")
    inserted = api.run("toc.insert", {"doc": str(doc), "after": _front(doc)}).result
    assert inserted["code"] == 'TOC \\o "1-3" \\h \\z \\u'
    done = api.run("fields.update", {"doc": str(doc)}).result
    assert [u["type"] for u in done["updated"]] == ["TOC"] and done["skipped"] == []
    assert done["updated"][0]["after"] == [
        "Alpha\t2",
        "Beta\t3",
        "Beta sub\t3",
        "Gamma\t5",
        "Delta\t6",
    ]
    assert toc_page_mismatches(render_pages(doc, tmp_path)) == []
    # the field is current: a second update changes nothing
    again = api.run("fields.update", {"doc": str(doc)}).result
    assert again["updated"] == [] and again["unchanged"] == 1 and again["written"] is False


def test_toc_insert_is_a_tracked_stale_field(tmp_path: Path) -> None:
    doc = book(tmp_path / "book.docx")
    before = api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]
    out = api.run("toc.insert", {"doc": str(doc), "after": _front(doc), "levels": "1-2"}).result
    assert out["code"] == 'TOC \\o "1-2" \\h \\z \\u' and out["op_id"]
    (row,) = _toc_rows(doc)
    assert row["at"] == out["at"] and row["stale"] and row["code"] == out["code"]
    after = api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]
    assert [r["text"] for r in after[:3]] == ["Front matter", "Contents", row["result"]]
    assert len(after) == len(before) + 3
    kinds = {r["type"] for r in api.run("revisions.list", {"doc": str(doc)}).result["revisions"]}
    assert kinds == {"insert", "mark_insert"}
    # rejecting the call's revisions takes the contents out again
    api.run("batch", {"doc": str(doc), "ops": [{"op": "revision.reject", "all": True}]})
    assert [
        r["text"] for r in api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]
    ] == [r["text"] for r in before]


def test_toc_insert_dry_run_and_refusals(tmp_path: Path) -> None:
    doc = book(tmp_path / "book.docx")
    original = doc.read_bytes()
    front = _front(doc)
    dry = api.run("toc.insert", {"doc": str(doc), "after": front, "dry_run": True}).result
    assert dry["dry_run"] is True and "op_id" not in dry and doc.read_bytes() == original
    for extra, code in (
        ({"levels": "3-1"}, ErrorCode.SCHEMA_INVALID),
        ({"after": "fn1/p1#00000000"}, ErrorCode.UNSUPPORTED_CONTENT),
        ({"after": "p99#00000000"}, ErrorCode.STALE_ADDRESS),
    ):
        with pytest.raises(DocControlError) as err:
            api.run("toc.insert", {"doc": str(doc), "after": front, **extra})
        assert err.value.code is code
    assert doc.read_bytes() == original


def test_toc_insert_without_title_and_undo_restores_bytes(tmp_path: Path) -> None:
    doc = book(tmp_path / "book.docx")
    original = doc.read_bytes()
    out = api.run("toc.insert", {"doc": str(doc), "after": _front(doc), "title": ""}).result
    assert out["title"] is None
    texts = [r["text"] for r in api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]]
    assert "Contents" not in texts and doc.read_bytes() != original
    api.run("undo", {"doc": str(doc)})
    assert doc.read_bytes() == original


def test_update_without_libreoffice_is_refused_and_leaves_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = book(tmp_path / "book.docx")
    api.run("toc.insert", {"doc": str(doc), "after": _front(doc)})
    before = doc.read_bytes()
    monkeypatch.setenv("DOC_CONTROL_NO_SOFFICE", "1")
    with pytest.raises(DocControlError) as err:
        api.run("fields.update", {"doc": str(doc)})
    assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE
    (stale,) = err.value.details["stale"]
    assert stale["type"] == "TOC" and stale["at"] in err.value.message
    assert doc.read_bytes() == before
    # no field to recompute: nothing to refuse
    plain = _build(tmp_path / "plain.docx", _para("No fields here."))
    assert api.run("fields.update", {"doc": str(plain)}).result["updated"] == []


def test_set_update_on_open(tmp_path: Path) -> None:
    doc = book(tmp_path / "book.docx")
    original = doc.read_bytes()
    out = api.run("fields.update", {"doc": str(doc), "set_update_on_open": True}).result
    assert out["changed"] is True and out["op_id"]
    with zipfile.ZipFile(doc) as zf:
        settings = zf.read("word/settings.xml").decode()
    # CT_Settings order: updateFields comes before compat
    assert '<w:updateFields w:val="true"/><w:compat/>' in settings
    again = api.run("fields.update", {"doc": str(doc), "set_update_on_open": True}).result
    assert again["changed"] is False and "op_id" not in again
    api.run("undo", {"doc": str(doc)})
    assert doc.read_bytes() == original


def _numpages_doc(path: Path) -> Path:
    field = (
        '<w:p><w:r><w:t xml:space="preserve">Pages: </w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> NUMPAGES </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:rPr><w:b/></w:rPr><w:t>1</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    return _build(path, field + _page_break() + _para("two") + _page_break() + _para("three"))


def test_update_rewrites_a_one_paragraph_result_and_undo_is_exact(tmp_path: Path) -> None:
    _need_soffice()
    doc = _numpages_doc(tmp_path / "n.docx")
    original = doc.read_bytes()
    out = api.run("fields.update", {"doc": str(doc)}).result
    (change,) = out["updated"]
    assert change["type"] == "NUMPAGES" and change["before"] == ["1"] and change["after"] == ["3"]
    (row,) = api.run("fields.list", {"doc": str(doc)}).result["fields"]
    assert row["result"] == "3"  # the live result: the old one is a tracked deletion
    with zipfile.ZipFile(doc) as zf:
        xml = zf.read("word/document.xml").decode()
    assert "<w:delText>1</w:delText>" in xml and "<w:b />" in xml.replace("<w:b/>", "<w:b />")
    assert "Pages: " in xml and "w:dirty" not in xml
    api.run("undo", {"doc": str(doc)})
    assert doc.read_bytes() == original


def test_update_then_undo_a_toc_is_byte_for_byte(tmp_path: Path) -> None:
    _need_soffice()
    doc = book(tmp_path / "book.docx")
    original = doc.read_bytes()
    api.run("toc.insert", {"doc": str(doc), "after": _front(doc)})
    inserted = doc.read_bytes()
    api.run("fields.update", {"doc": str(doc)})
    assert doc.read_bytes() != inserted
    (row,) = _toc_rows(doc)
    assert row["result"] == "Alpha2Beta3Beta sub3Gamma5Delta6" and not row["stale"]
    api.run("undo", {"doc": str(doc)})
    assert doc.read_bytes() == inserted
    api.run("undo", {"doc": str(doc)})
    assert doc.read_bytes() == original


def test_update_touches_only_the_result(tmp_path: Path) -> None:
    _need_soffice()
    doc = book(tmp_path / "book.docx")
    api.run("toc.insert", {"doc": str(doc), "after": _front(doc)})
    before = {
        r["at"]: r["text"]
        for r in api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]
    }
    texts_before = [t for t in before.values() if t.startswith(("Alpha", "Beta", "Gamma"))]
    api.run("fields.update", {"doc": str(doc)})
    after = [r["text"] for r in api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]]
    # every paragraph outside the contents survives as it was; the entries are new paragraphs
    for text in (
        "Front matter",
        "First chapter text.",
        "Second chapter text.",
        "Last chapter text.",
    ):
        assert text in after
    assert len([t for t in after if t.startswith(("Alpha\t", "Beta\t", "Gamma\t", "Delta\t"))]) == 4
    assert texts_before  # the headings were there before and after


def _texts(doc: Path) -> list[str]:
    return [r["text"] for r in api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]]


def test_update_is_rejectable_and_accepting_leaves_the_new_result(tmp_path: Path) -> None:
    _need_soffice()
    doc = book(tmp_path / "book.docx")
    api.run("toc.insert", {"doc": str(doc), "after": _front(doc)})
    api.run("batch", {"doc": str(doc), "ops": [{"op": "revision.accept", "all": True}]})
    settled = _texts(doc)
    api.run("fields.update", {"doc": str(doc), "author": "Updater"})
    assert _texts(doc) != settled
    api.run("batch", {"doc": str(doc), "ops": [{"op": "revision.reject", "author": "Updater"}]})
    assert _texts(doc) == settled
    assert _toc_rows(doc)[0]["result"].startswith("Update the field")
    api.run("fields.update", {"doc": str(doc), "author": "Updater"})
    api.run("batch", {"doc": str(doc), "ops": [{"op": "revision.accept", "all": True}]})
    (row,) = _toc_rows(doc)
    assert row["result"] == "Alpha2Beta3Beta sub3Gamma5Delta6" and not row["stale"]
    assert _texts(doc)[:7] == [
        "Front matter",
        "Contents",
        "Alpha\t2",
        "Beta\t3",
        "Beta sub\t3",
        "Gamma\t5",
        "Delta\t6",
    ]


def _toc_code(code: str = ' TOC \\o "1-3" \\h ') -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve">{code}</w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    )


def _end() -> str:
    return '<w:r><w:fldChar w:fldCharType="end"/></w:r>'


def _entry(text: str, page: str) -> str:
    return f"<w:r><w:t>{text}</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>{page}</w:t></w:r>"


def _headings_after(body: str) -> str:
    return body + _page_break() + _heading("Alpha") + _page_break() + _heading("Beta")


def test_update_rewrites_a_word_style_multi_paragraph_toc(tmp_path: Path) -> None:
    _need_soffice()
    toc = (
        f"<w:p>{_toc_code()}{_entry('Old one', '9')}</w:p>"
        f"<w:p>{_entry('Old two', '9')}</w:p>"
        f"<w:p>{_entry('Old three', '9')}{_end()}</w:p>"
    )
    doc = _build(tmp_path / "w.docx", _headings_after(toc))
    out = api.run("fields.update", {"doc": str(doc)}).result
    (change,) = out["updated"]
    assert change["before"] == ["Old one\t9", "Old two\t9", "Old three\t9"]
    assert change["after"] == ["Alpha\t2", "Beta\t3"]
    (row,) = _toc_rows(doc)
    assert row["result"] == "Alpha2Beta3"
    api.run("batch", {"doc": str(doc), "ops": [{"op": "revision.accept", "all": True}]})
    # the old entries are gone; one paragraph still holds the end of the field
    assert _texts(doc)[:4] == ["Alpha\t2", "Beta\t3", "", ""]


def test_update_skips_what_it_cannot_split(tmp_path: Path) -> None:
    _need_soffice()
    one_paragraph = f"<w:p>{_toc_code()}{_entry('Old', '9')}{_end()}</w:p>"
    doc = _build(tmp_path / "s.docx", _headings_after(one_paragraph))
    before = doc.read_bytes()
    out = api.run("fields.update", {"doc": str(doc)}).result
    assert out["updated"] == [] and out["written"] is False
    (skip,) = out["skipped"]
    assert skip["type"] == "TOC" and "several lines" in skip["reason"]
    assert doc.read_bytes() == before
