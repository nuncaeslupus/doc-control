"""Undo log, undo, restore, history.list/search/purge, orphans and backup on open.

The gate ``undo_roundtrip_mismatches == 0``: undoing any logged batch returns every
paragraph's text and properties (and the whole ``word/document.xml``) to its prior state.
"""

from __future__ import annotations

import json
import random
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from doc_control.cli import main
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import build_index
from doc_control.ooxml.zipio import Package
from doc_control.ops import history as history_ops
from doc_control.ops.batch import batch
from doc_control.safety.history import read_log, to_bytes, to_text
from tests.corpus.generator import generate
from tests.helpers import CONTENT_TYPES, RELS


def _reply(fn: Any) -> Callable[[dict[str, Any]], Reply]:
    return cast("Callable[[dict[str, Any]], Reply]", fn)


history_list = _reply(history_ops.history_list)
history_orphans = _reply(history_ops.history_orphans)
history_purge = _reply(history_ops.history_purge)
history_search = _reply(history_ops.history_search)
open_ = _reply(history_ops.open_)
restore = _reply(history_ops.restore)
undo = _reply(history_ops.undo)
DOCX_PART = "word/document.xml"
PPR = re.compile(rb"<w:pPr>.*?</w:pPr>", re.S)
NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
ALIGNS = ("left", "center", "right", "both")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_CONTROL_STORE", raising=False)
    monkeypatch.delenv("DOC_CONTROL_HOME", raising=False)


def formatted_docx(path: Path, count: int = 10) -> Path:
    """Paragraphs with distinct alignment and spacing, to check properties survive."""
    paras = "".join(
        f'<w:p><w:pPr><w:jc w:val="{ALIGNS[i % 4]}"/><w:spacing w:after="{i * 20}"/></w:pPr>'
        f"<w:r><w:t>Párrafo {i} con texto propio número {i * 7}.</w:t></w:r></w:p>"
        for i in range(count)
    )
    xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {NS}>'
        f"<w:body>{paras}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES, zipfile.ZIP_DEFLATED)
        zf.writestr("_rels/.rels", RELS, zipfile.ZIP_DEFLATED)
        zf.writestr(DOCX_PART, xml, zipfile.ZIP_DEFLATED)
    return path


def corpus_docx(path: Path, kind: str, seed: int) -> Path:
    path.write_bytes(generate(kind, seed))
    return path


def snapshot(path: Path) -> tuple[bytes, list[tuple[str, bytes]]]:
    """document.xml and, per paragraph, its visible text and properties."""
    data = Package(path).read(DOCX_PART)
    index = build_index(data)
    rows = []
    for para in index.paragraphs:
        m = PPR.search(index.spans.paragraph_bytes(para.span_index))
        rows.append((para.text, m[0] if m else b""))
    return data, rows


def addresses(path: Path) -> list[str]:
    return [str(p.address) for p in build_index(Package(path).read(DOCX_PART)).paragraphs]


def run_batch(path: Path, ops: list[dict[str, Any]]) -> dict[str, Any]:
    reply = _reply(batch)({"doc": str(path), "ops": ops})
    return dict(reply.result)


def random_ops(path: Path, rng: random.Random) -> list[dict[str, Any]]:
    addr = addresses(path)
    if len(addr) < 4:
        return []
    picks = rng.sample(range(len(addr) - 1), min(len(addr) - 1, rng.randint(1, 4)))
    ops: list[dict[str, Any]] = []
    for i in picks:
        kind = rng.choice(["replace", "range", "insert", "delete", "move", "format", "bold"])
        if kind == "format":
            ops.append(
                {"op": "format.apply", "at": addr[i], "align": rng.choice(["center", "right"])}
            )
        elif kind == "bold":
            ops.append({"op": "format.apply", "at": f"{addr[i]}:0-3", "bold": True})
        elif kind == "replace":
            ops.append({"op": "text.replace", "at": addr[i], "text": f"nuevo {rng.random():.4f}"})
        elif kind == "range":
            ops.append({"op": "text.replace", "at": f"{addr[i]}:0-3", "text": "ZZ"})
        elif kind == "insert":
            ops.append({"op": "para.insert", "after": addr[i], "text": f"añadido {i}"})
        elif kind == "delete":
            ops.append({"op": "para.delete", "at": addr[i]})
        else:
            target = rng.choice([j for j in range(len(addr) - 1) if j not in picks] or [i])
            ops.append({"op": "para.move", "at": addr[i], "after": addr[target]})
    return ops


