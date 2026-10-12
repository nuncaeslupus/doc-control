"""Footprint verifier, auto-restore and fault injection at every write stage."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import doc_control.safety.verify as verify_mod
from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml import zipio
from doc_control.ooxml.splice import scan
from doc_control.ooxml.zipio import Package, sha256_file
from doc_control.safety.verify import (
    DOCUMENT_PART,
    STAGES,
    Footprint,
    verify_footprint,
    write_verified,
)
from tests.helpers import make_docx

PARAS = ("uno", "dos", "tres")
NEW_P = b"<w:p><w:r><w:t>cambiado</w:t></w:r></w:p>"
MEDIA = {"word/media/a.bin": b"\x00\x01binary\x02" * 40, "word/styles.xml": b"<s/>"}
Hook = Callable[[str], None]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_CONTROL_STORE", raising=False)
    monkeypatch.delenv("DOC_CONTROL_HOME", raising=False)


def _doc(tmp_path: Path) -> Path:
    return make_docx(tmp_path / "Novela.docx", *PARAS, extra=MEDIA)


def _edit(path: Path, index: int = 1) -> dict[str, bytes]:
    old = Package(path).read(DOCUMENT_PART)
    return {DOCUMENT_PART: scan(old).splice({index: NEW_P})}


def _stray(tmp_path: Path) -> list[str]:
    keep = ("Novela.docx", ".doc-control")
    return sorted(n.name for n in tmp_path.iterdir() if n.name not in keep)


def _backups(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / ".doc-control" / "Novela.docx" / "backups").glob("*.docx"))


def test_write_verified_applies_edit_and_keeps_everything_else(tmp_path: Path) -> None:
    path = _doc(tmp_path)
    before = Package(path)
    result = write_verified(path, _edit(path), Footprint(replaced=frozenset({1})))
    after = Package(path)
    assert result.before_sha256 == before.sha256
    assert result.after_sha256 == sha256_file(path) == after.sha256
    assert sha256_file(result.backup) == before.sha256
    assert b"cambiado" in after.read(DOCUMENT_PART)
    for name in before.names:
        if name != DOCUMENT_PART:
            assert after.fingerprint(name) == before.fingerprint(name)
    assert _stray(tmp_path) == []


def test_stages_run_in_order(tmp_path: Path) -> None:
    path = _doc(tmp_path)
    seen: list[str] = []
    write_verified(path, _edit(path), Footprint(replaced=frozenset({1})), stage=seen.append)
    assert tuple(seen) == STAGES


def test_guard_refusals_pass_through_untouched(tmp_path: Path) -> None:
    path = _doc(tmp_path)
    original = path.read_bytes()
    fp = Footprint(replaced=frozenset({1}))
    (tmp_path / "~$Novela.docx").write_bytes(b"")
    with pytest.raises(DocControlError) as err:
        write_verified(path, _edit(path), fp)
    assert err.value.code == ErrorCode.DOC_LOCKED
    assert path.read_bytes() == original
    assert not (tmp_path / ".doc-control").exists()
    (tmp_path / "~$Novela.docx").unlink()
    with pytest.raises(DocControlError) as err:
        write_verified(path, _edit(path), fp, "0" * 64)
    assert err.value.code == ErrorCode.DOC_CHANGED_ON_DISK
    assert path.read_bytes() == original


def test_non_bytes_replacement_is_a_type_error_before_anything_happens(tmp_path: Path) -> None:
    path = _doc(tmp_path)
    with pytest.raises(TypeError):
        write_verified(path, {DOCUMENT_PART: "x"}, Footprint())  # type: ignore[dict-item]
    assert not (tmp_path / ".doc-control").exists()


# -- verify_footprint ---------------------------------------------------------


def _verify(tmp_path: Path, new_doc: bytes, footprint: Footprint, **parts: bytes) -> list[str]:
    path = _doc(tmp_path)
    original = Package(path)
    out = tmp_path / "out.docx"
    out.write_bytes(path.read_bytes())
    zipio.write_package(Package(out), {DOCUMENT_PART: new_doc, **parts}, out)
    return verify_footprint(original, Package(out), footprint)


def _spliced(tmp_path: Path) -> bytes:
    return scan(Package(_doc(tmp_path)).read(DOCUMENT_PART)).splice({1: NEW_P})


def test_verifier_accepts_replace_delete_and_insert(tmp_path: Path) -> None:
    spans = scan(Package(_doc(tmp_path)).read(DOCUMENT_PART))
    fp = Footprint(replaced=frozenset({0, 2}), inserted_after=frozenset({-1, 1}))
    new = spans.splice({0: NEW_P}, insert_after={-1: [NEW_P], 1: [NEW_P]}, delete=[2])
    assert _verify(tmp_path, new, fp) == []


def test_verifier_rejects_change_outside_declared_paragraphs(tmp_path: Path) -> None:
    new = _spliced(tmp_path)
    problems = _verify(tmp_path, new, Footprint(replaced=frozenset({0})))
    assert problems == [f"{DOCUMENT_PART} changed outside the declared paragraphs"]
    assert _verify(tmp_path, new, Footprint()) != []


def test_verifier_rejects_unlisted_part_changes_and_new_members(tmp_path: Path) -> None:
    new = _spliced(tmp_path)
    fp = Footprint(replaced=frozenset({1}))
    changed = _verify(tmp_path, new, fp, **{"word/styles.xml": b"<t/>"})
    assert any("word/styles.xml" in p for p in changed)
    added = _verify(tmp_path, new, fp, **{"word/comments.xml": b"<c/>"})
    assert any("added" in p for p in added)
    ok = Footprint(
        replaced=frozenset({1}), parts=frozenset({"word/styles.xml", "word/comments.xml"})
    )
    parts = {"word/styles.xml": b"<t/>", "word/comments.xml": b"<c/>"}
    assert _verify(tmp_path, new, ok, **parts) == []


def test_verifier_rejects_unparseable_parts(tmp_path: Path) -> None:
    fp = Footprint(replaced=frozenset({1}), parts=frozenset({"word/styles.xml"}))
    bad_part = _verify(tmp_path, _spliced(tmp_path), fp, **{"word/styles.xml": b"<t>"})
    assert any("styles.xml" in p for p in bad_part)
    assert any("does not parse" in p for p in _verify(tmp_path, b"<w:document>", fp))


def test_verifier_rejects_bad_footprint_indices(tmp_path: Path) -> None:
    new = _spliced(tmp_path)
    assert _verify(tmp_path, new, Footprint(replaced=frozenset({9})))
    assert _verify(tmp_path, new, Footprint(inserted_after=frozenset({9})))


def test_package_fingerprint_sees_content_but_not_object_identity(tmp_path: Path) -> None:
    path = _doc(tmp_path)
    pkg = Package(path)
    copy = tmp_path / "copy.docx"
    copy.write_bytes(path.read_bytes())
    zipio.write_package(Package(copy), {"word/styles.xml": b"<t/>"}, copy)
    other = Package(copy)
    assert other.fingerprint("word/media/a.bin") == pkg.fingerprint("word/media/a.bin")
    assert other.fingerprint("word/styles.xml") != pkg.fingerprint("word/styles.xml")


# -- fault injection ----------------------------------------------------------


@dataclass
class Fault:
    """One injected failure: how to trigger it, and what the write is asked to do."""

    name: str
    install: Callable[[pytest.MonkeyPatch], Hook | None]
    replacements: Callable[[Path], dict[str, bytes]] = _edit
    footprint: Footprint = field(default_factory=lambda: Footprint(replaced=frozenset({1})))


def _raise_at(stage_name: str, exc: Exception) -> Callable[[pytest.MonkeyPatch], Hook]:
    def install(_mp: pytest.MonkeyPatch) -> Hook:
        def hook(name: str) -> None:
            if name == stage_name:
                raise exc

        return hook

    return install


def _real_fsync_failure(mp: pytest.MonkeyPatch) -> Hook:
    armed = False
    real = os.fsync

    def hook(name: str) -> None:
        nonlocal armed
        armed = name == "fsync"

    def fsync(fd: int) -> None:
        if armed:
            raise OSError(5, "injected EIO")
        real(fd)

    mp.setattr(zipio.os, "fsync", fsync)
    return hook


def _first_replace(
    mp: pytest.MonkeyPatch, fail: Callable[[Callable[..., None], Path, Path], None]
) -> None:
    """Make the first atomic replace misbehave; the restore's own replace is left alone."""
    real = zipio._replace
    calls = 0

    def replace(src: Path, dest: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            fail(real, src, dest)
        else:
            real(src, dest)

    mp.setattr(zipio, "_replace", replace)


def _replace_denied(mp: pytest.MonkeyPatch) -> None:
    def fail(_real: Callable[..., None], _src: Path, _dest: Path) -> None:
        raise PermissionError(13, "target held open")

    _first_replace(mp, fail)


def _replace_then_error(mp: pytest.MonkeyPatch) -> None:
    def fail(real: Callable[..., None], src: Path, dest: Path) -> None:
        real(src, dest)
        raise OSError(5, "reported failure after the rename")

    _first_replace(mp, fail)


def _replace_then_truncated(mp: pytest.MonkeyPatch) -> None:
    def fail(real: Callable[..., None], src: Path, dest: Path) -> None:
        real(src, dest)
        dest.write_bytes(dest.read_bytes()[:200])

    _first_replace(mp, fail)


def _backup_disk_full(mp: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError(28, "no space left on device")

    mp.setattr(zipio.os, "fsync", boom)


def _no_fault(_mp: pytest.MonkeyPatch) -> None:
    """The write itself is the fault: it asks for a change the footprint does not allow."""


FAULTS: list[Fault] = [
    *(Fault(f"hook_{s}", _raise_at(s, OSError(5, f"injected at {s}"))) for s in STAGES),
    *(Fault(f"hook_{s}_runtime", _raise_at(s, RuntimeError("boom"))) for s in STAGES),
    Fault("real_fsync_eio", _real_fsync_failure),
    Fault("real_replace_denied", _replace_denied),
    Fault("real_replace_applied_then_error", _replace_then_error),
    Fault("real_replace_then_truncated_file", _replace_then_truncated),
    Fault("real_backup_disk_full", _backup_disk_full),
    Fault(
        "verify_change_outside_paragraphs", _no_fault, footprint=Footprint(replaced=frozenset({0}))
    ),
    Fault(
        "verify_unlisted_part",
        _no_fault,
        replacements=lambda p: {**_edit(p), "word/styles.xml": b"<other/>"},
    ),
    Fault(
        "verify_new_unlisted_member",
        _no_fault,
        replacements=lambda p: {**_edit(p), "word/comments.xml": b"<c/>"},
    ),
    Fault(
        "verify_document_not_xml",
        _no_fault,
        replacements=lambda p: {DOCUMENT_PART: b"<w:document><w:body>"},
        footprint=Footprint(replaced=frozenset({0, 1, 2})),
    ),
]


def _inject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: Fault
) -> tuple[DocControlError, bytes]:
    path = _doc(tmp_path)
    original = path.read_bytes()
    replacements = fault.replacements(path)
    hook = fault.install(monkeypatch)
    with pytest.raises(DocControlError) as err:
        write_verified(path, replacements, fault.footprint, stage=hook)
    monkeypatch.undo()
    return err.value, original


def _partial_write(tmp_path: Path, original: bytes, fault: Fault) -> bool:
    """Whether the document, the directory or the backup is left in a bad state."""
    path = tmp_path / "Novela.docx"
    try:
        if path.read_bytes() != original:
            return True
        Package(path)
    except (OSError, DocControlError):
        return True
    store = tmp_path / ".doc-control"
    temp_files = list(store.rglob("*.tmp")) if store.exists() else []
    if _stray(tmp_path) or temp_files:
        return True
    # Every fault past the backup stage must leave a backup that restores the original.
    needs_backup = fault.name not in ("hook_backup", "hook_backup_runtime", "real_backup_disk_full")
    return needs_backup and not any(b.read_bytes() == original for b in _backups(tmp_path))


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f.name)
def test_every_fault_restores_original_and_reports_verify_failed_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: Fault
) -> None:
    err, original = _inject(tmp_path, monkeypatch, fault)
    assert err.code == ErrorCode.VERIFY_FAILED_RESTORED
    assert "stage" in err.details
    assert not _partial_write(tmp_path, original, fault)


