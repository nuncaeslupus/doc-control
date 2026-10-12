"""`term.rename`: case variants, whole word, inflections, runs, fields, hidden text, dry run."""

from __future__ import annotations

import hashlib
import statistics
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex
from doc_control.ooxml.revisions import reject_revisions
from doc_control.ooxml.splice import W_NS
from doc_control.ooxml.zipio import Package
from doc_control.ops.rename import match_case
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import generate
from tests.helpers import CONTENT_TYPES, RELS, make_docx

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def run(path: Path, old: str, new: str, **options: Any) -> dict[str, Any]:
    reply = REGISTRY["term.rename"].run({"doc": str(path), "from": old, "to": new, **options})
    assert isinstance(reply, Reply)
    return {**reply.result, "warnings": reply.warnings, "doc": reply.doc}


def accepted(path: Path) -> list[str]:
    """Paragraph texts with every tracked change accepted (deletions gone, insertions in)."""
    return [p.text for p in DocumentIndex.from_package(Package(path)).paragraphs]


def package(path: Path, body: str) -> Path:
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}>'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", xml)
    return path


def r(text: str, props: str = "") -> str:
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_rename_preserves_case_variants(tmp_path: Path) -> None:
    doc = make_docx(
        tmp_path / "d.docx",
        "Lucía llegó. LUCÍA gritó, y lucía sonrió.",
        "La Lucíana no cambia, ni Lucías.",
    )
    out = run(doc, "Lucía", "Elena")
    assert out["counts"]["replaced"] == 3 and out["counts"]["paragraphs"] == 1
    texts = accepted(doc)
    # Tracked: the visible text (deletions hidden) holds the new names, never Lucíana.
    assert "Elena llegó. ELENA gritó, y elena sonrió." in texts[0]
    assert "Lucíana" in texts[1] and "Lucías" in texts[1] and "Elena" not in texts[1]
    changes = [(c["before"], c["after"]) for c in out["preview"]]
    assert changes == [("Lucía", "Elena"), ("LUCÍA", "ELENA"), ("lucía", "elena")]


def test_match_case_shapes() -> None:
    assert match_case("Lucía", "Lucía", "Elena") == "Elena"
    assert match_case("LUCÍA", "Lucía", "Elena") == "ELENA"
    assert match_case("lucía", "Lucía", "Elena") == "elena"
    assert match_case("LuCíA", "Lucía", "Elena") == "Elena"
    assert match_case("Casa", "casa", "hogar") == "Hogar"
    assert match_case("A", "a", "la") == "La"
    assert match_case("123", "123", "456") == "456"