def test_undo_each_logged_op_restores_prior_state(tmp_path: Path) -> None:
    """Fuzz: batches over formatted and corpus documents, each undone, singly and stacked."""
    rng = random.Random(1313)
    mismatches = checked = 0
    docs: list[Path] = []
    for n in range(40):
        docs.append(formatted_docx(tmp_path / f"f{n}.docx", rng.randint(5, 14)))
    for kind in ("plain", "headings", "revisions", "tables", "kitchen_sink"):
        for seed in range(4):
            docs.append(corpus_docx(tmp_path / f"c-{kind}-{seed}.docx", kind, seed))
    for path in docs:
        history: list[tuple[str, tuple[bytes, list[tuple[str, bytes]]]]] = []
        for _ in range(rng.randint(1, 3)):
            before = snapshot(path)
            ops = random_ops(path, rng)
            if not ops:
                continue
            try:
                result = run_batch(path, ops)
            except DocControlError:
                continue  # an edit the engine refuses (table end, conflicts): nothing logged
            history.append((result["op_id"], before))
        # undo newest first, comparing with the state before that batch
        for op_id, before in reversed(history):
            out = undo({"doc": str(path), "op_id": op_id}).result
            after = snapshot(path)
            checked += 1
            if after[1] != before[1] or after[0] != before[0] or out["undid"] != op_id:
                mismatches += 1
    print(f"undo_roundtrip_mismatches={mismatches} (of {checked} undone batches)")
    assert checked > 60
    assert mismatches == 0


def test_log_entry_has_the_spec_fields(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    a = addresses(path)
    result = run_batch(
        path,
        [
            {"op": "text.replace", "at": a[1], "text": "x"},
            {"op": "para.insert", "after": a[3], "text": "y"},
        ],
    )
    assert result["op_id"] == "h-000001"
    log = read_log(tmp_path / ".doc-control" / "d.docx" / "log.jsonl")
    (entry,) = log.entries
    assert entry["log_version"] == 1 and entry["kind"] == "batch"
    assert entry["sha256_before"] != entry["sha256_after"]
    assert entry["backup"].endswith(".docx") and entry["request"][0]["op"] == "text.replace"
    assert {c["kind"] for c in entry["changes"]} == {"replace", "insert"}
    assert entry["doc_id"] and entry["author"] == "Claude"


def test_bytes_round_trip_through_the_log_text() -> None:
    for raw in (b"<w:p>\xc3\xa1</w:p>", b"\xff\xfe\x00bad", bytes(range(256))):
        assert to_bytes(json.loads(json.dumps(to_text(raw)))) == raw


def test_undo_defaults_to_latest_in_effect_and_is_logged(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    a = addresses(path)
    original = snapshot(path)
    run_batch(path, [{"op": "text.replace", "at": a[1], "text": "uno"}])
    mid = snapshot(path)
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[5], "text": "dos"}])
    dry = undo({"doc": str(path), "dry_run": True}).result
    assert dry["undid"] == "h-000002" and dry["dry_run"] and "op_id" not in dry
    first = undo({"doc": str(path)}).result
    assert first["undid"] == "h-000002" and first["op_id"] == "h-000003"
    assert snapshot(path) == mid
    assert undo({"doc": str(path)}).result["undid"] == "h-000001"
    assert snapshot(path) == original
    for call in ({"doc": str(path)}, {"doc": str(path), "op_id": "h-000001"}):
        with pytest.raises(DocControlError) as err:
            undo(call)
        assert err.value.code is ErrorCode.HISTORY_NOT_FOUND
    with pytest.raises(DocControlError) as err:
        undo({"doc": str(path), "op_id": "h-000003"})  # an undo cannot be undone
    assert err.value.code is ErrorCode.HISTORY_NOT_FOUND
    with pytest.raises(DocControlError) as err:
        undo({"doc": str(path), "op_id": "h-000099"})
    assert err.value.code is ErrorCode.HISTORY_NOT_FOUND


