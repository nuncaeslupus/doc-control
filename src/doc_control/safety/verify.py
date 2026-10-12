"""Write a document through the safety pipeline: back up, write, verify, auto-restore.

A write runs five stages, each a seam for fault injection (``backup``,
``temp_write``, ``fsync``, ``replace``, ``verify``). The document itself is only
touched by the atomic replace, so a failure before it leaves the original
alone; a failure at or after it (including a verification that finds a change
outside the declared footprint) puts the backup back with the same atomic
replace. Either way the caller gets ``VERIFY_FAILED_RESTORED`` and the original
bytes, never a half-written file.

The restore never destroys anyone else's work. Just before the replace the
document is re-checked (lock file, sha256), and it is put back only if it is
missing or holds exactly the bytes we wrote: our own replace swaps in a fully
fsynced temp file atomically, so it can never leave a torn copy, and an empty or
partial file is another program mid-save. If anything else is on disk (Word or
a sync client saved in between), that file is left in place, a copy of it is
saved in the backups folder as
``foreign-<sha8>-<UTC stamp>.docx`` (a name retention never prunes) and the
error is ``DOC_CHANGED_ON_DISK``. Reads of the document are retried briefly on
``PermissionError``, as antivirus scanners on Windows hold files for a moment.

Unavoidable race: between the restore's last read of the document and its
replace another program can still save, and that save is lost; the window is
one read and one rename.

Known gap: a process killed between the replace and the verification leaves
the new file on disk with no marker saying it was never verified; the backup
taken before it is the way back.
"""

from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.splice import scan
from doc_control.ooxml.zipio import (
    Package,
    build_package,
    commit_bytes,
    restore_file,
    sha256_file,
)
from doc_control.safety import guard_write
from doc_control.safety.lock import check_unlocked
from doc_control.safety.store import store_dir

DOCUMENT_PART = "word/document.xml"
STAGES = ("backup", "temp_write", "fsync", "replace", "verify")
_READ_RETRY_DELAYS = (0.05, 0.1, 0.2)
_T = TypeVar("_T")


@dataclass(frozen=True)
class Footprint:
    """What a write is allowed to change.

    ``replaced`` are the indices (in the original ``word/document.xml``) of
    paragraphs that are rewritten or deleted, ``inserted_after`` those after
    which new paragraphs land (``-1``: at the start of the body). ``parts`` are
    the other zip members that may change or be created; every member not
    listed is checked byte for byte, header fields included.
    """

    replaced: frozenset[int] = frozenset()
    inserted_after: frozenset[int] = frozenset()
    parts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class WriteResult:
    before_sha256: str
    after_sha256: str
    backup: Path


class _FootprintViolation(Exception):
    def __init__(self, message: str, problems: list[str]) -> None:
        super().__init__(message)
        self.problems = problems


def verify_footprint(original: Package, result: Package, footprint: Footprint) -> list[str]:
    """Problems found comparing ``result`` with ``original``; empty means all is as declared."""
    problems: list[str] = []
    before, after = original.names, result.names
    if after[: len(before)] != before:
        problems.append("zip member names or order changed")
    added = [n for n in after[len(before) :] if n not in footprint.parts]
    if added:
        problems.append(f"members added outside the footprint: {', '.join(added)}")
    if result.comment != original.comment:
        problems.append("zip comment changed")
    for name in before:
        if name == DOCUMENT_PART or name not in result:
            continue
        if name not in footprint.parts and result.fingerprint(name) != original.fingerprint(name):
            problems.append(f"member changed outside the footprint: {name}")
    if DOCUMENT_PART in result:
        problems.extend(_document_problems(original, result, footprint))
    for name in after:
        if (
            name != DOCUMENT_PART
            and name.endswith((".xml", ".rels"))
            and _changed(original, result, name)
        ):
            try:
                ET.fromstring(result.read(name))
            except ET.ParseError as exc:
                problems.append(f"{name} does not parse: {exc}")
    return problems


def _changed(original: Package, result: Package, name: str) -> bool:
    return name not in original or original.read(name) != result.read(name)


