"""Pre-write checks: editors' lock files and on-disk change detection.

When Word opens ``Name.docx`` it creates a hidden owner file next to it whose
name is ``~$`` followed by the document name. To keep the result short Word
truncates long names: with a stem of 7 characters the first character is
dropped (``Capitul.docx`` -> ``~$apitul.docx``), and with a stem of 8 or more
the first two are (``Manuscrito.docx`` -> ``~$nuscrito.docx``). Short stems get
the plain prefix (``Novela.docx`` -> ``~$Novela.docx``). Other Office builds and
sync clients have been seen to use the plain prefix for any length, so it is
always checked too: a false positive only refuses a write, a miss could
overwrite a document Word is editing. OnlyOffice and WPS use Word's ``~$`` names;
LibreOffice writes ``.~lock.<name>#`` next to the document, which is checked too.
"""

from __future__ import annotations

from pathlib import Path

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.zipio import sha256_file

LOCK_PREFIX = "~$"


def lock_file_candidates(doc_path: Path) -> list[Path]:
    """Every name Word may give the owner lock file for ``doc_path``, plain form first."""
    name = doc_path.name
    stem_len = len(doc_path.stem)
    names = [LOCK_PREFIX + name]
    if stem_len == 7:
        names.append(LOCK_PREFIX + name[1:])
    elif stem_len >= 8:
        names.append(LOCK_PREFIX + name[2:])
    names.append(f".~lock.{name}#")  # LibreOffice
    return [doc_path.with_name(n) for n in names]


def find_lock_file(doc_path: Path) -> Path | None:
    for candidate in lock_file_candidates(doc_path):
        if candidate.exists():
            return candidate
    return None


def check_unlocked(doc_path: Path) -> None:
    """Raise ``DOC_LOCKED`` if Word's owner lock file sits next to the document."""
    lock = find_lock_file(doc_path)
    if lock is not None:
        raise DocControlError(
            ErrorCode.DOC_LOCKED,
            f"an editor has {doc_path.name} open (lock file {lock.name})",
            path=str(doc_path),
            lock_file=str(lock),
        )


def check_changed(doc_path: Path, expect_sha256: str | None) -> str:
    """Raise ``DOC_CHANGED_ON_DISK`` unless the file hashes to ``expect_sha256``.

    With no expectation the check is skipped. Returns the actual sha256.
    """
    actual = sha256_file(doc_path)
    if expect_sha256 is not None and actual != expect_sha256.strip().lower():
        raise DocControlError(
            ErrorCode.DOC_CHANGED_ON_DISK,
            f"{doc_path.name} changed on disk since it was read",
            path=str(doc_path),
            expected_sha256=expect_sha256,
            actual_sha256=actual,
        )
    return actual
