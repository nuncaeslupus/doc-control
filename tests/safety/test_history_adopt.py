"""``history.adopt`` (D-22): a moved or renamed document takes its orphan store back.

The gate ``history_adopt_mismatches == 0``: after an adopt the document undoes its last
batch byte for byte, and no store is ever overwritten or taken by a document that differs
(unless forced).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError
from doc_control.ops import history as history_ops
from doc_control.safety import history as history_mod
from doc_control.safety.history import read_log
from doc_control.safety.store import store_dir
from tests.safety.test_history import addresses, formatted_docx, run_batch


def _reply(fn: Any) -> Callable[[dict[str, Any]], Reply]:
    return cast("Callable[[dict[str, Any]], Reply]", fn)


adopt = _reply(history_ops.history_adopt)
open_ = _reply(history_ops.open_)
undo = _reply(history_ops.undo)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DOC_CONTROL_STORE", "DOC_CONTROL_HOME", "DOC_CONTROL_STORE_DIR"):
        monkeypatch.delenv(name, raising=False)


def edited_twice(path: Path) -> bytes:
    """Write ``path`` twice; returns its bytes after the first write (what undo restores)."""
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[1], "text": "uno"}])
    first = path.read_bytes()
    run_batch(path, [{"op": "text.replace", "at": addresses(path)[2], "text": "dos"}])
    return first


def test_adopt_after_move(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    doc = formatted_docx(old / "Novela.docx")
    first = edited_twice(doc)
    moved = new / "Novela.docx"
    shutil.move(doc, moved)
    found = open_({"doc": str(moved)}).result["adoptable"]
    assert len(found) == 1 and found[0]["store"] == str(old / ".doc-control" / "Novela.docx")
    out = adopt({"doc": str(moved)}).result
    assert out["adopted"] is True and out["store"] == str(store_dir(moved))
    assert not (old / ".doc-control" / "Novela.docx").exists()
    entries = read_log(store_dir(moved) / "log.jsonl").entries
    assert entries[-1]["kind"] == "adopt" and "old" in entries[-1]["adopted_from"]["doc"]
    undo({"doc": str(moved)})
    assert moved.read_bytes() == first


def test_adopt_after_rename_in_place(tmp_path: Path) -> None:
    doc = formatted_docx(tmp_path / "Old.docx")
    first = edited_twice(doc)
    renamed = tmp_path / "New.docx"
    doc.rename(renamed)
    assert len(open_({"doc": str(renamed)}).result["adoptable"]) == 1
    out = adopt({"doc": str(renamed)}).result
    assert out["adopted"] is True
    assert not (tmp_path / ".doc-control" / "Old.docx").exists()
    undo({"doc": str(renamed)})
    assert renamed.read_bytes() == first


def test_adopt_needs_force_when_sha_differs(tmp_path: Path) -> None:
    doc = formatted_docx(tmp_path / "Old.docx")
    edited_twice(doc)
    renamed = tmp_path / "New.docx"
    doc.rename(renamed)
    # an edit outside doc-control: the bytes no longer match the store's last sha
    shutil.copyfile(formatted_docx(tmp_path / "scratch.docx", count=4), renamed)
    assert not open_({"doc": str(renamed)}).result.get("adoptable")
    old_store = tmp_path / ".doc-control" / "Old.docx"
    with pytest.raises(DocControlError):
        adopt({"doc": str(renamed)})
    with pytest.raises(DocControlError) as err:
        adopt({"doc": str(renamed), "store": str(old_store)})
    assert "force" in str(err.value)
    assert old_store.is_dir()
    out = adopt({"doc": str(renamed), "store": str(old_store), "force": True}).result
    assert out["adopted"] is True and out["sha_matched"] is False
    assert not old_store.exists()


def test_adopt_dry_run_moves_nothing(tmp_path: Path) -> None:
    doc = formatted_docx(tmp_path / "Old.docx")
    edited_twice(doc)
    renamed = tmp_path / "New.docx"
    doc.rename(renamed)
    old_store = tmp_path / ".doc-control" / "Old.docx"
    before = sorted(str(p.relative_to(old_store)) for p in old_store.rglob("*"))
    out = adopt({"doc": str(renamed), "dry_run": True}).result
    assert out["dry_run"] is True and out["adopted"] is False and out["would_move"]
    assert sorted(str(p.relative_to(old_store)) for p in old_store.rglob("*")) == before
    assert not (tmp_path / ".doc-control" / "New.docx").exists()


def test_adopt_refuses_when_target_store_exists(tmp_path: Path) -> None:
    doc = formatted_docx(tmp_path / "Old.docx")
    edited_twice(doc)
    renamed = tmp_path / "New.docx"
    shutil.copyfile(doc, renamed)  # the old document stays; New has history of its own
    run_batch(renamed, [{"op": "text.replace", "at": addresses(renamed)[3], "text": "x"}])
    old_store = tmp_path / ".doc-control" / "Old.docx"
    own = store_dir(renamed)
    own_log = (own / "log.jsonl").read_bytes()
    with pytest.raises(DocControlError) as err:
        adopt({"doc": str(renamed), "store": str(old_store), "force": True})
    assert "already" in str(err.value)
    assert (own / "log.jsonl").read_bytes() == own_log and old_store.is_dir()


def test_adopt_appdata_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_CONTROL_STORE", "appdata")
    monkeypatch.setenv("DOC_CONTROL_HOME", str(tmp_path / "home"))
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = formatted_docx(docs / "Old.docx")
    first = edited_twice(doc)
    old_store = store_dir(doc)
    moved = docs / "sub" / "New.docx"
    moved.parent.mkdir()
    shutil.move(doc, moved)
    found = open_({"doc": str(moved)}).result["adoptable"]
    assert [f["store"] for f in found] == [str(old_store)]
    out = adopt({"doc": str(moved)}).result
    assert out["store"] == str(store_dir(moved)) and not old_store.exists()
    undo({"doc": str(moved)})
    assert moved.read_bytes() == first


def test_adopt_with_several_matches_lists_them(tmp_path: Path) -> None:
    a = formatted_docx(tmp_path / "A.docx")
    edited_twice(a)
    shutil.copytree(tmp_path / ".doc-control" / "A.docx", tmp_path / ".doc-control" / "B.docx")
    renamed = tmp_path / "C.docx"
    a.rename(renamed)
    out = adopt({"doc": str(renamed)}).result
    # neighbouring test folders hold identical documents, so count only this one's
    ours = [c for c in out["candidates"] if Path(c["store"]).is_relative_to(tmp_path)]
    assert out["adopted"] is False and len(ours) == 2


def _ours(found: list[dict[str, Any]], root: Path) -> list[dict[str, Any]]:
    return [f for f in found if Path(f["store"]).is_relative_to(root)]


def test_renamed_folder_copy_does_not_take_the_live_store(tmp_path: Path) -> None:
    # the store moved with its folder; the logged doc_path (Libro/...) is stale, but the
    # document beside the store is alive, so a copy of it must not steal the history
    (tmp_path / "Libro").mkdir()
    doc = formatted_docx(tmp_path / "Libro" / "Novela.docx")
    edited_twice(doc)
    (tmp_path / "Libro").rename(tmp_path / "Libro2")
    live = tmp_path / "Libro2" / "Novela.docx"
    copy = tmp_path / "Libro2" / "Novela - copia.docx"
    shutil.copyfile(live, copy)
    store = tmp_path / "Libro2" / ".doc-control" / "Novela.docx"
    log = (store / "log.jsonl").read_bytes()
    assert not _ours(open_({"doc": str(copy)}).result.get("adoptable", []), tmp_path)
    with pytest.raises(DocControlError) as err:
        adopt({"doc": str(copy), "store": str(store)})
    assert "orphan" in str(err.value)
    assert (store / "log.jsonl").read_bytes() == log


def test_explicit_store_of_a_live_document_needs_force(tmp_path: Path) -> None:
    a = formatted_docx(tmp_path / "A.docx")
    edited_twice(a)
    b = tmp_path / "B.docx"
    shutil.copyfile(a, b)
    store = tmp_path / ".doc-control" / "A.docx"
    with pytest.raises(DocControlError) as err:
        adopt({"doc": str(b), "store": str(store)})
    assert "orphan" in str(err.value) and store.is_dir()
    out = adopt({"doc": str(b), "store": str(store), "force": True}).result
    assert out["adopted"] is True and not store.exists()


def test_failed_record_puts_both_stores_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = formatted_docx(tmp_path / "Old.docx")
    edited_twice(doc)
    renamed = tmp_path / "New.docx"
    doc.rename(renamed)
    open_({"doc": str(renamed)})  # a fresh store: an `open` note and a backup
    old_store = tmp_path / ".doc-control" / "Old.docx"
    fresh = store_dir(renamed)
    old_files = sorted(str(p.relative_to(old_store)) for p in old_store.rglob("*"))
    fresh_log = (fresh / "log.jsonl").read_bytes()

    def boom(*_a: Any, **_k: Any) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(history_mod, "record", boom)
    with pytest.raises(OSError):
        adopt({"doc": str(renamed), "store": str(old_store)})
    assert sorted(str(p.relative_to(old_store)) for p in old_store.rglob("*")) == old_files
    assert (fresh / "log.jsonl").read_bytes() == fresh_log
    assert not [p for p in (tmp_path / ".doc-control").iterdir() if p.name.startswith(".")]


def test_failed_copy_puts_both_stores_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = formatted_docx(tmp_path / "Old.docx")
    edited_twice(doc)
    renamed = tmp_path / "New.docx"
    doc.rename(renamed)
    open_({"doc": str(renamed)})
    old_store = (tmp_path / ".doc-control" / "Old.docx").resolve()
    fresh = store_dir(renamed)
    fresh_log = (fresh / "log.jsonl").read_bytes()
    real_rename = Path.rename

    def no_rename(self: Path, target: Any) -> Path:
        if self.resolve() == old_store:
            raise OSError("in use")
        return real_rename(self, target)

    def half_copy(src: Any, dst: Any, *_a: Any, **_k: Any) -> None:
        Path(dst).mkdir()
        (Path(dst) / "log.jsonl").write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(Path, "rename", no_rename)
    monkeypatch.setattr(history_mod.shutil, "copytree", half_copy)
    with pytest.raises(OSError):
        adopt({"doc": str(renamed), "store": str(old_store)})
    monkeypatch.undo()
    assert (old_store / "log.jsonl").is_file()
    assert (fresh / "log.jsonl").read_bytes() == fresh_log
    assert not [p for p in (tmp_path / ".doc-control").iterdir() if p.name.startswith(".")]


def test_undeletable_old_store_is_set_aside_after_adopt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = formatted_docx(tmp_path / "Old.docx")
    first = edited_twice(doc)
    renamed = tmp_path / "New.docx"
    doc.rename(renamed)
    old_store = (tmp_path / ".doc-control" / "Old.docx").resolve()
    real_rename = Path.rename
    real_rmtree = shutil.rmtree

    def no_rename(self: Path, target: Any) -> Path:
        if self.resolve() == old_store and Path(target).name == "New.docx":
            raise OSError("in use")
        return real_rename(self, target)

    def no_rmtree(path: Any, *a: Any, **k: Any) -> None:
        if Path(path).resolve() == old_store:
            raise OSError("in use")
        real_rmtree(path, *a, **k)

    monkeypatch.setattr(Path, "rename", no_rename)
    monkeypatch.setattr(history_mod.shutil, "rmtree", no_rmtree)
    out = adopt({"doc": str(renamed), "store": str(old_store)}).result
    monkeypatch.undo()
    assert out["adopted"] is True and not old_store.exists()
    assert not _ours(open_({"doc": str(renamed)}).result.get("adoptable", []), tmp_path)
    assert not _ours(history_mod.adoptable_stores(renamed, anything=True), tmp_path)
    undo({"doc": str(renamed)})
    assert renamed.read_bytes() == first


def test_scan_error_does_not_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = formatted_docx(tmp_path / "Old.docx")
    edited_twice(doc)
    renamed = tmp_path / "New.docx"
    doc.rename(renamed)

    def denied(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise PermissionError("access denied")

    monkeypatch.setattr(history_mod, "adoptable_stores", denied)
    out = open_({"doc": str(renamed)}).result
    assert out["backup"] and "adoptable" not in out
