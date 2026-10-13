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


def odt(body: str = BODY, *, mimetype: str = MIMETYPE, encrypted: bool = False) -> bytes:
    import io

    content = (
        f"<office:document-content {NS}><office:automatic-styles>"
        '<style:style style:name="P1" style:family="paragraph" '
        'style:parent-style-name="Scene_20_break"/></office:automatic-styles>'
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
        reply = doc.reply("info")
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
        doc.run("info")
    assert err.value.code is ErrorCode.NOT_A_DOCX


def test_writes_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "book.odt"
    path.write_bytes(odt())
    with api.open(path, store=tmp_path / "store") as doc, pytest.raises(DocControlError) as err:
        doc.run("open")
    assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT
    assert not (tmp_path / "store").exists()
