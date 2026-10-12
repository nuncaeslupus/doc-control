"""Shared fixtures: synthetic corpus documents written to tmp paths."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.corpus.generator import generate

CorpusWriter = Callable[[str, int], Path]


def write_corpus_doc(directory: Path, kind: str, seed: int = 0) -> Path:
    """Write the synthetic ``kind`` document for ``seed`` under ``directory``."""
    path = directory / f"{kind}-{seed}.docx"
    path.write_bytes(generate(kind, seed))
    return path


@pytest.fixture
def corpus_doc(tmp_path: Path) -> CorpusWriter:
    """Factory fixture: ``corpus_doc(kind, seed)`` returns a path to a fresh .docx."""

    def write(kind: str, seed: int = 0) -> Path:
        return write_corpus_doc(tmp_path, kind, seed)

    return write