def test_undo_refuses_when_a_later_edit_touched_the_paragraph(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[2], "text": "uno"}])
    run_batch(path, [{"op": "text.replace", "at": f"{addresses(path)[2]}:0-1", "text": "Q"}])
    before = path.read_bytes()
    with pytest.raises(DocControlError) as err:
        undo({"doc": str(path), "op_id": "h-000001"})
    assert err.value.code is ErrorCode.BATCH_CONFLICT and err.value.details["changes"]
    assert path.read_bytes() == before
    # the later one undoes, then the earlier one
    undo({"doc": str(path), "op_id": "h-000002"})
    undo({"doc": str(path), "op_id": "h-000001"})


def test_undo_a_middle_op_when_other_paragraphs_changed(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    base = snapshot(path)
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[1], "text": "uno"}])
    run_batch(path, [{"op": "para.delete", "at": addresses(path)[6]}])
    undo({"doc": str(path), "op_id": "h-000001"})
    texts = [t for t, _ in snapshot(path)[1]]
    assert base[1][1][0] in texts and base[1][6][0] not in texts  # 6 is still deleted
    undo({"doc": str(path), "op_id": "h-000002"})
    assert snapshot(path) == base


def test_undo_stops_on_a_changed_document_and_lock(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[1], "text": "uno"}])
    with pytest.raises(DocControlError) as err:
        undo({"doc": str(path), "expect": {"sha256": "0" * 64}})
    assert err.value.code is ErrorCode.DOC_CHANGED_ON_DISK
    (tmp_path / "~$d.docx").write_bytes(b"x")
    with pytest.raises(DocControlError) as err:
        undo({"doc": str(path)})
    assert err.value.code is ErrorCode.DOC_LOCKED


def test_restore_brings_back_a_deleted_paragraph_as_tracked_insertion(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    a = addresses(path)
    original = snapshot(path)[1]
    run_batch(
        path, [{"op": "para.delete", "at": a[3]}, {"op": "text.replace", "at": a[5], "text": "z"}]
    )
    listed = history_list({"doc": str(path)}).result
    assert [d["text"] for d in listed["entries"][0]["deleted"]] == [original[3][0]]
    dry = restore({"doc": str(path), "op_id": "h-000001", "dry_run": True}).result
    assert dry["restored"][0]["text"] == original[3][0] and "op_id" not in dry
    out = restore({"doc": str(path), "op_id": "h-000001"}).result
    assert out["op_id"] == "h-000002"
    data = Package(path).read(DOCX_PART)
    index = build_index(data)
    texts = [p.text for p in index.paragraphs]
    assert texts.count(original[3][0]) == 1  # the deleted one is hidden, the copy is visible
    new = next(p for p in index.paragraphs if p.text == original[3][0])
    assert b"<w:ins " in index.spans.paragraph_bytes(new.span_index)
    # same properties as the original paragraph, and it can be undone like any batch
    jc = re.compile(rb"<w:jc [^>]*/><w:spacing [^>]*/>")
    mine = jc.search(index.spans.paragraph_bytes(new.span_index))
    theirs = jc.search(original[3][1])
    assert mine is not None and theirs is not None and mine[0] == theirs[0]
    undo({"doc": str(path)})
    assert [p.text for p in build_index(Package(path).read(DOCX_PART)).paragraphs].count(
        original[3][0]
    ) == 0
    # only the deleted paragraph is restorable by default; an edited one needs `change`
    with pytest.raises(DocControlError) as err:
        restore({"doc": str(path), "op_id": "h-000001", "change": 9})
    assert err.value.code is ErrorCode.HISTORY_NOT_FOUND


def test_restore_after_the_place_was_edited_needs_an_address(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    run_batch(path, [{"op": "para.delete", "at": addresses(path)[3]}])
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[3], "text": "Q"}])
    with pytest.raises(DocControlError) as err:
        restore({"doc": str(path), "op_id": "h-000001"})
    assert err.value.code is ErrorCode.BATCH_CONFLICT
    out = restore({"doc": str(path), "op_id": "h-000001", "after": addresses(path)[0]}).result
    assert out["restored"][0]["after"] == addresses(path)[0] and out["applied"] == 1


