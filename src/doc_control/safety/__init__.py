"""Safety layer: checks and a backup that run before any write to a document."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from doc_control.errors import DocControlError, ErrorCode
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.store import backup

__all__ = ["guard_write"]


def guard_write(
    doc_path: Path,
    expect_sha256: str | None,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Run before any write: lock check, change check, then a backup.

    Raises ``DOC_NOT_FOUND``, ``DOC_LOCKED`` or ``DOC_CHANGED_ON_DISK`` without
    touching the document or creating a store; returns the backup path.
    """
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    return backup(doc_path, now=now, env=env)
