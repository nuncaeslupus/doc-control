"""Plain writes (D-21): ``tracked: false`` on ``batch``, ``term.rename`` and ``restore``.

The gate ``plain_write_revision_marks == 0`` and ``plain_undo_roundtrip_mismatches == 0``: a
plain write is the tracked edit with this call's own revisions accepted in the same verified
write, so it leaves no revision mark of its own, keeps every other revision byte for byte,
refuses a paragraph that holds another unresolved revision, and ``undo`` takes it back.
"""

from __future__ import annotations

import random
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import build_index
from doc_control.ops.batch import batch
from doc_control.ops.history import history_list, restore, undo
from doc_control.ops.rename import term_rename
from doc_control.ops.revision import _REVISION_TAG, ResolveRevisions, apply_resolutions
from doc_control.ops.tracked import OPERATION_SCHEMAS
from tests.conftest import CorpusWriter
from tests.helpers import CONTENT_TYPES, RELS, make_docx

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
KINDS = ("plain", "headings", "tables", "comments")
PARA = re.compile(rb"<w:p[ >].*?</w:p>", re.S)
PPR = re.compile(rb"<w:pPr>.*?</w:pPr>", re.S)
SHA = re.compile(r"[0-9a-f]{64}")


def result_of(reply: Any) -> dict[str, Any]:
    result = reply.result if isinstance(reply, Reply) else reply
    assert isinstance(result, dict)
    return dict(result)


def document_xml(path: Path) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml")


def texts(path: Path) -> list[str]:
    return [p.text for p in build_index(document_xml(path)).paragraphs]


def paragraph_props(path: Path) -> list[bytes]:
    return [m.group(0) if (m := PPR.search(p)) else b"" for p in PARA.findall(document_xml(path))]


def marks(path: Path) -> int:
    return len(_REVISION_TAG.findall(document_xml(path)))


def run(path: Path, ops: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return result_of(batch({"doc": str(path), "ops": ops, **extra}))


def operations(path: Path) -> dict[str, list[dict[str, Any]]]:
    """One batch per operation kind of ``OPERATION_SCHEMAS``, addressed in ``path``."""
    paras = [p for p in build_index(document_xml(path)).paragraphs if len(p.text) > 12]
    if len(paras) < 5:
        return {}
    a, b, c, d = (str(p.address) for p in paras[1:5])
    return {
        "text.replace": [
            {"op": "text.replace", "at": f"{a}:2-6", "text": "nuevo"},
            {"op": "text.replace", "at": f"{b}:0-0", "text": "al inicio "},
        ],
        "para.insert": [{"op": "para.insert", "after": a, "text": "Un párrafo nuevo."}],
        "para.delete": [{"op": "para.delete", "at": b}],
        "para.move": [{"op": "para.move", "at": a, "after": d}],
        "style.apply": [{"op": "style.apply", "at": c, "style": "Quote"}],
        "format.apply": [
            {"op": "format.apply", "at": a, "align": "center"},
            {"op": "format.apply", "at": f"{c}:1-5", "bold": True},
        ],
    }


def tracked_then_accepted(path: Path, ops: list[dict[str, Any]]) -> None:
    run(path, ops)
    apply_resolutions(path, [ResolveRevisions("accept", all=True)], env={})


def test_plain_batch_equals_tracked_then_accepted(tmp_path: Path, corpus_doc: CorpusWriter) -> None:
    assert set(operations(corpus_doc("plain", 0))) == set(OPERATION_SCHEMAS)
    bad: list[str] = []
    compared = 0
    for kind in KINDS:
        for seed in range(3):
            source = corpus_doc(kind, seed)
            for name, ops in operations(source).items():
                label = f"{kind}-{seed} {name}"
                tracked, plain = tmp_path / "t.docx", tmp_path / "p.docx"
                for target in (tracked, plain):
                    target.write_bytes(source.read_bytes())
                try:
                    tracked_then_accepted(tracked, ops)
                except DocControlError as err:  # the same edit is refused plain, too
                    with pytest.raises(DocControlError) as caught:
                        run(plain, ops, tracked=False)
                    assert caught.value.code is err.code, label
                    continue
                result = run(plain, ops, tracked=False)
                compared += 1
                assert result["tracked"] is False and result["revision_ids"] == [], label
                if texts(plain) != texts(tracked):
                    bad.append(f"{label} text")
                if paragraph_props(plain) != paragraph_props(tracked):
                    bad.append(f"{label} properties")
                if marks(plain) != marks(source):
                    bad.append(f"{label} marks")
    print(f"plain_write_revision_marks={len(bad)} compared={compared}")
    assert bad == []
    assert compared >= 48


def test_plain_random_batches_leave_no_marks(tmp_path: Path, corpus_doc: CorpusWriter) -> None:
    from tests.ops.test_tracked_text import random_specs, to_edit

    applied = 0
    for seed in range(12):
        source = corpus_doc("plain", seed)
        tracked, plain = tmp_path / "t.docx", tmp_path / "p.docx"
        for target in (tracked, plain):
            target.write_bytes(source.read_bytes())
        index = build_index(document_xml(source))
        specs = random_specs(random.Random(f"plain:{seed}"), [p.text for p in index.paragraphs])
        ops = [_as_op(to_edit(s, index.paragraphs)) for s in specs]
        if not ops:
            continue
        try:
            tracked_then_accepted(tracked, ops)
        except DocControlError:
            continue
        run(plain, ops, tracked=False)
        applied += 1
        assert texts(plain) == texts(tracked), seed
        assert marks(plain) == marks(source), seed
    assert applied >= 6


def _as_op(edit: Any) -> dict[str, Any]:
    from doc_control.ops.tracked import edits_as_request

    return edits_as_request([edit])[0]


def package(path: Path, *paragraphs: str) -> Path:
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}>'
        f"<w:body>{''.join(paragraphs)}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", xml)
    return path