def test_list_search_and_paging(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    for i in range(5):
        run_batch(path, [{"op": "text.replace", "at": addresses(path)[i], "text": f"Ñandú {i}"}])
    first = history_list({"doc": str(path), "limit": 2})
    assert [e["op_id"] for e in first.result["entries"]] == ["h-000005", "h-000004"]
    assert first.next_cursor == "h-000004"
    rest = history_list({"doc": str(path), "cursor": first.next_cursor})
    assert [e["op_id"] for e in rest.result["entries"]] == ["h-000003", "h-000002", "h-000001"]
    assert rest.next_cursor is None
    hit = history_search({"doc": str(path), "query": "ñandú 3", "ignore_case": True}).result
    assert {m["op_id"] for m in hit["matches"]} == {"h-000004"}
    hit = history_search(
        {"doc": str(path), "query": "nandu", "ignore_accents": True, "ignore_case": True}
    ).result
    assert hit["total"] >= 5
    old = history_search({"doc": str(path), "query": "texto propio número 14"}).result
    assert any(m["in"].endswith("before") for m in old["matches"])  # a quote only the old text had
    assert history_search({"doc": str(path), "query": "no existe"}).result["matches"] == []
    with pytest.raises(DocControlError):
        history_list({})


def test_reading_history_creates_no_store(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    assert history_list({"doc": str(path)}).result["entries"] == []
    assert not (tmp_path / ".doc-control").exists()


def test_log_survives_a_torn_line(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[1], "text": "uno"}])
    log = tmp_path / ".doc-control" / "d.docx" / "log.jsonl"
    with log.open("ab") as fh:
        fh.write(b'{"log_version":1,"op_id":"h-0000')  # a write cut short
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[2], "text": "dos"}])
    entries = read_log(log)
    assert [e["op_id"] for e in entries.entries] == ["h-000001", "h-000002"]
    assert entries.skipped == 1
    assert history_list({"doc": str(path)}).result["unreadable_lines"] == 1


