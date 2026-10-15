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
