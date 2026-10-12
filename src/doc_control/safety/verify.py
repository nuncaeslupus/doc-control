"""Write a document through the safety pipeline: back up, write, verify, auto-restore.

A write runs five stages, each a seam for fault injection (``backup``,
``temp_write``, ``fsync``, ``replace``, ``verify``). The document itself is only
touched by the atomic replace, so a failure before it leaves the original
alone; a failure at or after it (including a verification that finds a change
outside the declared footprint) puts the backup back with the same atomic
replace. Either way the caller gets ``VERIFY_FAILED_RESTORED`` and the original
bytes, never a half-written file.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.splice import scan
from doc_control.ooxml.zipio import Package, restore_file, sha256_file, write_package
from doc_control.safety import guard_write

DOCUMENT_PART = "word/document.xml"
STAGES = ("backup", "temp_write", "fsync", "replace", "verify")


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

    The pre-write checks of ``guard_write`` (and its errors) come first and leave
    the document alone. After that any failure, whatever the stage, restores the
    original from the backup if the document was touched and raises
    ``VERIFY_FAILED_RESTORED`` naming the stage; if the restore itself cannot be
    confirmed byte for byte the error is ``INTERNAL`` and names the backup.
    ``stage``, if given, is called with each name in ``STAGES`` just before that
    step runs: tests raise from it to inject a fault.
    """
    for name, data in replacements.items():
        if not isinstance(data, bytes):
            raise TypeError(f"replacement for {name} must be bytes")
    current = "backup"
    backup_path: Path | None = None
    original_sha = ""

    def mark(name: str) -> None:
        nonlocal current
        current = name
        if stage is not None:
            stage(name)

    try:
        mark("backup")
        backup_path = guard_write(doc_path, expect_sha256, now=now, env=env)
        pkg = Package(doc_path)
        original_sha = pkg.sha256
        if sha256_file(backup_path) != original_sha:
            raise DocControlError(
                ErrorCode.DOC_CHANGED_ON_DISK,
                f"{doc_path.name} changed on disk while it was backed up",
                path=str(doc_path),
            )
        after_sha = write_package(pkg, replacements, doc_path, mark)
        mark("verify")
        if sha256_file(doc_path) != after_sha:
            raise OSError(f"{doc_path.name} does not hold the bytes that were written")
        problems = verify_footprint(pkg, Package(doc_path), footprint)
        if problems:
            raise _FootprintViolation("; ".join(problems), problems)
    except BaseException as exc:
        if backup_path is not None:
            _restore(doc_path, backup_path, original_sha)
        if not isinstance(exc, Exception):
            raise
        if (
            current == "backup"
            and isinstance(exc, DocControlError)
            and (exc.code != ErrorCode.INTERNAL)
        ):
            raise  # a guard refusal (locked, changed, bad config): nothing was touched
        raise DocControlError(
            ErrorCode.VERIFY_FAILED_RESTORED,
            f"write failed at stage {current}, original kept: {exc}",
            stage=current,
            path=str(doc_path),
            cause=f"{type(exc).__name__}: {exc}",
            **({"problems": exc.problems} if isinstance(exc, _FootprintViolation) else {}),
        ) from exc
    return WriteResult(original_sha, after_sha, backup_path)


def _restore(doc_path: Path, backup_path: Path, original_sha: str) -> None:
    """Put the backup back unless the document already holds the original bytes."""
    try:
        if sha256_file(doc_path) == original_sha:
            return
    except OSError:
        pass  # missing or unreadable: restore below
    try:
        restore_file(backup_path, doc_path)
        restored = sha256_file(doc_path) == original_sha
    except OSError:
        restored = False
    if not restored:
        raise DocControlError(
            ErrorCode.INTERNAL,
            f"could not restore {doc_path.name}; the original is at {backup_path}",
            path=str(doc_path),
            backup=str(backup_path),
        )
