"""`field.list` (LAYOUT T6): fields are listed in every story, citations are never touched."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from tests.corpus.generator import generate
from tests.helpers import CONTENT_TYPES, RELS

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
ZOTERO_CODE = (
    ' ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"a1b2","properties":{"formattedCitation":'
    '"(Smith, 2020)","plainCitation":"(Smith, 2020)"},"citationItems":[{"id":1,"uris":'
    '["http://zotero.org/users/1/items/AB12"],"itemData":{"title":"A <b>title</b> & more"}}],'
    '"schema":"https://github.com/citation-style-language/schema/raw/master/csl-citation.json"} '
)
ZOTERO_RESULT = "(Smith, 2020)"
WORD_RESULT = "(Jones, 2019)"


def _run(text: str, tag: str = "w:t") -> str:
    return f'<w:r><{tag} xml:space="preserve">{text}</{tag}></w:r>'


def _char(kind: str) -> str:
    return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _zotero() -> str:
    code = _escape(ZOTERO_CODE)
    half = len(code) // 2
    return (
        _char("begin")
        + _run(code[:half], "w:instrText")  # the code is split across runs
        + _run(code[half:], "w:instrText")
        + _char("separate")
        + _run(ZOTERO_RESULT[:4])
        + _run(ZOTERO_RESULT[4:])
        + _char("end")
    )


def _word_complex() -> str:
    return (
        _char("begin")
        + _run(" CITATION Jones", "w:instrText")
        + _run("19 \\l 1033 ", "w:instrText")
        + _char("separate")
        + _run(WORD_RESULT)
        + _char("end")
    )


def _simple(code: str, result: str) -> str:
    return f'<w:fldSimple w:instr="{code}"><w:r><w:t>{result}</w:t></w:r></w:fldSimple>'


def _paragraph(*parts: str) -> str:
    return "<w:p>" + "".join(parts) + "</w:p>"


def _field(code: str, result: str) -> str:
    return (
        _char("begin") + _run(code, "w:instrText") + _char("separate") + _run(result) + _char("end")
    )


def _build(path: Path, body: str, extra: dict[str, str] | None = None) -> Path:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f"<w:document {NS}><w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", document)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles {NS}>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
    '<w:basedOn w:val="Normal"/></w:style>'
    '<w:style w:type="character" w:styleId="Strong"><w:name w:val="Strong"/></w:style>'
    '<w:style w:type="character" w:styleId="Emphasis"><w:name w:val="Emphasis"/></w:style>'
    "</w:styles>"
)
DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/styles" Target="styles.xml"/></Relationships>'
)


def _citation_doc(path: Path) -> Path:
    body = "".join(
        [
            _paragraph(_run("Intro "), _zotero(), _run(" and more text.")),
            _paragraph(_run("Word style "), _simple("CITATION Jones19 \\l 1033", WORD_RESULT)),
            _paragraph(_run("Complex "), _word_complex(), _run(" ends here.")),
            _paragraph(_run("A plain paragraph to edit.")),
            _paragraph(_run("Delete this one.")),
            _paragraph(_run("Move this one.")),
        ]
    )
    return _build(
        path,
        body,
        {"word/styles.xml": STYLES, "word/_rels/document.xml.rels": DOCUMENT_RELS},
    )


def _fields(doc: Path, **extra: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = api.run("field.list", {"doc": str(doc), **extra}).result["fields"]
    return rows


def _citations(doc: Path) -> list[tuple[str, str, str]]:
    return sorted((f["type"], f["code"], f["result"]) for f in _fields(doc) if f["citation"])


def _at(doc: Path, text: str) -> str:
    rows = api.run("para.list", {"doc": str(doc)}).result["paragraphs"]
    return str(next(r["at"] for r in rows if text in r["text"]))


def test_fields_list_reads_citations_split_across_runs(tmp_path: Path) -> None:
    doc = _citation_doc(tmp_path / "c.docx")
    rows = _fields(doc)
    assert [(r["type"], r["result"]) for r in rows] == [
        ("ADDIN ZOTERO_ITEM", ZOTERO_RESULT),
        ("CITATION", WORD_RESULT),
        ("CITATION", WORD_RESULT),
    ]
    assert rows[0]["code"] == _escape(ZOTERO_CODE).strip().replace("&amp;", "&").replace(
        "&lt;", "<"
    ).replace("&gt;", ">")
    assert all(r["citation"] and not r["stale"] for r in rows)
    assert _fields(doc, type="citation") == rows[1:]


def test_fields_list_nested_and_multi_paragraph_fields(tmp_path: Path) -> None:
    toc = (
        _paragraph(_char("begin"), _run(' TOC \\o "1-3" \\h ', "w:instrText"), _char("separate"))
        + _paragraph(_field(" PAGEREF _Toc1 ", "7"))
        + _paragraph(_run("Chapter one"), _char("end"))
    )
    tail = _paragraph(_char("begin"), _run(" PAGE ", "w:instrText"))
    outer, inner, dangling = _fields(_build(tmp_path / "t.docx", toc + tail))
    assert outer["type"] == "TOC" and outer["code"] == 'TOC \\o "1-3" \\h'
    assert outer["result"] == "7Chapter one" and not outer["stale"]
    assert inner["type"] == "PAGEREF" and inner["result"] == "7"
    assert dangling["type"] == "PAGE" and dangling["stale"]  # never closed, never calculated


def test_fields_list_text_boxes_comments_and_stale(tmp_path: Path) -> None:
    box = (
        "<w:r><w:pict><w:txbxContent>"
        + _paragraph(_field(" SEQ Figure ", "3"))
        + "</w:txbxContent></w:pict></w:r>"
    )
    body = _paragraph(box, _simple("PAGE", ""), _simple("NUMPAGES", "9"))
    comments = (
        f'<w:comments {NS}><w:comment w:id="4" w:author="A">'
        + _paragraph(_field(" REF x ", "x"))
        + "</w:comment></w:comments>"
    )
    doc = _build(tmp_path / "b.docx", body, {"word/comments.xml": comments})
    by_type = {r["type"]: r for r in _fields(doc)}
    assert by_type["SEQ"]["in"] == "textbox" and by_type["SEQ"]["result"] == "3"
    assert by_type["PAGE"]["stale"] and not by_type["NUMPAGES"]["stale"]
    assert by_type["REF"]["at"] == "comment:4" and by_type["REF"]["story"] == "comments"
    assert [r["type"] for r in _fields(doc, stories=["comments"])] == ["REF"]


def test_fields_list_mendeley_case_xe_and_comment_text_boxes(tmp_path: Path) -> None:
    mendeley = _paragraph(
        _char("begin"),
        _run(' ADDIN Mendeley Citation{"citationID":"x"} ', "w:instrText"),
        _char("separate"),
        _run("(Doe, 2021)"),
        _char("end"),
    )
    index_entry = _paragraph(_char("begin"), _run(' XE "term" ', "w:instrText"), _char("end"))
    box = (
        "<w:r><w:pict><w:txbxContent>"
        + _paragraph(_field(" SEQ Figure ", "3"))
        + "</w:txbxContent></w:pict></w:r>"
    )
    comments = (
        f'<w:comments {NS}><w:comment w:id="1" w:author="A">'
        + _paragraph(box)
        + "</w:comment></w:comments>"
    )
    doc = _build(tmp_path / "m.docx", mendeley + index_entry, {"word/comments.xml": comments})
    by_type = {r["type"]: r for r in _fields(doc)}
    assert by_type["ADDIN MENDELEY"]["citation"] is True
    assert _fields(doc, type="addin mendeley") == [by_type["ADDIN MENDELEY"]]
    assert by_type["XE"]["stale"] is False  # no result to go stale
    assert [r["type"] for r in _fields(doc, stories=["comments"])] == ["SEQ"]  # listed once


def test_fields_list_headers(tmp_path: Path) -> None:
    doc = tmp_path / "h.docx"
    doc.write_bytes(generate("headers", 0))
    rows = _fields(doc, stories=["headers"])
    assert rows and all(r["story"].startswith(("hdr", "ftr")) for r in rows)
    assert _fields(doc, stories=["body"]) == [r for r in _fields(doc) if r["story"] == "body"]


def _batches(doc: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    zotero, word, cmplx = _at(doc, "Intro"), _at(doc, "Word style"), _at(doc, "Complex")
    plain, gone, moved = _at(doc, "A plain"), _at(doc, "Delete this"), _at(doc, "Move this")
    return [
        ("text.replace before", [{"op": "text.replace", "at": zotero + ":0-5", "text": "Start"}]),
        ("text.replace after", [{"op": "text.replace", "at": zotero + ":30-34", "text": "less"}]),
        ("text.replace elsewhere", [{"op": "text.replace", "at": plain, "text": "Changed."}]),
        ("para.insert after", [{"op": "para.insert", "after": zotero, "text": "Inserted."}]),
        ("para.insert before", [{"op": "para.insert", "before": cmplx, "text": "Inserted."}]),
        ("para.delete", [{"op": "para.delete", "at": gone}]),
        ("para.move citation", [{"op": "para.move", "at": zotero, "after": moved}]),
        ("para.move other", [{"op": "para.move", "at": moved, "before": zotero}]),
        ("style.apply paragraph", [{"op": "style.apply", "at": zotero, "style": "Heading1"}]),
        ("style.apply range", [{"op": "style.apply", "at": zotero + ":0-20", "style": "Strong"}]),
        ("format.apply paragraph", [{"op": "format.apply", "at": zotero, "bold": True}]),
        (
            "format.apply range",
            [{"op": "format.apply", "at": zotero + ":3-12", "italic": True, "size_pt": 14}],
        ),
        (
            "format.apply run",
            [{"op": "format.apply", "at": word + ".." + cmplx, "bold": True}],
        ),
        ("comment.add range", [{"op": "comment.add", "at": zotero + ":2-12", "text": "Check."}]),
        ("comment.add paragraph", [{"op": "comment.add", "at": cmplx, "text": "Hm."}]),
        ("style.create", [{"op": "style.create", "name": "Quote Block", "based_on": "Normal"}]),
        ("style.update", [{"op": "style.update", "style": "Heading1", "run": {"bold": True}}]),
        ("style.rename", [{"op": "style.rename", "style": "Strong", "name": "Strong Text"}]),
        ("style.merge", [{"op": "style.merge", "from": "Strong", "into": "Emphasis"}]),
        (
            "mixed batch",
            [
                {"op": "text.replace", "at": zotero + ":0-5", "text": "Begin"},
                {"op": "format.apply", "at": word, "italic": True},
                {"op": "comment.add", "at": cmplx, "text": "Note."},
                {"op": "para.delete", "at": gone},
            ],
        ),
    ]


def test_write_ops_never_change_citation_fields(tmp_path: Path) -> None:
    source = _citation_doc(tmp_path / "source.docx")
    before = _citations(source)
    assert len(before) == 3 and {t for t, _, _ in before} == {"ADDIN ZOTERO_ITEM", "CITATION"}
    failures: list[str] = []
    refused: list[str] = []
    for n, (label, ops) in enumerate(_batches(source)):
        doc = tmp_path / f"case{n}.docx"
        doc.write_bytes(source.read_bytes())
        try:
            api.run("doc.batch", {"doc": str(doc), "ops": ops})
        except DocControlError as err:
            # Refusing a paragraph that holds a field is safe, provided nothing was written.
            assert err.code is ErrorCode.UNSUPPORTED_CONTENT, (label, err)
            assert doc.read_bytes() == source.read_bytes(), label
            refused.append(label)
        steps = [("after the edit", doc)]
        for action in ("accept", "reject"):  # resolving the revisions must not touch them either
            resolved = tmp_path / f"case{n}-{action}.docx"
            resolved.write_bytes(doc.read_bytes())
            try:
                api.run(
                    "doc.batch",
                    {"doc": str(resolved), "ops": [{"op": f"revision.{action}", "all": True}]},
                )
            except DocControlError:  # nothing to resolve in an untracked change
                continue
            steps.append((f"after revision.{action}", resolved))
        failures += [f"{label} {when}" for when, path in steps if _citations(path) != before]
    assert not failures, failures
    # The edits that do not touch a field's paragraph really ran, so the check is not vacuous.
    assert not {"text.replace elsewhere", "para.insert after", "style.rename"} & set(refused)
