"""``.dotx`` export and import of style sets.

The gate ``dotx_validator_errors == 0``: every exported template passes the validator
(structural lint plus a LibreOffice round trip).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from doc_control.errors import DocControlError
from doc_control.ops.dotx import TEMPLATE_CONTENT_TYPE, styleset_export_dotx, styleset_import_dotx
from doc_control.ops.styleset import styleset_export, styleset_install
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import KINDS, generate
from tests.corpus.private import private_documents
from tests.corpus.structural import validate_structure
from tests.corpus.validator import require_soffice, validate_documents
from tests.helpers import make_docx
from tests.ops.test_stylesets import STYLES, defs


def _corpus() -> dict[str, bytes]:
    docs = {f"{kind}-0": generate(kind, 0) for kind in KINDS}
    docs.update({f"private-{doc_id}": data for doc_id, data in private_documents()})
    return docs


def _export(data: bytes, tmp: Path, stem: str) -> tuple[Path, Path]:
    src = tmp / f"{stem}.docx"
    src.write_bytes(data)
    out = tmp / f"{stem}.dotx"
    styleset_export_dotx({"doc": str(src), "out": str(out)})
    return src, out


def test_exported_dotx_passes_validator(tmp_path: Path) -> None:
    require_soffice()
    docs = _corpus()
    exported: dict[str, bytes] = {}
    for doc_id, data in docs.items():
        work = tmp_path / doc_id
        work.mkdir()
        _, out = _export(data, work, "t")
        exported[doc_id] = out.read_bytes()
    results = validate_documents(exported, tmp_path)
    errors = sum(len(problems) for problems in results.values())
    for doc_id, problems in results.items():
        if problems and not doc_id.startswith("private-"):
            print(f"{doc_id}: {problems}")
    print(f"dotx_validator_errors == {errors} ({len(exported)} exported templates)")
    assert set(results) == set(docs)
    assert errors == 0


def test_dotx_package_is_a_template(tmp_path: Path) -> None:
    _, out = _export(generate("headings", 1), tmp_path, "t")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        types = zf.read("[Content_Types].xml").decode()
    assert {"word/styles.xml", "word/document.xml", "_rels/.rels"} <= set(names)
    assert {"word/_rels/document.xml.rels", "docProps/core.xml", "docProps/app.xml"} <= set(names)
    assert TEMPLATE_CONTENT_TYPE in types
    assert "wordprocessingml.document.main+xml" not in types
    assert validate_structure(out.read_bytes()) == []


def test_export_is_deterministic(tmp_path: Path) -> None:
    src = tmp_path / "s.docx"
    src.write_bytes(generate("kitchen_sink", 2))
    a, b = tmp_path / "a.dotx", tmp_path / "b.dotx"
    styleset_export_dotx({"doc": str(src), "out": str(a)})
    styleset_export_dotx({"doc": str(src), "out": str(b)})
    assert a.read_bytes() == b.read_bytes()


def test_export_dotx_refuses_existing_file_and_source(tmp_path: Path) -> None:
    src = tmp_path / "s.docx"
    src.write_bytes(generate("plain", 0))
    before = src.read_bytes()
    existing = tmp_path / "taken.dotx"
    existing.write_bytes(b"keep")
    for target in (existing, src):
        with pytest.raises(DocControlError):
            styleset_export_dotx({"doc": str(src), "out": str(target)})
    assert existing.read_bytes() == b"keep"
    assert src.read_bytes() == before
    link = tmp_path / "link.dotx"
    link.symlink_to(src)
    with pytest.raises(DocControlError):
        styleset_export_dotx({"doc": str(src), "out": str(link)})
    assert src.read_bytes() == before


def test_export_dotx_selection(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "d.docx", "x", extra={"word/styles.xml": STYLES.encode()})
    out = tmp_path / "custom.dotx"
    reply = styleset_export_dotx({"doc": str(src), "out": str(out), "custom_only": True})
    assert reply.result["count"] == 3  # type: ignore[union-attr]
    assert set(defs(out)) == {"BodyChar", "Old", "Child"}
    table = defs(out)
    assert table["BodyChar"][4] is None and table["Old"][2] is None  # references to Body dropped
    assert table["Child"][2] == "Old"
    with pytest.raises(DocControlError):
        styleset_export_dotx({"doc": str(src), "out": str(tmp_path / "x.dotx"), "styles": ["Nope"]})


def test_export_then_import_reproduces_style_definitions(tmp_path: Path) -> None:
    total = styles = 0
    docs = _corpus()
    for doc_id, data in docs.items():
        work = tmp_path / doc_id
        work.mkdir()
        src, out = _export(data, work, "t")
        blank = make_docx(work / "blank.docx", "")
        styleset_import_dotx({"doc": str(blank), "path": str(out)})
        before, after = defs(src), defs(blank)
        diffs = sum(1 for sid in before if after.get(sid) != before[sid]) + sum(
            1 for sid in after if sid not in before
        )
        styles += len(before)
        total += diffs
    print(
        f"dotx_import_style_definition_diffs == {total} "
        f"({len(docs)} documents, {styles} style definitions)"
    )
    assert total == 0


def test_import_dotx_on_conflict_and_dry_run(tmp_path: Path) -> None:
    src = make_docx(tmp_path / "d.docx", "x", extra={"word/styles.xml": STYLES.encode()})
    out = tmp_path / "t.dotx"
    styleset_export_dotx({"doc": str(src), "out": str(out)})
    target = make_docx(
        tmp_path / "target.docx",
        "y",
        extra={
            "word/styles.xml": STYLES.replace('<w:sz w:val="22"/>', '<w:sz w:val="30"/>').encode()
        },
    )
    before = target.read_bytes()
    with pytest.raises(DocControlError):
        styleset_import_dotx({"doc": str(target), "path": str(out), "on_conflict": "error"})
    dry = styleset_import_dotx({"doc": str(target), "path": str(out), "dry_run": True})
    assert dry.result["dry_run"] is True  # type: ignore[union-attr]
    assert target.read_bytes() == before
    skipped = styleset_import_dotx({"doc": str(target), "path": str(out), "on_conflict": "skip"})
    assert {a["action"] for a in skipped.result["applied"]} == {"skipped"}  # type: ignore[union-attr]
    assert defs(target)["Body"] != defs(src)["Body"]
    replaced = styleset_import_dotx({"doc": str(target), "path": str(out)})
    assert "replaced" in {a["action"] for a in replaced.result["applied"]}  # type: ignore[union-attr]
    assert defs(target)["Body"] == defs(src)["Body"]
    with pytest.raises(DocControlError):
        styleset_import_dotx({"doc": str(target), "path": str(target)})
    with pytest.raises(DocControlError):
        styleset_import_dotx({"doc": str(target), "path": str(tmp_path / "missing.dotx")})


def test_import_dotx_without_styles_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    empty = make_docx(tmp_path / "empty.dotx", "x")
    target = make_docx(tmp_path / "target.docx", "y", extra={"word/styles.xml": STYLES.encode()})
    before = target.read_bytes()
    with pytest.raises(DocControlError):
        styleset_import_dotx({"doc": str(target), "path": str(empty)})
    assert target.read_bytes() == before
    assert not (tmp_path / ".doc-control").exists()


def test_import_dotx_matches_json_install(tmp_path: Path) -> None:
    data = generate("headings", 3)
    src, out = _export(data, tmp_path, "t")
    styleset = styleset_export({"doc": str(src)}).result["styleset"]  # type: ignore[union-attr]
    a = make_docx(tmp_path / "a.docx", "")
    b = make_docx(tmp_path / "b.docx", "")
    styleset_install({"doc": str(a), "styleset": styleset})
    styleset_import_dotx({"doc": str(b), "path": str(out)})
    assert defs(a) == defs(b)


def test_imported_documents_pass_lint_and_libreoffice(tmp_path: Path) -> None:
    require_soffice()
    out: dict[str, bytes] = {}
    for kind in ("headings", "tables", "kitchen_sink"):
        work = tmp_path / kind
        work.mkdir()
        _, template = _export(generate(kind, 1), work, "t")
        target = work / "target.docx"
        target.write_bytes(generate("plain", 3))
        styleset_import_dotx({"doc": str(target), "path": str(template)})
        out[kind] = target.read_bytes()
        assert validate_structure(out[kind]) == []
    assert validate_documents(out, tmp_path) == {kind: [] for kind in out}


def test_registered() -> None:
    registry = load_operations()
    assert registry["styleset.export-dotx"].kind == "write"
    assert registry["styleset.import-dotx"].kind == "write"
    assert "styleset.export-dotx" in REGISTRY
