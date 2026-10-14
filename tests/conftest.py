"""Shared fixtures: synthetic corpus documents written to tmp paths."""

from __future__ import annotations

import hashlib
import os
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


@pytest.fixture(autouse=True)
def _keep_written_documents(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """CI hook: with ``DOC_CONTROL_KEEP_DOCS=<dir>``, copy every package the tool commits.

    The OOXML validation job points this at a directory and lints what was kept
    (``scripts/ci_validate_docs.py``). Without the variable this does nothing.
    """
    target = os.environ.get("DOC_CONTROL_KEEP_DOCS")
    if not target:
        return
    from doc_control.ooxml import zipio
    from doc_control.safety import verify

    keep = Path(target)
    keep.mkdir(parents=True, exist_ok=True)
    original = zipio.commit_bytes

    def recording(payload: bytes, dest: Path, stage: Callable[[str], None] | None) -> None:
        if request.node.get_closest_marker("writes_invalid_docs"):
            original(payload, dest, stage)
            return
        before = dest.read_bytes() if dest.is_file() else None
        original(payload, dest, stage)
        name = hashlib.sha256(payload).hexdigest()[:16]
        (keep / f"{name}.docx").write_bytes(payload)
        (keep / f"{name}.test").write_text(request.node.nodeid, encoding="utf-8")
        if before is not None:
            (keep / f"{name}.before").write_bytes(before)

    monkeypatch.setattr(zipio, "commit_bytes", recording)
    monkeypatch.setattr(verify, "commit_bytes", recording)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """A ``bench`` test named on the command line by its node id runs even under the
    default ``-m 'not bench'`` (a task gate names its benchmark that way), and so does one
    named exactly by ``-k <test name>``."""
    named = {arg for arg in config.args if "::" in arg}
    keyword = str(config.getoption("keyword", "") or "").strip()
    for item in items:
        if item.name == keyword or any(
            item.nodeid.endswith(arg.split("/", 1)[-1]) or item.nodeid == arg for arg in named
        ):
            item.own_markers = [m for m in item.own_markers if m.name != "bench"]