def _document_problems(original: Package, result: Package, footprint: Footprint) -> list[str]:
    old, new = original.read(DOCUMENT_PART), result.read(DOCUMENT_PART)
    try:
        spans = scan(old)
        scan(new)
        ET.fromstring(new)
    except (DocControlError, ET.ParseError) as exc:
        return [f"{DOCUMENT_PART} does not parse: {exc}"]
    cuts: list[tuple[int, int]] = []
    for i in footprint.replaced:
        if not 0 <= i < len(spans.paragraphs):
            return [f"footprint names paragraph {i}, but the document has {len(spans.paragraphs)}"]
        cuts.append((spans.paragraphs[i].start, spans.paragraphs[i].end))
    for i in footprint.inserted_after:
        if i == -1 and spans.body_content is not None:
            cuts.append((spans.body_content, spans.body_content))
        elif 0 <= i < len(spans.paragraphs):
            cuts.append((spans.paragraphs[i].end, spans.paragraphs[i].end))
        else:
            return [f"footprint inserts after paragraph {i}, which does not exist"]
    # Everything between the touched spans must reappear, in order, in the result.
    gaps: list[bytes] = []
    pos = 0
    for start, end in sorted(cuts):
        if start >= pos:
            gaps.append(old[pos:start])
            pos = end
        else:
            pos = max(pos, end)  # nested or overlapping spans: one touched region
    gaps.append(old[pos:])
    if _gaps_match(gaps, new):
        return []
    return [f"{DOCUMENT_PART} changed outside the declared paragraphs"]


def _gaps_match(gaps: list[bytes], new: bytes) -> bool:
    if len(gaps) == 1:
        return new == gaps[0]
    first, last = gaps[0], gaps[-1]
    if not (new.startswith(first) and new.endswith(last)):
        return False
    pos, end = len(first), len(new) - len(last)
    if end < pos:
        return False
    for gap in gaps[1:-1]:
        at = new.find(gap, pos, end)
        if at < 0:
            return False
        pos = at + len(gap)
    return True


def write_verified(
    doc_path: Path,
    replacements: Mapping[str, bytes],
    footprint: Footprint,
    expect_sha256: str | None = None,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    stage: Callable[[str], None] | None = None,
) -> WriteResult:
    """Apply ``replacements`` to ``doc_path`` and verify the result against ``footprint``.

    Errors raised by the checks around the backup (``DOC_NOT_FOUND``,
    ``DOC_LOCKED``, ``DOC_CHANGED_ON_DISK``, ``SCHEMA_INVALID``, ``INTERNAL``)
    and by the re-check just before the replace pass through unchanged, with
    the document untouched; so does ``DOC_LOCKED`` when the replace is still
    denied after its retry. After that any failure, whatever the stage, puts
    the original back if the document was touched (see the module docstring for
    when it is not) and raises ``VERIFY_FAILED_RESTORED`` naming the stage. If
    the restore cannot be confirmed byte for byte the error is ``INTERNAL`` and
    names the backup; an interrupt is re-raised with that error as its cause.
    ``stage``, if given, is called with each name in ``STAGES`` just before that
    step runs: tests raise from it to inject a fault.
    """
    for name, data in replacements.items():
        if not isinstance(data, bytes):
            raise TypeError(f"replacement for {name} must be bytes")
    current = "backup"
    touched = False  # the replace may have happened: the document can differ from the original
    backup_path: Path | None = None
    created_backup = False  # this call made the file, so it may delete it
    original_sha = ""
    payload = b""

    def mark(name: str) -> None:
        nonlocal current, touched
        current = name
        if stage is not None:
            stage(name)
        if name == "replace":
            check_unlocked(doc_path)
            _check_unchanged(doc_path, original_sha)
            touched = True

    try:
        mark("backup")
        known = _existing_backups(doc_path, env)
        backup_path = guard_write(doc_path, expect_sha256, now=now, env=env)
        created_backup = backup_path not in known
        pkg = Package(doc_path)
        if sha256_file(backup_path) != pkg.sha256:
            if created_backup:
                backup_path.unlink(missing_ok=True)  # a torn copy is no backup
            backup_path = None
            raise DocControlError(
                ErrorCode.DOC_CHANGED_ON_DISK,
                f"{doc_path.name} changed on disk while it was backed up",
                path=str(doc_path),
            )
        original_sha = pkg.sha256
        payload = build_package(pkg, replacements)
        after_sha = hashlib.sha256(payload).hexdigest()
        commit_bytes(payload, doc_path, mark)
        mark("verify")
        if _retry(sha256_file, doc_path) != after_sha:
            raise OSError(f"{doc_path.name} does not hold the bytes that were written")
        problems = verify_footprint(pkg, _retry(Package, doc_path), footprint)
        if problems:
            raise _FootprintViolation("; ".join(problems), problems)
    except BaseException as exc:
        if touched and backup_path is not None:
            try:
                _restore(doc_path, backup_path, original_sha, payload)
            except DocControlError as restore_error:
                if isinstance(exc, Exception):
                    raise restore_error from exc
                raise exc from restore_error
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, DocControlError) and current in ("backup", "replace"):
            raise  # a refusal before anything was written: nothing to restore, nothing to wrap
        if current == "replace" and isinstance(exc, PermissionError):
            raise DocControlError(
                ErrorCode.DOC_LOCKED,
                f"could not replace {doc_path.name}, it is held open: {exc}",
                path=str(doc_path),
            ) from exc
        raise DocControlError(
            ErrorCode.VERIFY_FAILED_RESTORED,
            f"write failed at stage {current}, original kept: {exc}",
            stage=current,
            path=str(doc_path),
            cause=f"{type(exc).__name__}: {exc}",
            **({"problems": exc.problems} if isinstance(exc, _FootprintViolation) else {}),
        ) from exc
    return WriteResult(original_sha, after_sha, backup_path)


