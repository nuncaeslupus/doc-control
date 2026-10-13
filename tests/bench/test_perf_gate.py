"""Performance budgets on the 100k-word novel through ``doc_control.api`` (T46).

Gate: ``perf_budget_breaches == 0``. Budgets are seconds on a reference machine, scaled by
how slow this runner is: a fixed pure-Python loop is timed and compared with its reference
time, so a slow CI runner gets proportionally more room (never less than the reference).
Like every wall-clock budget it is opt-in (``make bench``, the CI bench job), and it runs
when named by node id (the T46 gate).
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from tests.corpus.generator import generate

REFERENCE_LOOP_SECONDS = 0.024  # _reference_loop() on the reference machine (best of 3)
BUDGETS = {
    "first_parse": 0.6,
    "read_op_p95": 0.05,  # an op repeated on the open handle
    "paragraphs": 0.3,
    "locate": 1.2,  # cold: builds the structure and page estimate too
    "batch_20_edits": 1.5,
    "term_rename": 2.0,
}


def _reference_loop() -> float:
    start = time.perf_counter()
    total = 0
    for k in range(400_000):
        total += k % 7
    assert total > 0
    return time.perf_counter() - start


def _timed(fn: Callable[[], Any]) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


@pytest.mark.bench
@pytest.mark.skipif(sys.platform != "linux", reason="budgets are calibrated on Linux runners")
def test_perf_budgets_100k(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_CONTROL_STORE", "appdata")
    monkeypatch.setenv("DOC_CONTROL_HOME", str(tmp_path / "home"))
    factor = max(1.0, min(_reference_loop() for _ in range(3)) / REFERENCE_LOOP_SECONDS)
    path = tmp_path / "novel.docx"
    path.write_bytes(generate("novel", 1))
    seen: dict[str, float] = {}
    with api.open(path) as doc:
        seen["first_parse"] = _timed(lambda: doc.run("outline"))
        seen["paragraphs"] = _timed(doc.paragraphs)
        seen["locate"] = _timed(lambda: doc.run("locate"))
        at = doc.paragraphs()[100]["at"]
        reads: list[float] = []
        for op, request in [
            ("stats", {}),
            ("outline", {}),
            ("segment", {}),
            ("segment", {"unit": "scenes"}),
            ("para.get", {"at": at}),
            ("styles.list", {}),
            ("page.estimate", {}),
            ("find", {"query": "lluvia"}),
            ("locate", {"at": at}),
        ]:
            doc.run(op, request)  # the first call may build a derived table once
            for _ in range(5):
                start = time.perf_counter()
                doc.run(op, request)
                reads.append(time.perf_counter() - start)
        seen["read_op_p95"] = statistics.quantiles(reads, n=20)[18]
        rows = doc.paragraphs()
        ops = [
            {"op": "text.replace", "at": f"{r['at']}:0-1", "text": "X"}
            for r in rows[10:900:20]
            if r["text"]
        ][:20]
        assert len(ops) == 20
        seen["batch_20_edits"] = _timed(lambda: doc.run("batch", {"ops": ops}))
        seen["term_rename"] = _timed(
            lambda: doc.run("term.rename", {"from": "lluvia", "to": "nieve"})
        )
    breaches = [k for k, v in seen.items() if v > BUDGETS[k] * factor]
    shown = ", ".join(f"{k}={v:.3f}s/{BUDGETS[k] * factor:.3f}s" for k, v in seen.items())
    print(f"runner factor {factor:.2f}; {shown}")
    print(f"perf_budget_breaches == {len(breaches)} (of {len(BUDGETS)} budgets)")
    assert breaches == []