def test_purge_needs_a_dry_run_and_leaves_a_tombstone(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    for i in range(3):
        run_batch(path, [{"op": "text.replace", "at": addresses(path)[i], "text": f"t{i}"}])
    log = tmp_path / ".doc-control" / "d.docx" / "log.jsonl"
    before = log.read_bytes()
    for bad in ({}, {"all": True, "before": "2999-01-01"}):
        with pytest.raises(DocControlError):
            history_purge({"doc": str(path), **bad})
    dry = history_purge({"doc": str(path), "all": True, "dry_run": True}).result
    assert dry["would_remove"] == 3 and dry["removed"] == 0 and log.read_bytes() == before
    with pytest.raises(DocControlError) as err:
        history_purge({"doc": str(path), "all": True})
    assert err.value.code is ErrorCode.SCHEMA_INVALID and log.read_bytes() == before
    with pytest.raises(DocControlError):
        history_purge({"doc": str(path), "all": True, "confirm": "wrong"})
    done = history_purge({"doc": str(path), "all": True, "confirm": dry["confirm"]}).result
    assert done["removed"] == 3 and done["tombstone"] == "h-000004"
    (entry,) = read_log(log).entries
    assert entry["kind"] == "purge" and entry["removed"] == 3 and "changes" not in entry
    assert b"t0" not in log.read_bytes()
    # ids are never reused, and the tombstone survives a later purge
    assert (
        run_batch(path, [{"op": "text.replace", "at": addresses(path)[4], "text": "n"}])["op_id"]
        == "h-000005"
    )
    again = history_purge({"doc": str(path), "all": True, "dry_run": True}).result
    assert again["would_remove"] == 1
    assert undo({"doc": str(path)}).result["undid"] == "h-000005"


def test_purge_before_a_date_keeps_newer_entries(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    for i in range(3):
        run_batch(path, [{"op": "text.replace", "at": addresses(path)[i], "text": f"t{i}"}])
    log = tmp_path / ".doc-control" / "d.docx" / "log.jsonl"
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    lines[0]["time"] = "2020-01-01T00:00:00Z"
    log.write_text("".join(json.dumps(x) + "\n" for x in lines))
    dry = history_purge({"doc": str(path), "before": "2021-01-01", "dry_run": True}).result
    assert dry["would_remove"] == 1 and dry["last_op_id"] == "h-000001"
    history_purge({"doc": str(path), "before": "2021-01-01", "confirm": dry["confirm"]})
    kinds = [(e["op_id"], e["kind"]) for e in read_log(log).entries]
    assert kinds == [("h-000002", "batch"), ("h-000003", "batch"), ("h-000004", "purge")]
    with pytest.raises(DocControlError):
        history_purge({"doc": str(path), "before": "not a date", "dry_run": True})


def test_open_backs_up_and_notes_new_versions_once(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    original = path.read_bytes()
    first = open_({"doc": str(path)}).result
    assert Path(first["backup"]).read_bytes() == original and first["op_id"] == "h-000001"
    assert path.read_bytes() == original
    assert open_({"doc": str(path)}).result["op_id"] is None
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[1], "text": "uno"}])
    assert open_({"doc": str(path)}).result["op_id"] is None
    path.write_bytes(path.read_bytes() + b"")  # unchanged bytes: still known
    formatted_docx(path, 6)  # saved by someone else
    assert open_({"doc": str(path)}).result["op_id"] == "h-000003"
    with pytest.raises(DocControlError) as err:
        open_({"doc": str(tmp_path / "missing.docx")})
    assert err.value.code is ErrorCode.DOC_NOT_FOUND
    (tmp_path / "x.docx").write_bytes(b"not a zip")
    with pytest.raises(DocControlError) as err:
        open_({"doc": str(tmp_path / "x.docx")})
    assert err.value.code is ErrorCode.NOT_A_DOCX
    assert not (tmp_path / ".doc-control" / "x.docx").exists()


def test_orphans_listed_never_deleted_and_restorable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOC_CONTROL_STORE", "appdata")
    monkeypatch.setenv("DOC_CONTROL_HOME", str(tmp_path / "home"))
    docs = tmp_path / "docs"
    docs.mkdir()
    gone = formatted_docx(docs / "gone.docx")
    kept = formatted_docx(docs / "kept.docx")
    deleted_text = snapshot(gone)[1][3][0]
    run_batch(gone, [{"op": "para.delete", "at": addresses(gone)[3]}])
    run_batch(kept, [{"op": "text.replace", "at": addresses(kept)[1], "text": "k"}])
    gone.unlink()
    result = history_orphans({}).result
    assert [Path(o["doc"]).name for o in result["orphans"]] == ["gone.docx"]
    orphan = result["orphans"][0]
    assert orphan["entries"] == 1 and Path(orphan["store"], "log.jsonl").is_file()
    # the history is readable and restorable into another document through `store`
    other = formatted_docx(docs / "other.docx")
    entries = history_list({"store": orphan["store"]}).result["entries"]
    assert entries[0]["op_id"] == "h-000001"
    out = restore(
        {
            "doc": str(other),
            "store": orphan["store"],
            "op_id": "h-000001",
            "after": addresses(other)[0],
        }
    ).result
    assert out["restored"][0]["text"] == deleted_text
    # purging it is explicit
    dry = history_purge({"store": orphan["store"], "all": True, "dry_run": True}).result
    history_purge({"store": orphan["store"], "all": True, "confirm": dry["confirm"]})
    assert Path(orphan["store"], "backups").is_dir()  # backups are retention's business
    # adjacent stores in a folder
    monkeypatch.delenv("DOC_CONTROL_STORE")
    adj = formatted_docx(docs / "adj.docx")
    run_batch(adj, [{"op": "text.replace", "at": addresses(adj)[1], "text": "k"}])
    assert history_orphans({"dir": str(docs)}).result["orphans"] == []
    adj.unlink()
    names = [Path(o["doc"]).name for o in history_orphans({"dir": str(docs)}).result["orphans"]]
    assert "adj.docx" in names


