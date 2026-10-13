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

import socket
from pathlib import Path
from typing import Any

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


def _libreoffice_running() -> bool | None:
    """Whether a LibreOffice process runs on this machine; None where that cannot be told."""
    proc = Path("/proc")
    if not (proc / "self" / "comm").is_file():
        return None  # ponytail: Linux only; a process list for Windows and macOS when needed
    for comm in proc.glob("[0-9]*/comm"):
        try:
            if comm.read_text().strip() == "soffice.bin":
                return True
        except OSError:
            continue
    return False


def lock_info(doc_path: Path) -> dict[str, Any] | None:
    """The editor holding ``doc_path`` by its lock file: ``{"app", "lock_file", "stale"}``.

    ``stale`` is True only when the lock is provably left over: a LibreOffice lock written
    on this machine while no LibreOffice runs here. It is None when that cannot be told:
    Word's lock names no machine, so the document may be open over a shared folder.
    """
    lock = find_lock_file(doc_path)
    if lock is None:
        return None
    if not lock.name.startswith(".~lock."):
        return {"app": "word", "lock_file": str(lock), "stale": None}
    stale: bool | None = None
    try:
        # ``name,login,host,date,profile;``
        fields = lock.read_text(encoding="utf-8", errors="replace").split(",")
    except OSError:
        fields = []
    if len(fields) > 2 and fields[2] == socket.gethostname():
        running = _libreoffice_running()
        stale = None if running is None else not running
    return {"app": "libreoffice", "lock_file": str(lock), "stale": stale}


_APP_NAMES = {"word": "Word", "libreoffice": "LibreOffice"}


def check_unlocked(doc_path: Path) -> None:
    """Raise ``DOC_LOCKED`` if an editor's lock file sits next to the document."""
    info = lock_info(doc_path)
    if info is not None:
        lock = Path(info["lock_file"])
        left_over = " left over: no LibreOffice is running" if info["stale"] else ""
        raise DocControlError(
            ErrorCode.DOC_LOCKED,
            f"{_APP_NAMES[info['app']]} has {doc_path.name} open"
            f" (lock file {lock.name}{left_over})",
            path=str(doc_path),
            **info,
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
