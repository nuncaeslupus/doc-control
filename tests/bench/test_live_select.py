"""Live select latency (LIVE T6): ``live.select`` by find phrase on the 100k-word novel,
in a headless LibreOffice.

Gate: ``live_select_ms_p95 <= 500``. Skipped where LibreOffice cannot be driven.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from doc_control import api
from doc_control.live.lo_uno import LibreOffice
from tests.bench.test_perf_gate import REFERENCE_LOOP_SECONDS, _reference_loop
from tests.corpus.generator import generate
from tests.live.conftest import libreoffice, live, lo_home  # noqa: F401  (fixtures)

SELECT_MS_P95 = 500.0
SAMPLES = 40


@pytest.mark.bench
def test_live_select_latency(live: LibreOffice, lo_home: Path) -> None:  # noqa: F811
    factor = max(1.0, min(_reference_loop() for _ in range(3)) / REFERENCE_LOOP_SECONDS)
    doc = lo_home / "novel.docx"
    doc.write_bytes(generate("novel", 1))
    with api.open(doc) as handle:
        rows = handle.run("para.locate", {"structure": False})["paragraphs"]
    phrased = [r for r in rows if r.get("find_phrase")]
    picks = phrased[:: max(1, len(phrased) // SAMPLES)][:SAMPLES]
    assert len(picks) == SAMPLES
    base = {"doc": str(doc)}
    api.run("live.open", base)
    try:
        took = []
        for row in picks:
            ask = {k: row[k] for k in ("find_phrase", "find_phrase_across") if k in row}
            start = time.perf_counter()
            got = api.run("live.select", {**base, **ask}).result
            took.append((time.perf_counter() - start) * 1000)
            assert got["at"] == row["at"]
    finally:
        api.run("live.close", {**base, "discard": True})
    p95 = sorted(took)[int(len(took) * 0.95) - 1]
    print(f"live_select_ms_p95 == {p95:.0f} (budget {SELECT_MS_P95 * factor:.0f}, n={SAMPLES})")
    assert p95 <= SELECT_MS_P95 * factor