def test_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    before = snapshot(path)
    req = {"doc": str(path), "ops": [{"op": "para.delete", "at": addresses(path)[2]}]}
    assert main(["batch", "--json", json.dumps(req)]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["op_id"] == "h-000001"
    for op in ("history.list", "history.search"):
        body = {"doc": str(path), **({"query": "Párrafo"} if op.endswith("search") else {})}
        assert main([op, "--json", json.dumps(body)]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
    assert main(["undo", "--json", json.dumps({"doc": str(path)})]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["undid"] == "h-000001"
    assert snapshot(path) == before
    assert main(["undo", "--json", json.dumps({"doc": str(path)})]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "HISTORY_NOT_FOUND"


def test_every_history_operation_is_registered_with_a_schema() -> None:
    from doc_control.registry import load_operations

    registry = load_operations()
    for name in (
        "open",
        "undo",
        "restore",
        "history.list",
        "history.search",
        "history.purge",
        "history.orphans",
    ):
        assert name in registry and registry[name].schema["type"] == "object"
    assert registry["undo"].kind == registry["restore"].kind == "write"


def test_undo_whose_log_append_fails_still_succeeds_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    before = snapshot(path)
    run_batch(path, [{"op": "para.delete", "at": addresses(path)[2]}])

    def broken(*args: Any, **kwargs: Any) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(history_ops.history, "record", broken)
    reply = undo({"doc": str(path)})
    assert snapshot(path) == before  # the document write stands
    assert reply.result["op_id"] is None and reply.result["undid"] == "h-000001"
    assert reply.warnings and reply.warnings[0].startswith("HISTORY_NOT_RECORDED")


def test_undo_restores_a_format_apply_batch_exactly(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    original = snapshot(path)
    a = addresses(path)
    result = run_batch(
        path,
        [
            {"op": "format.apply", "at": a[1], "align": "right", "indent_first_line_cm": 0.6},
            {"op": "format.apply", "at": f"{a[2]}:2-8", "bold": True, "font": "Arial"},
            {"op": "text.replace", "at": a[4], "text": "otro"},
        ],
    )
    changed = snapshot(path)
    assert changed[1] != original[1] and result["op_id"] == "h-000001"
    entry = read_log(tmp_path / ".doc-control" / "d.docx" / "log.jsonl").entries[0]
    assert [r["op"] for r in entry["request"]] == ["format.apply", "format.apply", "text.replace"]
    assert entry["request"][0]["align"] == "right"
    undo({"doc": str(path)})
    assert snapshot(path) == original  # pPr, rPr and the whole document.xml, byte for byte


def test_comment_batch_is_logged_and_its_undo_is_refused(tmp_path: Path) -> None:
    path = formatted_docx(tmp_path / "d.docx")
    a = addresses(path)
    result = run_batch(
        path,
        [
            {"op": "comment.add", "at": a[1], "text": "Revisar esto"},
            {"op": "text.replace", "at": a[3], "text": "otro"},
        ],
    )
    assert result["op_id"] == "h-000001" and "comment_ids" in result
    entry = read_log(tmp_path / ".doc-control" / "d.docx" / "log.jsonl").entries[0]
    assert [r["op"] for r in entry["request"]] == ["comment.add", "text.replace"]
    assert entry["parts"] and len(entry["changes"]) == 2  # the commented and the edited paragraph
    assert history_search({"doc": str(path), "query": "Revisar"}).result["total"] >= 1
    written = path.read_bytes()
    with pytest.raises(DocControlError) as err:
        undo({"doc": str(path)})
    assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT
    assert "comments" in err.value.message and path.read_bytes() == written
    # a later text-only batch is still undoable on its own
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[6], "text": "x"}])
    assert undo({"doc": str(path)}).result["undid"] == "h-000002"