def p(*runs: str) -> str:
    return f"<w:p>{''.join(runs)}</w:p>"


def r(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


ZOE = 'w:author="Zoe" w:date="2026-01-01T00:00:00Z"'
OTHER = p(r("Texto de Zoe, "), f'<w:ins w:id="90" {ZOE}>{r("con una inserción")}</w:ins>')
OTHER_DEL = p(
    f'<w:del w:id="91" {ZOE}><w:r><w:delText>quitado</w:delText></w:r></w:del>', r("resto")
)


def with_revisions(tmp_path: Path) -> Path:
    return package(
        tmp_path / "rev.docx",
        p(r("Primero: la casa de Lucía.")),
        OTHER,
        p(r("Tercero: Lucía vuelve.")),
        OTHER_DEL,
    )


def address(path: Path, n: int) -> str:
    return str(build_index(document_xml(path)).paragraphs[n].address)


def test_plain_write_keeps_other_revisions(tmp_path: Path) -> None:
    path = with_revisions(tmp_path)
    before = PARA.findall(document_xml(path))
    run(
        path,
        [{"op": "text.replace", "at": f"{address(path, 0)}:0-7", "text": "Uno"}],
        tracked=False,
    )
    run(path, [{"op": "para.insert", "after": address(path, 2), "text": "Cuarto."}], tracked=False)
    after = PARA.findall(document_xml(path))
    assert after[1] == before[1] and after[4] == before[3]  # Zoe's, byte for byte
    assert texts(path)[0].startswith("Uno: la casa")
    assert b"Cuarto." in after[3] and _REVISION_TAG.search(after[3]) is None
    assert marks(path) == 2


def test_plain_write_overlapping_a_revision_is_refused(tmp_path: Path) -> None:
    path = with_revisions(tmp_path)
    before = path.read_bytes()
    for ops in (
        [{"op": "text.replace", "at": f"{address(path, 1)}:0-5", "text": "X"}],
        [{"op": "para.delete", "at": address(path, 1)}],
        [{"op": "text.replace", "at": f"{address(path, 3)}:0-2", "text": "X"}],
        [
            {"op": "text.replace", "at": f"{address(path, 0)}:0-3", "text": "X"},
            {"op": "text.replace", "at": f"{address(path, 1)}:0-3", "text": "Y"},
        ],
    ):
        with pytest.raises(DocControlError) as caught:
            run(path, ops, tracked=False)
        assert caught.value.code is ErrorCode.UNSUPPORTED_CONTENT, ops
        assert path.read_bytes() == before
    with pytest.raises(DocControlError) as caught:  # a dry run refuses too
        run(
            path,
            [{"op": "text.replace", "at": f"{address(path, 1)}:0-5", "text": "X"}],
            tracked=False,
            dry_run=True,
        )
    assert caught.value.code is ErrorCode.UNSUPPORTED_CONTENT
    run(path, [{"op": "text.replace", "at": f"{address(path, 1)}:0-5", "text": "X"}])  # tracked ok


def test_plain_undo_roundtrip(tmp_path: Path) -> None:
    bad = 0
    # a plain batch: replace, insert and a format change
    path = package(
        tmp_path / "b.docx",
        p(r("Uno: la casa de Lucía.")),
        p(r("Dos: Lucía vuelve a casa.")),
        p(r("Tres: nada que decir.")),
    )
    start = path.read_bytes()
    a = [address(path, n) for n in range(3)]
    run(
        path,
        [
            {"op": "text.replace", "at": f"{a[0]}:0-3", "text": "Primero"},
            {"op": "para.insert", "after": a[1], "text": "Entre dos."},
            {"op": "format.apply", "at": a[2], "align": "center"},
        ],
        tracked=False,
    )
    assert marks(path) == 0 and texts(path)[2] == "Entre dos."
    bad += document_xml(path) == document_xml_of(start)
    undo({"doc": str(path)})
    bad += PARA.findall(document_xml(path)) != PARA.findall(document_xml_of(start))
    # a plain term.rename
    after_batch = document_xml(path)
    done = term_rename({"doc": str(path), "from": "Lucía", "to": "Elena", "tracked": False})
    assert result_of(done)["tracked"] is False and "Elena" in texts(path)[0] and marks(path) == 0
    undo({"doc": str(path)})
    bad += document_xml(path) != after_batch
    # a plain restore of a paragraph a tracked batch deleted
    a = [address(path, n) for n in range(3)]
    deleted = run(path, [{"op": "para.delete", "at": a[1]}])["op_id"]
    held = document_xml(path)
    marks_held = marks(path)
    restore({"doc": str(path), "op_id": deleted, "tracked": False})
    assert texts(path)[1:3] == ["", "Dos: Lucía vuelve a casa."]  # deleted (tracked), restored
    assert marks(path) == marks_held  # the restore adds no revision mark
    undo({"doc": str(path)})
    bad += document_xml(path) != held
    print(f"plain_undo_roundtrip_mismatches={bad}")
    assert bad == 0


def document_xml_of(payload: bytes) -> bytes:
    import io

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return zf.read("word/document.xml")


def test_plain_paragraph_delete_and_move_undo_from_the_backup(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "Uno largo uno", "Dos largo dos", "Tres largo tres")
    start = path.read_bytes()
    a = [address(path, n) for n in range(3)]
    run(
        path,
        [{"op": "para.delete", "at": a[1]}, {"op": "para.move", "at": a[0], "after": a[2]}],
        tracked=False,
    )
    assert texts(path) == ["Tres largo tres", "Uno largo uno"] and marks(path) == 0
    undo({"doc": str(path)})
    assert path.read_bytes() != b"" and texts(path) == [
        "Uno largo uno",
        "Dos largo dos",
        "Tres largo tres",
    ]
    assert document_xml(path) == document_xml_of(start)


def test_plain_write_is_logged(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "Uno largo uno", "Dos largo dos")
    done = run(
        path, [{"op": "text.replace", "at": f"{address(path, 0)}:0-3", "text": "Un"}], tracked=False
    )
    assert SHA.fullmatch(done["sha256"]) and done["op_id"]
    from doc_control.safety.history import log_path_for, read_log

    entry = read_log(log_path_for(path)).get(done["op_id"])
    assert entry["tracked"] is False
    listed = result_of(history_list({"doc": str(path)}))
    assert listed["entries"][0]["tracked"] is False
    tracked = run(path, [{"op": "text.replace", "at": f"{address(path, 1)}:0-3", "text": "Du"}])
    assert "tracked" not in read_log(log_path_for(path)).get(tracked["op_id"])
    assert "tracked" not in tracked


def test_tracked_stays_default(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "Uno largo uno", "Dos largo dos")
    a = address(path, 0)
    run(path, [{"op": "text.replace", "at": f"{a}:0-3", "text": "Un"}])
    assert marks(path) > 0
    path2 = make_docx(tmp_path / "e.docx", "Uno largo uno", "Dos largo dos")
    run(
        path2,
        [{"op": "text.replace", "at": f"{address(path2, 0)}:0-3", "text": "Un"}],
        tracked=True,
    )
    assert marks(path2) > 0


def test_plain_live_and_row_and_comment_edits_are_refused(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "Uno largo uno", "Dos largo dos")
    a = address(path, 0)
    for extra, ops in (
        ({"engine": "live"}, [{"op": "para.delete", "at": a}]),
        ({}, [{"op": "comment.add", "at": a, "text": "x"}]),
        ({}, [{"op": "table.row.delete", "at": a}]),
    ):
        with pytest.raises(DocControlError) as caught:
            run(path, ops, tracked=False, **extra)
        assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert "live" in _live_message(path, a)


def _live_message(path: Path, a: str) -> str:
    with pytest.raises(DocControlError) as caught:
        run(path, [{"op": "para.delete", "at": a}], tracked=False, engine="live")
    return caught.value.message


def test_capabilities_flag_plain_writes_for_the_file_engine_only() -> None:
    from doc_control.ops.meta import capabilities

    assert result_of(capabilities({}))["features"]["plain_writes"] == {"file": True, "live": False}


def test_unknown_parameters_are_still_rejected(tmp_path: Path) -> None:
    from doc_control.registry import load_operations

    registry = load_operations()
    path = make_docx(tmp_path / "d.docx", "Uno largo uno", "Dos largo dos")
    ops = [{"op": "para.delete", "at": address(path, 0)}]
    for name, request in (
        ("doc.batch", {"ops": ops, "plain": True}),
        ("term.rename", {"from": "Uno", "to": "Dos", "plain": True}),
        ("history.restore", {"op_id": "op-1", "plain": True}),
    ):
        with pytest.raises(DocControlError) as caught:
            registry[name].run({"doc": str(path), **request})
        assert caught.value.code is ErrorCode.SCHEMA_INVALID, name
    # and the new key is accepted by all three schemas
    for name in ("doc.batch", "term.rename", "history.restore"):
        assert registry[name].schema["properties"]["tracked"]["type"] == "boolean"


def test_direct_write_tools_take_tracked_false(tmp_path: Path) -> None:
    from doc_control.registry import load_operations

    registry = load_operations()
    for name in ("text.replace", "para.insert", "para.delete"):
        assert registry[name].schema["properties"]["tracked"]["type"] == "boolean", name
    for name in ("comment.add", "revision.accept", "revision.reject"):
        assert "tracked" not in registry[name].schema["properties"], name
    path = make_docx(tmp_path / "d.docx", "Uno largo uno", "Dos largo dos", "Tres largo tres")
    a = [address(path, n) for n in range(3)]
    replaced = result_of(
        registry["text.replace"].run(
            {"doc": str(path), "at": f"{a[0]}:0-3", "text": "Un", "tracked": False}
        )
    )
    inserted = result_of(
        registry["para.insert"].run(
            {"doc": str(path), "after": a[1], "text": "Nuevo", "tracked": False}
        )
    )
    a = [address(path, n) for n in range(4)]
    deleted = result_of(
        registry["para.delete"].run({"doc": str(path), "at": a[1], "tracked": False})
    )
    assert all(r["tracked"] is False for r in (replaced, inserted, deleted))
    assert marks(path) == 0
    assert texts(path) == ["Un largo uno", "Nuevo", "Tres largo tres"]
    with pytest.raises(DocControlError) as caught:  # not offered where it makes no sense
        registry["comment.add"].run({"doc": str(path), "at": a[0], "text": "x", "tracked": False})
    assert caught.value.code is ErrorCode.SCHEMA_INVALID


def test_plain_undo_never_picks_a_later_identical_paragraph(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "c.docx", "Uno largo uno", "Dos largo dos", "Tres largo tres")
    a = run(path, [{"op": "text.replace", "at": address(path, 0), "text": "Hola."}], tracked=False)
    run(path, [{"op": "para.insert", "after": address(path, 2), "text": "Hola."}], tracked=False)
    run(path, [{"op": "text.replace", "at": address(path, 0), "text": "Adios."}], tracked=False)
    held = path.read_bytes()
    with pytest.raises(DocControlError) as caught:
        undo({"doc": str(path), "op_id": a["op_id"]})
    assert caught.value.code is ErrorCode.BATCH_CONFLICT
    assert "exactly as it left it" in caught.value.message
    assert path.read_bytes() == held
    # identical plain inserts (repeated scene breaks) undo in order, from the backup
    two = make_docx(tmp_path / "d.docx", "Uno largo uno", "Dos largo dos")
    ops = [
        {"op": "para.insert", "after": address(two, 0), "text": "* * *"},
        {"op": "para.insert", "after": address(two, 1), "text": "* * *"},
    ]
    start = document_xml(two)
    run(two, ops, tracked=False)
    undo({"doc": str(two)})
    assert document_xml(two) == start


def test_plain_is_refused_with_revision_operations(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "Uno largo uno", "Dos largo dos")
    for kind in ("revision.accept", "revision.reject"):
        with pytest.raises(DocControlError) as caught:
            run(path, [{"op": kind, "all": True}], tracked=False)
        assert caught.value.code is ErrorCode.SCHEMA_INVALID


def test_with_parts_refuses_an_unknown_part(tmp_path: Path) -> None:
    from doc_control.ooxml.zipio import Package

    pkg = Package(make_docx(tmp_path / "d.docx", "Uno"))
    with pytest.raises(KeyError):
        pkg.with_parts({"word/nope.xml": b"<x/>"})