def _existing_backups(doc_path: Path, env: Mapping[str, str] | None) -> set[Path]:
    try:
        return set((store_dir(doc_path, env) / "backups").iterdir())
    except OSError:
        return set()


def _retry(read: Callable[[Path], _T], path: Path) -> _T:
    """``read(path)``, retried a few times on ``PermissionError`` (a scanner holding the file)."""
    for delay in _READ_RETRY_DELAYS:
        try:
            return read(path)
        except PermissionError:
            time.sleep(delay)
    return read(path)


def _check_unchanged(doc_path: Path, original_sha: str) -> None:
    actual = _retry(sha256_file, doc_path)
    if actual != original_sha:
        raise DocControlError(
            ErrorCode.DOC_CHANGED_ON_DISK,
            f"{doc_path.name} changed on disk while the write was prepared",
            path=str(doc_path),
            expected_sha256=original_sha,
            actual_sha256=actual,
        )


def _restore(doc_path: Path, backup_path: Path, original_sha: str, written: bytes) -> None:
    """Put the backup back if the document is missing or exactly what we wrote.

    Anything else (an empty or partial file included) was saved by someone else
    since our replace: leave it, keep a copy of it in the backups folder and raise
    ``DOC_CHANGED_ON_DISK``.
    """

    def internal(why: str) -> DocControlError:
        return DocControlError(
            ErrorCode.INTERNAL,
            f"{why} {doc_path.name}; the original is at {backup_path}",
            path=str(doc_path),
            backup=str(backup_path),
        )

    try:
        current: bytes | None = _retry(Path.read_bytes, doc_path)
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise internal("could not inspect") from exc
    if current is not None:
        digest = hashlib.sha256(current).hexdigest()
        if digest == original_sha:
            return
        if digest != hashlib.sha256(written).hexdigest():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            copy = backup_path.parent / f"foreign-{digest[:8]}-{stamp}.docx"
            try:
                commit_bytes(current, copy, None)
            except OSError as exc:
                raise internal("could not save a copy of the newer") from exc
            raise DocControlError(
                ErrorCode.DOC_CHANGED_ON_DISK,
                f"{doc_path.name} was saved by another program during the write; "
                "it was left as it is and a copy was kept",
                path=str(doc_path),
                saved_copy=str(copy),
                actual_sha256=digest,
            )
    try:
        restore_file(backup_path, doc_path)
        restored = _retry(sha256_file, doc_path) == original_sha
    except OSError as exc:
        raise internal("could not restore") from exc
    if not restored:
        raise internal("could not restore")
