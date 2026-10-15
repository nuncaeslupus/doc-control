"""OpenDocument text (.odt) read as a document (T57): what is kept, what is left out."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.odt import MIMETYPE, paragraphs

NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"'
)
STYLES = (
    f"<office:document-styles {NS}><office:styles>"
    '<style:style style:name="Standard" style:family="paragraph"/>'
    '<style:style style:name="Scene_20_break" style:display-name="Scene break" '
    'style:family="paragraph"/></office:styles></office:document-styles>'
)
BODY = (
    '<text:h text:outline-level="2">Chapter <text:span>One</text:span></text:h>'
    '<text:p text:style-name="P1">a<text:s text:c="3"/>b<text:tab/>c<text:line-break/>d'
    "<text:note><text:note-body><text:p>footnote</text:p></text:note-body></text:note>"
    "<office:annotation><dc:creator>x</dc:creator><text:p>comment</text:p></office:annotation>"
    "</text:p>"
    '<text:p text:style-name="Standard">kept <text:a>link</text:a>'
    "<draw:frame><draw:text-box><text:p>boxed</text:p></draw:text-box></draw:frame></text:p>"
    "<text:list><text:list-item><text:p>item</text:p></text:list-item></text:list>"
    "<table:table><table:table-row><table:table-cell><text:p>cell</text:p></table:table-cell>"
    "</table:table-row></table:table>"
    "<text:p/>"
)
TRACKED = (
    "<text:tracked-changes><text:changed-region><text:deletion>"
    "<text:p>deleted text</text:p></text:deletion></text:changed-region></text:tracked-changes>"
)


def odt(
    body: str = BODY, *, mimetype: str = MIMETYPE, encrypted: bool = False, styles: str = ""
) -> bytes:
    import io

    content = (
        f"<office:document-content {NS}><office:automatic-styles>"
        '<style:style style:name="P1" style:family="paragraph" '
        f'style:parent-style-name="Scene_20_break"/>{styles}</office:automatic-styles>'
        f"<office:body><office:text>{TRACKED}{body}</office:text></office:body>"
        "</office:document-content>"
    )
    manifest = (
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
    )
    manifest += "<manifest:encryption-data/>" if encrypted else ""
    manifest += "</manifest:manifest>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        zf.writestr("content.xml", content)
        zf.writestr("styles.xml", STYLES)
        zf.writestr("META-INF/manifest.xml", manifest)
    return buf.getvalue()


def test_paragraphs_keep_the_main_story_only() -> None:
    assert paragraphs(odt()) == [
        ("Chapter One", 2, None),
        ("a   b\tc\nd", None, "Scene break"),
        ("kept link", None, None),
        ("item", None, None),
        ("cell", None, None),
        ("", None, None),
    ]


def test_read_ops_see_styles_headings_and_breaks(tmp_path: Path) -> None:
    path = tmp_path / "book.odt"
    path.write_bytes(odt())
    with api.open(path) as doc:
        rows = doc.paragraphs()
        reply = doc.reply("doc.info")
    assert reply.doc is not None and reply.doc["source"] == {"format": "odt"}
    assert rows[0]["heading_level"] == 2
    assert rows[1]["style_name"] == "Scene break"
    assert rows[1]["text"] == "a   b\tc\nd"
    assert [p.name for p in tmp_path.iterdir()] == ["book.odt"]


@pytest.mark.parametrize(
    "data",
    [odt(encrypted=True), odt(mimetype="application/vnd.oasis.opendocument.spreadsheet"), b"x"],
)
def test_unreadable_odt_is_not_a_document(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "bad.odt"
    path.write_bytes(data)
    with api.open(path) as doc, pytest.raises(DocControlError) as err:
        doc.run("doc.info")
    assert err.value.code is ErrorCode.NOT_A_DOCX


def test_writes_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "book.odt"
    path.write_bytes(odt())
    with api.open(path, store=tmp_path / "store") as doc, pytest.raises(DocControlError) as err:
        doc.run("doc.open")
    assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT
    assert not (tmp_path / "store").exists()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "<text:p>a<draw:rect><text:p>RECT</text:p></draw:rect>b</text:p>"
            "<draw:g><draw:rect><text:p>BODY</text:p></draw:rect></draw:g>",
            [("ab", None, None)],
        ),
        (
            "<text:p>x<text:ruby><text:ruby-base>base</text:ruby-base><text:ruby-text>gloss"
            "</text:ruby-text></text:ruby>y</text:p>",
            [("xbasey", None, None)],
        ),
        (
            '<text:p>a<text:s text:c="2000000000"/>b</text:p>',
            [("a" + " " * 1000 + "b", None, None)],
        ),
        ('<text:p>a<text:s text:c="x"/>b</text:p>', [("a b", None, None)]),
        ('<text:h text:outline-level="²">T</text:h>', [("T", 1, None)]),
    ],
)
def test_unusual_and_hostile_content(
    body: str, expected: list[tuple[str, int | None, None]]
) -> None:
    assert paragraphs(odt(body)) == expected


def test_deep_nesting_is_not_a_document(tmp_path: Path) -> None:
    deep = "<text:span>" * 5000 + "x" + "</text:span>" * 5000
    path = tmp_path / "deep.odt"
    path.write_bytes(odt(f"<text:p>{deep}</text:p>"))
    with api.open(path) as doc, pytest.raises(DocControlError) as err:
        doc.run("doc.info")
    assert err.value.code is ErrorCode.NOT_A_DOCX
    assert "nested" in str(err.value)


def test_style_names_with_bad_characters_still_read(tmp_path: Path) -> None:
    body = '<text:p text:style-name="P3">x</text:p><text:p text:style-name="P2">y</text:p>'
    data = odt(
        body,
        styles='<style:style style:name="P2" style:family="paragraph" '
        'style:parent-style-name="P1"/><style:style style:name="P3" style:family="paragraph" '
        'style:parent-style-name="A_00_B"/>',
    )
    path = tmp_path / "names.odt"
    path.write_bytes(data)
    with api.open(path) as doc:
        rows = doc.paragraphs()
    assert [r["style_name"] for r in rows] == ["AB", "Scene break"]


def test_long_automatic_style_chains_stay_linear() -> None:
    import time

    chain = "".join(
        f'<style:style style:name="S{k}" style:family="paragraph" '
        f'style:parent-style-name="S{k + 1}"/>'
        for k in range(20000)
    )
    start = time.perf_counter()
    paragraphs(odt('<text:p text:style-name="S0">x</text:p>', styles=chain))
    assert time.perf_counter() - start < 2.0


LAYOUT_STYLES = (
    '<style:style style:name="Title_20_centre" style:family="paragraph" '
    'style:parent-style-name="Scene_20_break"/>'
    '<style:style style:name="C1" style:family="paragraph" style:parent-style-name="Centred">'
    "</style:style>"
    '<style:style style:name="B1" style:family="paragraph" style:parent-style-name="Breaks"/>'
    '<style:style style:name="A1" style:family="paragraph">'
    '<style:paragraph-properties fo:break-after="page"/></style:style>'
)


def _layout_odt(body: str) -> bytes:
    named = (
        '<style:style style:name="Centred" style:family="paragraph">'
        '<style:paragraph-properties fo:text-align="center"/></style:style>'
        '<style:style style:name="Breaks" style:family="paragraph">'
        '<style:paragraph-properties fo:break-before="page"/></style:style>'
    )
    data = odt(body, styles=LAYOUT_STYLES)
    import io

    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for item in src.infolist():
            raw = src.read(item.filename)
            if item.filename == "styles.xml":
                raw = raw.replace(b"</office:styles>", named.encode() + b"</office:styles>")
            if item.filename in ("styles.xml", "content.xml"):
                raw = raw.replace(
                    b'xmlns:dc="',
                    b'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:'
                    b'xsl-fo-compatible:1.0" xmlns:dc="',
                )
            zf.writestr(item, raw)
    return buf.getvalue()


def test_layout_reads_page_marks_breaks_and_centring(tmp_path: Path) -> None:
    body = (
        '<text:p text:style-name="C1">Title</text:p>'
        "<text:soft-page-break/>"
        "<text:p>starts page two</text:p>"
        "<text:p>long<text:soft-page-break/> paragraph</text:p>"
        '<text:p text:style-name="B1">broken before</text:p>'
        '<text:p text:style-name="A1">breaks after</text:p>'
        "<text:p>after the break</text:p>"
    )
    path = tmp_path / "layout.odt"
    path.write_bytes(_layout_odt(body))
    with api.open(path) as doc:
        rows = doc.paragraphs()
        pages = doc.run("page.estimate", {})
    assert [r["centred"] for r in rows] == [True, False, False, False, False, False]
    assert [r["page_break_before"] for r in rows] == [False, False, False, True, False, True]
    assert [p["page"] for p in pages["paragraphs"]] == [1, 2, 2, 4, 4, 5]
    assert pages["rendered_marks"] == 2


def test_undefined_style_keeps_its_raw_name() -> None:
    body = '<text:p text:style-name="Chapter_20_Title">x</text:p>'
    assert paragraphs(odt(body)) == [("x", None, "Chapter Title")]