def test_options_case_sensitive_whole_word_and_accents(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", "Lucía lucía LUCÍA Lucia Lucíana")
    plan = run(doc, "Lucía", "Elena", case_sensitive=True, dry_run=True)
    assert plan["counts"]["replaced"] == 1
    plan = run(doc, "Lucía", "Elena", ignore_accents=True, dry_run=True)
    assert plan["counts"]["replaced"] == 4
    plan = run(doc, "Lucía", "Elena", whole_word=False, dry_run=True)
    assert plan["counts"]["replaced"] == 4  # includes the start of Lucíana
    plan = run(doc, "Lucía", "Elena", preserve_case=False, dry_run=True)
    assert {c["after"] for c in plan["preview"]} == {"Elena"}


def test_inflections_rename_forms_and_longest_wins(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", "Lucía, Lucías y LUCÍAS; la Lucíana no.")
    out = run(doc, "Lucía", "Elena", inflections=["s", {"from": "na", "to": "nita"}], dry_run=True)
    assert [(c["before"], c["after"]) for c in out["preview"]] == [
        ("Lucía", "Elena"),
        ("Lucías", "Elenas"),
        ("LUCÍAS", "ELENAS"),
        ("Lucíana", "Elenanita"),
    ]
    out = run(doc, "Lucía", "Elena", whole_word=False, inflections=["s"], dry_run=True)
    assert [c["after"] for c in out["preview"]] == ["Elena", "Elenas", "ELENAS", "Elena"]
    assert out["counts"]["matches"] == 4
    with pytest.raises(DocControlError) as err:
        run(doc, "Lucía", "Elena", inflections=[3])
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_dry_run_writes_nothing_and_reports_counts(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", "Lucía y Lucía.", "Nada.", "Lucía")
    before = sha(doc)
    out = run(doc, "Lucía", "Elena", dry_run=True, preview_limit=2)
    assert sha(doc) == before
    assert out["dry_run"] is True and "sha256" not in out
    assert out["counts"] == {
        "matches": 3,
        "replaced": 3,
        "unchanged": 0,
        "paragraphs": 2,
        "skipped_unsupported": 0,
        "skipped_boundary": 0,
    }
    assert len(out["preview"]) == 2 and out["preview"][0]["at"].endswith(":0-5")
    assert out["preview"][0]["context"] == "Lucía y Lucía."


def test_no_match_and_same_name_write_nothing(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", "Lucía.")
    before = sha(doc)
    assert run(doc, "Pedro", "Juan")["counts"]["matches"] == 0
    same = run(doc, "Lucía", "Lucía")
    assert same["counts"]["unchanged"] == 1 and same["counts"]["replaced"] == 0
    assert sha(doc) == before


def test_match_across_runs_is_one_tracked_replacement(tmp_path: Path) -> None:
    doc = package(
        tmp_path / "d.docx",
        f"<w:p>{r('Hola Lu')}{r('cí', '<w:b/>')}{r('a, adiós')}</w:p>",
    )
    out = run(doc, "Lucía", "Elena")
    assert out["counts"]["replaced"] == 1
    assert accepted(doc) == ["Hola Elena, adiós"]
    data = Package(doc).read("word/document.xml").decode()
    assert data.count("<w:del ") >= 1 and data.count("<w:ins ") == 1
    root = ET.fromstring(data)
    para = root.find(f".//{{{W_NS}}}p")
    assert para is not None
    reject_revisions(para, {str(i) for i in out["revision_ids"]})
    assert "".join(t.text or "" for t in para.iter(f"{{{W_NS}}}t")) == "Hola Lucía, adiós"


def test_match_in_field_result_is_skipped_not_corrupted(tmp_path: Path) -> None:
    field = (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> REF Lucía </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f"{r('Lucía')}"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )
    doc = package(tmp_path / "d.docx", f"<w:p>{r('Lucía ')}{field}{r(' Lucía')}</w:p>")
    out = run(doc, "Lucía", "Elena")
    assert out["counts"]["replaced"] == 2 and out["counts"]["skipped_unsupported"] == 1
    assert any("left unchanged" in w for w in out["warnings"])
    data = Package(doc).read("word/document.xml").decode()
    assert "<w:instrText" in data and "REF Lucía" in data
    assert accepted(doc) == [accepted(doc)[0]] and "Lucía" in accepted(doc)[0]
    assert accepted(doc)[0].count("Elena") == 2


def test_match_in_hyperlink_is_skipped(tmp_path: Path) -> None:
    body = f'<w:p>{r("Lucía ")}<w:hyperlink w:anchor="x">{r("Lucía")}</w:hyperlink></w:p>'
    doc = package(tmp_path / "d.docx", body)
    out = run(doc, "Lucía", "Elena")
    assert out["counts"]["replaced"] == 1 and out["counts"]["skipped_unsupported"] == 1
    assert accepted(doc)[0].count("Elena") == 1 and "Lucía" in accepted(doc)[0]


def test_hidden_text_is_not_searched_and_stays(tmp_path: Path) -> None:
    hidden = r("Lucía", "<w:vanish/>")
    doc = package(tmp_path / "d.docx", f"<w:p>{r('Lucía ')}{hidden}{r(' fin')}</w:p>")
    out = run(doc, "Lucía", "Elena")
    assert out["counts"]["replaced"] == 1
    data = Package(doc).read("word/document.xml").decode()
    assert data.count("Lucía") >= 1 and "<w:vanish/>" in data
    inside = package(
        tmp_path / "e.docx", f"<w:p>{r('Lu')}{r('XX', '<w:vanish/>')}{r('cía fin')}</w:p>"
    )
    inner = run(inside, "Lucía", "Elena")
    assert inner["counts"]["replaced"] == 1
    assert "XX" in Package(inside).read("word/document.xml").decode()


def test_stale_expect_sha_is_refused(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", "Lucía")
    with pytest.raises(DocControlError) as err:
        run(doc, "Lucía", "Elena", expect={"sha256": "0" * 64})
    assert err.value.code is ErrorCode.DOC_CHANGED_ON_DISK
    assert accepted(doc) == ["Lucía"]


def test_registered_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert load_operations()["term.rename"].kind == "write"
    doc = make_docx(tmp_path / "d.docx", "Lucía")
    code = main(["term.rename", "--json", json_arg(doc)])
    assert code == 0
    assert "Elena" in accepted(doc)[0]
    capsys.readouterr()


def json_arg(doc: Path) -> str:
    import json

    return json.dumps({"doc": str(doc), "from": "Lucía", "to": "Elena"})


def test_novel_rename_is_one_tracked_batch(tmp_path: Path) -> None:
    path = tmp_path / "novel.docx"
    path.write_bytes(generate("novel", 0))
    out = run(path, "Lucía", "Elena", preview_limit=0)
    assert out["counts"]["replaced"] > 100 and out["preview"] == []
    assert len(out["revision_ids"]) >= 2 * out["counts"]["replaced"]
    assert "Elena" in "".join(accepted(path))


@pytest.mark.bench
def test_rename_100k_words_p95_s(tmp_path: Path) -> None:
    """Gate: p95 of a rename (plan, tracked edits, verified write) on the 100k-word novel."""
    source = generate("novel", 0)
    times: list[float] = []
    for i in range(8):
        path = tmp_path / f"novel{i}.docx"
        path.write_bytes(source)
        options: dict[str, Any] = (
            {"inflections": ["s"], "ignore_accents": True} if i % 2 else {"dry_run": i % 4 == 2}
        )
        start = time.perf_counter()
        out = run(path, "María" if i % 2 == 0 else "Tomás", "Elena", **options)
        times.append(time.perf_counter() - start)
        assert out["counts"]["replaced"] > 100
    p95 = statistics.quantiles(times, n=20)[-1]
    print(f"rename_100k_words_p95_s={p95:.2f}")
    assert p95 <= 5
