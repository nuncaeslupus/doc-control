"""Cold bulk read (T47): the shared, C-parsed read path gives the same rows, faster.

Gate: ``cold_bulk_read_regressions == 0``: on every corpus kind the bulk read through the
read-only parse equals the one through the bookkeeping parser writers use, and the
100k-word novel's first ``paragraphs(normalized=True, pages=True)`` meets its budget.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.ooxml.splice import DocumentSpans
from tests.bench.test_perf_gate import REFERENCE_LOOP_SECONDS, _reference_loop
from tests.corpus.generator import KINDS, generate

COLD_BULK_SECONDS = 0.8


def _rows(path: Path) -> list[dict[str, Any]]:
    with api.open(path) as doc:
        return doc.paragraphs(normalized=True, pages=True)


def _mismatches(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    bad = 0
    for kind in (*KINDS, "novel"):
        path = tmp / f"{kind}.docx"
        path.write_bytes(generate(kind, 3))
        fast = _rows(path)
        with monkeypatch.context() as m:
            m.setattr(DocumentSpans, "_read_only", DocumentSpans.parse_paragraph)
            slow = _rows(path)
        bad += fast != slow
    return bad


def test_read_only_parse_gives_the_same_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _mismatches(tmp_path, monkeypatch) == 0


@pytest.mark.bench
def test_cold_bulk_read_same_rows_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    regressions = _mismatches(tmp_path, monkeypatch)
    factor = max(1.0, min(_reference_loop() for _ in range(3)) / REFERENCE_LOOP_SECONDS)
    path = tmp_path / "cold.docx"
    path.write_bytes(generate("novel", 1))
    api.run("server.capabilities", {})  # imports the operations, as any first call does
    start = time.perf_counter()
    _rows(path)
    took = time.perf_counter() - start
    regressions += took > COLD_BULK_SECONDS * factor
    print(f"cold bulk {took:.3f}s (budget {COLD_BULK_SECONDS * factor:.3f}s)")
    print(f"cold_bulk_read_regressions == {regressions}")
    assert regressions == 0