def test_fault_error_names_the_failing_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for stage_name in STAGES:
        sub = tmp_path / stage_name
        sub.mkdir()
        fault = Fault("x", _raise_at(stage_name, OSError("injected")))
        err, _ = _inject(sub, monkeypatch, fault)
        assert err.details["stage"] == stage_name
        assert "OSError" in err.details["cause"]


def test_fault_at_each_stage_leaves_original_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    partial = 0
    for n, fault in enumerate(FAULTS):
        sub = tmp_path / f"f{n}"
        sub.mkdir()
        err, original = _inject(sub, monkeypatch, fault)
        assert err.code == ErrorCode.VERIFY_FAILED_RESTORED, fault.name
        if _partial_write(sub, original, fault):
            partial += 1
    with capsys.disabled():
        print(f"\npartial_writes_on_failure={partial} (of {len(FAULTS)} injected faults)")
    assert partial == 0


def test_failed_verification_leaves_a_restorable_backup_and_a_retry_succeeds(
    tmp_path: Path,
) -> None:
    path = _doc(tmp_path)
    original = path.read_bytes()
    with pytest.raises(DocControlError) as err:
        write_verified(path, _edit(path), Footprint(replaced=frozenset({0})))
    assert err.value.details["problems"]
    assert path.read_bytes() == original
    write_verified(path, _edit(path), Footprint(replaced=frozenset({1})))
    assert b"cambiado" in Package(path).read(DOCUMENT_PART)


def test_restore_failure_is_internal_and_names_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _doc(tmp_path)

    def boom(src: Path, dest: Path) -> None:
        raise OSError(5, "restore failed too")

    _replace_then_truncated(monkeypatch)
    monkeypatch.setattr(verify_mod, "restore_file", boom)
    with pytest.raises(DocControlError) as err:
        write_verified(path, _edit(path), Footprint(replaced=frozenset({1})))
    assert err.value.code == ErrorCode.INTERNAL
    assert Path(err.value.details["backup"]).is_file()


def test_keyboard_interrupt_restores_and_propagates(tmp_path: Path) -> None:
    path = _doc(tmp_path)
    original = path.read_bytes()

    def hook(name: str) -> None:
        if name == "verify":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        write_verified(path, _edit(path), Footprint(replaced=frozenset({1})), stage=hook)
    assert path.read_bytes() == original


def test_restored_file_keeps_permission_bits(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits")
    path = _doc(tmp_path)
    path.chmod(0o640)
    with pytest.raises(DocControlError):
        write_verified(path, _edit(path), Footprint(replaced=frozenset({0})))
    assert path.stat().st_mode & 0o777 == 0o640
