"""Private-corpus hook: real documents that never enter the repository.

Set ``DOC_CONTROL_PRIVATE_CORPUS`` to a directory of .docx files to run the
private-corpus tests; without it they skip. Documents are identified only by a
short sha256 of their bytes. File names must never appear in test ids, logs or
assertion messages, so nothing here returns or formats a path for display.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

ENV_VAR = "DOC_CONTROL_PRIVATE_CORPUS"
ID_LENGTH = 12


def corpus_dir() -> Path | None:
    value = os.environ.get(ENV_VAR)
    return Path(value) if value else None


def short_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:ID_LENGTH]


def private_documents() -> Iterator[tuple[str, bytes]]:
    """Yield ``(short sha256 id, bytes)`` for each .docx under the private corpus.

    Word lock files (``~$*``) are skipped. Order is by id, so it reveals nothing
    about the directory layout.
    """
    root = corpus_dir()
    if root is None or not root.is_dir():
        return
    docs = {}
    for path in root.rglob("*.docx"):
        if path.name.startswith("~$") or not path.is_file():
            continue
        data = path.read_bytes()
        docs[short_id(data)] = data
    yield from sorted(docs.items())
