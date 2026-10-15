"""Style writes and JSON style sets.

The gate ``style_definition_diffs_after_roundtrip == 0``: exporting a document's styles
as a style set and installing it into a blank document reproduces every definition.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.styles import load_styles
from doc_control.ooxml.zipio import Package
from doc_control.ops.batch import batch
from doc_control.ops.styleset import styleset_export, styleset_install
from doc_control.ops.stylewrite import apply_style_ops
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import KINDS, generate
from tests.corpus.private import private_documents
from tests.corpus.structural import validate_structure
from tests.corpus.validator import require_soffice, validate_documents
from tests.helpers import make_docx

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def defs(source: Path | bytes, tmp: Path | None = None) -> dict[str, tuple[Any, ...]]:
    if isinstance(source, bytes):
        assert tmp is not None
        path = tmp / "probe.docx"
        path.write_bytes(source)
    else:
        path = source
    table = load_styles(Package(path))
    return {
        s.id: (
            s.name,
            s.type,
            s.based_on,
            s.next,
            s.link,
            s.default,
            s.custom,
            s.quick,
            s.ppr,
            s.rpr,
        )
        for s in table.styles
    }


def roundtrip_diffs(data: bytes, tmp: Path) -> int:
    """Differences between a document's style definitions and the ones a blank document gets."""
    src = tmp / "src.docx"
    src.write_bytes(data)
    exported = styleset_export({"doc": str(src)}).result["styleset"]  # type: ignore[union-attr]
    blank = make_docx(tmp / "blank.docx", "")
    styleset_install({"doc": str(blank), "styleset": exported})
    before, after = defs(src), defs(blank)
    return sum(1 for sid in before if after.get(sid) != before[sid]) + sum(
        1 for sid in after if sid not in before
    )


def test_export_then_install_reproduces_style_definitions(tmp_path: Path) -> None:
    docs = {f"{kind}-0": generate(kind, 0) for kind in KINDS}
    docs.update({f"private-{doc_id}": data for doc_id, data in private_documents()})
    total = styles = 0
    for doc_id, data in docs.items():
        work = tmp_path / doc_id
        work.mkdir()
        diffs = roundtrip_diffs(data, work)
        styles += len(defs(data, work))
        total += diffs
        print(f"{doc_id}: style_definition_diffs_after_roundtrip = {diffs}")
    print(
        f"style_definition_diffs_after_roundtrip == {total} "
        f"({len(docs)} documents, {styles} style definitions)"
    )
    assert total == 0


def test_installed_documents_pass_lint_and_libreoffice(tmp_path: Path) -> None:
    require_soffice()
    out: dict[str, bytes] = {}
    for kind in ("headings", "tables", "kitchen_sink"):
        src = tmp_path / f"{kind}.docx"
        src.write_bytes(generate(kind, 1))
        target = tmp_path / f"{kind}-target.docx"
        target.write_bytes(generate("plain", 3))
        exported = styleset_export({"doc": str(src)}).result["styleset"]  # type: ignore[union-attr]
        styleset_install({"doc": str(target), "styleset": exported})
        apply_style_ops(
            target,
            [
                {
                    "op": "style.create",
                    "name": "House Body",
                    "based_on": "Normal",
                    "run": {"font": "Book Antiqua", "size_pt": 11.5},
                    "paragraph": {"alignment": "both", "spacing": {"after": 0}},
                },
                {"op": "style.rename", "style": "Quote", "id": "BlockQuote"},
                {"op": "style.merge", "from": "Title", "into": "Heading1"},
            ],
        )
        out[kind] = target.read_bytes()
        assert validate_structure(out[kind]) == []
    results = validate_documents(out, tmp_path)
    assert results == {kind: [] for kind in out}


# -- fixtures for the behaviour tests -----------------------------------------------

STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {W}><w:docDefaults><w:pPrDefault><w:pPr><w:spacing w:after="120"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:latentStyles w:defLockedState="0" w:count="2"><w:lsdException w:name="heading 1" w:uiPriority="9"/></w:latentStyles>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Body"><w:name w:val="Body"/><w:basedOn w:val="Normal"/><w:next w:val="Body"/><w:link w:val="BodyChar"/><w:qFormat/><w:pPr><w:widowControl/><w:jc w:val="both"/></w:pPr><w:rPr><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="character" w:customStyle="1" w:styleId="BodyChar"><w:name w:val="Body Char"/><w:link w:val="Body"/><w:rPr><w:i/></w:rPr></w:style>
<w:style w:type="paragraph" w:customStyle="1" w:styleId="Old"><w:name w:val="Old Style"/><w:basedOn w:val="Body"/><w:next w:val="Old"/></w:style>
<w:style w:type="paragraph" w:customStyle="1" w:styleId="Child"><w:name w:val="Child"/><w:basedOn w:val="Old"/></w:style>
<w:style w:type="table" w:styleId="Grid"><w:name w:val="Grid"/><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4"/></w:tblBorders></w:tblPr></w:style>
</w:styles>"""


def _part(body: str, root: str = "document") -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?><w:{root} {W}>{body}</w:{root}>'.encode()


def _para(style: str, text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    path = make_docx(
        tmp_path / "d.docx",
        "plain",
        extra={
            "word/styles.xml": STYLES.encode(),
            "word/header1.xml": _part(_para("Old", "header"), "hdr"),
            "word/footnotes.xml": _part(
                f'<w:footnote w:id="1">{_para("Old", "note")}</w:footnote>', "footnotes"
            ),
        },
    )
    # Real paragraphs with styles, and a table style outside any paragraph.
    with zipfile.ZipFile(path) as zf:
        parts = {n: zf.read(n) for n in zf.namelist()}
    body = (
        _para("Old", "one")
        + _para("Body", "two")
        + _para("Old", "three")
        + '<w:tbl><w:tblPr><w:tblStyle w:val="Grid"/></w:tblPr><w:tr><w:tc>'
        + _para("Child", "cell")
        + "</w:tc></w:tr></w:tbl>"
    )
    # The footnote must be real: referenced from the body, related and typed.
    body += '<w:p><w:r><w:t>noted</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r></w:p>'
    parts["word/document.xml"] = _part(f"<w:body>{body}</w:body>")
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
        b"</Types>",
        b'<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-'
        b'officedocument.wordprocessingml.footnotes+xml"/></Types>',
    )
    parts["word/_rels/document.xml.rels"] = (
        b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
        b'openxmlformats.org/package/2006/relationships"><Relationship Id="rId9" Type="'
        b'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" '
        b'Target="footnotes.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return path


def text_of(path: Path, part: str) -> str:
    return Package(path).read(part).decode("utf-8")


def ops(path: Path, *items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return apply_style_ops(path, list(items), **extra)


def test_registered_and_batch_kinds() -> None:
    registry = load_operations()
    for name in ("style.create", "style.update", "style.rename", "style.merge", "styleset.install"):
        assert registry[name].kind == "write"
    assert registry["styleset.export"].kind == "read"
    assert "style.merge" in REGISTRY["doc.batch"].schema["properties"]["ops"]["description"]


def test_create_adds_one_block_and_keeps_the_rest(doc: Path) -> None:
    before = text_of(doc, "word/styles.xml")
    result = ops(
        doc,
        {
            "op": "style.create",
            "name": "House Body",
            "based_on": "Body",
            "quick": True,
            "run": {"font": "Book Antiqua", "size_pt": 11.5, "bold": False},
            "paragraph": {"indent": {"first_line": 340}, "keep_next": True},
        },
    )
    after = text_of(doc, "word/styles.xml")
    assert result["applied"][0]["style"] == "HouseBody"
    assert after.startswith(before[: before.rindex("</w:styles>")])  # every old byte kept
    got = defs(doc)["HouseBody"]
    assert got[0] == "House Body" and got[2] == "Body" and got[6] is True and got[7] is True
    assert got[8] == {"indent": {"first_line": 340}, "keep_next": True}
    assert got[9] == {"bold": False, "font": "Book Antiqua", "size_pt": 11.5}
    # in schema order: keepNext before ind
    assert after.index("<w:keepNext") < after.index("<w:ind")
    assert Document(str(doc)).styles.find("house body") is not None


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ({"name": "Body"}, ErrorCode.STYLE_CONFLICT),
        ({"name": "Other", "id": "Old"}, ErrorCode.STYLE_CONFLICT),
        ({"name": "Other", "based_on": "Nope"}, ErrorCode.STYLE_NOT_FOUND),
        ({"name": "Other", "run": {"sparkle": 1}}, ErrorCode.SCHEMA_INVALID),
        ({"name": "Other", "run": {"size_pt": 11.3}}, ErrorCode.SCHEMA_INVALID),
        ({"name": "Other", "default": True}, ErrorCode.STYLE_CONFLICT),
    ],
)
def test_create_refusals_leave_the_document_alone(
    doc: Path, spec: dict[str, Any], code: ErrorCode
) -> None:
    before = doc.read_bytes()
    with pytest.raises(DocControlError) as err:
        ops(doc, {"op": "style.create", **spec})
    assert err.value.code is code
    assert doc.read_bytes() == before


def test_update_keeps_unmodelled_properties_and_removes_on_null(doc: Path) -> None:
    ops(
        doc,
        {
            "op": "style.update",
            "style": "Body",
            "run": {"size_pt": 12, "bold": True},
            "paragraph": {"alignment": None, "spacing": {"before": 60}},
            "next": "",
            "based_on": "Normal",
        },
    )
    text = text_of(doc, "word/styles.xml")
    assert "<w:widowControl" in text  # not in the vocabulary: untouched
    got = defs(doc)["Body"]
    assert got[8] == {"spacing": {"before": 60}} and got[3] is None
    assert got[9] == {"bold": True, "size_pt": 12.0}
    assert text_of(doc, "word/document.xml") == Package(doc).read("word/document.xml").decode()


def test_update_based_on_cycle_is_refused(doc: Path) -> None:
    with pytest.raises(DocControlError) as err:
        ops(doc, {"op": "style.update", "style": "Body", "based_on": "Child"})
    assert err.value.code is ErrorCode.STYLE_CONFLICT


def test_rename_name_only_touches_nothing_else(doc: Path) -> None:
    sha_doc = Package(doc).read("word/document.xml")
    ops(doc, {"op": "style.rename", "style": "Old Style", "name": "Legacy"})
    assert defs(doc)["Old"][0] == "Legacy"
    assert Package(doc).read("word/document.xml") == sha_doc


def test_rename_id_follows_references_in_every_part(doc: Path) -> None:
    result = ops(doc, {"op": "style.rename", "style": "Old", "id": "Legacy", "name": "Legacy"})
    assert result["paragraphs_changed"] == 2
    styles = defs(doc)
    assert "Old" not in styles and styles["Legacy"][2] == "Body" and styles["Legacy"][3] == "Legacy"
    assert styles["Child"][2] == "Legacy"
    for part in ("word/document.xml", "word/header1.xml", "word/footnotes.xml"):
        text = text_of(doc, part)
        assert 'w:val="Old"' not in text and 'w:val="Legacy"' in text
    assert 'w:val="Body"' in text_of(doc, "word/document.xml")


def test_rename_conflicts(doc: Path) -> None:
    for spec in (
        {"style": "Old", "name": "body"},
        {"style": "Old", "id": "Child"},
        {"style": "Old"},
    ):
        with pytest.raises(DocControlError):
            ops(doc, {"op": "style.rename", **spec})


def test_merge_repoints_paragraphs_headers_footnotes_and_styles(doc: Path) -> None:
    result = ops(doc, {"op": "style.merge", "from": "Old Style", "into": "Body"})
    assert result["applied"][0]["merged"] == ["Old"]
    styles = defs(doc)
    assert "Old" not in styles
    assert styles["Child"][2] == "Body"
    for part in ("word/document.xml", "word/header1.xml", "word/footnotes.xml"):
        assert 'w:val="Old"' not in text_of(doc, part)
    assert text_of(doc, "word/document.xml").count('w:val="Body"') == 3


def test_merge_into_the_style_it_was_based_on_and_links(doc: Path) -> None:
    ops(doc, {"op": "style.merge", "from": "Body", "into": "Old"})
    styles = defs(doc)
    assert "Body" not in styles
    assert styles["Old"][2] == "Normal"  # re-based on the merged style's own parent
    assert styles["Old"][3] == "Old"
    assert styles["BodyChar"][4] is None  # its link partner is gone


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ({"from": "Normal", "into": "Body"}, ErrorCode.STYLE_CONFLICT),
        ({"from": "Body", "into": "Body"}, ErrorCode.STYLE_CONFLICT),
        ({"from": "BodyChar", "into": "Body"}, ErrorCode.STYLE_CONFLICT),
        ({"from": "Grid", "into": "Grid"}, ErrorCode.STYLE_CONFLICT),
        ({"from": "Nope", "into": "Body"}, ErrorCode.STYLE_NOT_FOUND),
    ],
)
def test_merge_refusals(doc: Path, spec: dict[str, Any], code: ErrorCode) -> None:
    before = doc.read_bytes()
    with pytest.raises(DocControlError) as err:
        ops(doc, {"op": "style.merge", **spec})
    assert err.value.code is code
    assert doc.read_bytes() == before


def test_renaming_a_table_style_in_use_is_unsupported(doc: Path) -> None:
    before = doc.read_bytes()
    with pytest.raises(DocControlError) as err:
        ops(doc, {"op": "style.rename", "style": "Grid", "id": "Grid2"})
    assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT
    assert doc.read_bytes() == before


def test_latent_styles_and_doc_defaults_survive(doc: Path) -> None:
    ops(doc, {"op": "style.create", "name": "Heading 1", "id": "Heading1"})
    text = text_of(doc, "word/styles.xml")
    assert '<w:lsdException w:name="heading 1" w:uiPriority="9"/>' in text
    assert '<w:spacing w:after="120"/>' in text


def test_batch_runs_style_operations_and_refuses_mixing(doc: Path) -> None:
    reply = batch(
        {
            "doc": str(doc),
            "ops": [
                {"op": "style.create", "name": "A"},
                {"op": "style.update", "style": "A", "run": {"italic": True}},
            ],
        }
    )
    assert reply.result["tracked"] is False  # type: ignore[union-attr]
    assert defs(doc)["A"][9] == {"italic": True}
    before = doc.read_bytes()
    with pytest.raises(DocControlError) as err:
        batch(
            {
                "doc": str(doc),
                "ops": [
                    {"op": "style.create", "name": "B"},
                    {"op": "para.delete", "at": "p1#00000000"},
                ],
            }
        )
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert doc.read_bytes() == before


def test_dry_run_writes_nothing_and_expect_is_checked(doc: Path) -> None:
    before = doc.read_bytes()
    result = ops(doc, {"op": "style.merge", "from": "Old", "into": "Body"}, dry_run=True)
    assert result["dry_run"] and result["paragraphs_changed"] == 2
    assert doc.read_bytes() == before
    with pytest.raises(DocControlError) as err:
        ops(doc, {"op": "style.create", "name": "Z"}, expect_sha256="0" * 64)
    assert err.value.code is ErrorCode.DOC_CHANGED_ON_DISK


def test_failed_operation_in_a_batch_applies_none(doc: Path) -> None:
    before = doc.read_bytes()
    with pytest.raises(DocControlError) as err:
        ops(doc, {"op": "style.create", "name": "Fine"}, {"op": "style.create", "name": "Body"})
    assert err.value.details["op_index"] == 1
    assert doc.read_bytes() == before


def test_styles_part_is_created_when_missing(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "blank.docx", "hello")
    ops(path, {"op": "style.create", "name": "First", "run": {"bold": True}})
    pkg = Package(path)
    assert "word/styles.xml" in pkg
    assert "/word/styles.xml" in pkg.read("[Content_Types].xml").decode()
    assert "relationships/styles" in pkg.read("word/_rels/document.xml.rels").decode()
    assert defs(path)["First"][9] == {"bold": True}
    ops(path, {"op": "style.create", "name": "Second"})  # now updates the existing part
    assert set(defs(path)) == {"First", "Second"}


def test_styleset_install_modes_and_file(doc: Path, tmp_path: Path) -> None:
    out = tmp_path / "set.json"
    exported = styleset_export(
        {"doc": str(doc), "custom_only": True, "out": str(out), "name": "mine"}
    )
    assert json.loads(out.read_text(encoding="utf-8")) == exported.result["styleset"]  # type: ignore[union-attr]
    assert {s["id"] for s in exported.result["styleset"]["styles"]} == {"BodyChar", "Old", "Child"}  # type: ignore[union-attr]
    target = make_docx(tmp_path / "t.docx", "x", extra={"word/styles.xml": STYLES.encode()})
    ops(target, {"op": "style.update", "style": "Old", "run": {"bold": True}})
    skipped = styleset_install({"doc": str(target), "path": str(out), "on_conflict": "skip"})
    assert {a["action"] for a in skipped.result["applied"]} == {"skipped"}  # type: ignore[union-attr]
    assert defs(target)["Old"][9] == {"bold": True}
    with pytest.raises(DocControlError) as err:
        styleset_install({"doc": str(target), "path": str(out), "on_conflict": "error"})
    assert err.value.code is ErrorCode.STYLE_CONFLICT
    styleset_install({"doc": str(target), "path": str(out)})
    assert defs(target)["Old"] == defs(doc)["Old"]


def test_styleset_install_validates_the_set(doc: Path) -> None:
    bad_sets: tuple[dict[str, Any], ...] = (
        {"styles": []},
        {"version": 1},
        {"version": 1, "styles": [{"id": "x"}]},
        {"version": 1, "styles": [{"id": "x", "name": "x", "based_on": "Missing"}]},
    )
    for bad in bad_sets:
        with pytest.raises(DocControlError):
            styleset_install({"doc": str(doc), "styleset": bad})
    with pytest.raises(DocControlError):
        styleset_install({"doc": str(doc)})


def test_export_out_never_overwrites(doc: Path, tmp_path: Path) -> None:
    before = doc.read_bytes()
    with pytest.raises(DocControlError) as err:
        styleset_export({"doc": str(doc), "out": str(doc)})
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert doc.read_bytes() == before
    existing = tmp_path / "keep.json"
    existing.write_text("mine", encoding="utf-8")
    with pytest.raises(DocControlError):
        styleset_export({"doc": str(doc), "out": str(existing)})
    assert existing.read_text(encoding="utf-8") == "mine"
    with pytest.raises(DocControlError):
        styleset_export({"doc": str(doc), "out": str(tmp_path / "nodir" / "x.json")})


def test_each_style_operation_runs_as_a_tool(doc: Path) -> None:
    """The four style kinds are operations of their own too, answering as a style batch."""
    created = api.run("style.create", {"doc": str(doc), "name": "Fresh", "based_on": "Body"})
    assert created.result["applied"] == [
        {"op": "style.create", "style": "Fresh", "name": "Fresh", "type": "paragraph"}
    ]
    updated = api.run("style.update", {"doc": str(doc), "style": "Fresh", "run": {"bold": True}})
    assert updated.result["applied"][0]["op"] == "style.update"
    renamed = api.run("style.rename", {"doc": str(doc), "style": "Fresh", "name": "Renamed"})
    assert renamed.result["applied"][0]["op"] == "style.rename"
    merged = api.run("style.merge", {"doc": str(doc), "from": "Old", "into": "Body"})
    assert merged.result["applied"][0]["merged"] == ["Old"]
    assert merged.result["tracked"] is False and merged.result["backup"]
