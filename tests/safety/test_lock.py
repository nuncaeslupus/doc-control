"""Lock and change detection: a guarded write never touches a locked or changed file."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.zipio import sha256_file
from doc_control.safety import guard_write
from doc_control.safety.lock import check_changed, check_unlocked, lock_file_candidates
from tests.helpers import make_docx


@pytest.fixture(autouse=True)
def _adjacent_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_CONTROL_STORE", raising=False)


def test_write_with_lock_file_present_returns_doc_locked(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    before = doc.read_bytes()
    (tmp_path / "~$Novela.docx").write_bytes(b"owner lock")
    with pytest.raises(DocControlError) as exc:
        guard_write(doc, sha256_file(doc))
    assert exc.value.code is ErrorCode.DOC_LOCKED
    assert doc.read_bytes() == before
    assert not (tmp_path / ".doc-control").exists()


@pytest.mark.parametrize(
    ("name", "lock"),
    [
        ("Novela.docx", "~$Novela.docx"),  # stem of 6 chars: prefix only
        ("Capitul.docx", "~$apitul.docx"),  # stem of 7 chars: first char replaced
        ("Manuscrito.docx", "~$nuscrito.docx"),  # stem of 8+ chars: first two replaced
        ("Manuscrito.docx", "~$Manuscrito.docx"),  # plain prefix form is also honoured
    ],
)
def test_both_lock_name_forms_are_detected(tmp_path: Path, name: str, lock: str) -> None:
    doc = make_docx(tmp_path / name, "x")
    check_unlocked(doc)
    (tmp_path / lock).write_bytes(b"")
    assert lock in [p.name for p in lock_file_candidates(doc)]
    with pytest.raises(DocControlError) as exc:
        check_unlocked(doc)
    assert exc.value.code is ErrorCode.DOC_LOCKED
    assert exc.value.details["lock_file"] == str(tmp_path / lock)


def test_short_name_does_not_use_truncated_form(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Ab.docx", "x")
    assert [p.name for p in lock_file_candidates(doc)] == ["~$Ab.docx"]


def test_sha_mismatch_returns_doc_changed_on_disk(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    before = doc.read_bytes()
    with pytest.raises(DocControlError) as exc:
        guard_write(doc, "0" * 64)
    assert exc.value.code is ErrorCode.DOC_CHANGED_ON_DISK
    assert exc.value.details["actual_sha256"] == sha256_file(doc)
    assert doc.read_bytes() == before
    assert not (tmp_path / ".doc-control").exists()


def test_check_changed_accepts_matching_and_missing_expectation(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    check_changed(doc, sha256_file(doc))
    check_changed(doc, sha256_file(doc).upper())
    check_changed(doc, None)


def test_guard_write_on_missing_document_returns_doc_not_found(tmp_path: Path) -> None:
    with pytest.raises(DocControlError) as exc:
        guard_write(tmp_path / "nope.docx", None)
    assert exc.value.code is ErrorCode.DOC_NOT_FOUND


def test_guard_write_backs_up_and_returns_path(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    backup = guard_write(doc, sha256_file(doc))
    assert backup.parent == tmp_path / ".doc-control" / "Novela.docx" / "backups"
    assert backup.read_bytes() == doc.read_bytes()
