"""LIVE T8: check the live engine against real Microsoft Word (Windows, Word 2016+).

    uv run python scripts/live_word_check.py

Runs the scenarios of ``tests/live/word_suite.py`` (the ones LibreOffice passes) in a
hidden Word of its own, so a document you have open is not touched, and times
``live.select`` on the 100k-word novel. Prints ``live_word_failures == N`` and exits
non-zero unless N is 0. Needs ``pywin32`` (``pip install doc-control[live]``).
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from doc_control import api  # noqa: E402
from doc_control.live.word_com import Word  # noqa: E402
from tests.corpus.generator import generate  # noqa: E402
from tests.live import word_suite  # noqa: E402

SAMPLES = 40


def select_ms(word: Word, home: Path) -> list[float]:
    """How long ``live.select`` by find phrase takes on the novel, per call."""
    doc = home / "novel.docx"
    doc.write_bytes(generate("novel", 1))
    with api.open(doc) as handle:
        rows = handle.run("locate", {"structure": False})["paragraphs"]
    phrased = [r for r in rows if r.get("find_phrase")]
    picks = phrased[:: max(1, len(phrased) // SAMPLES)][:SAMPLES]
    base = {"doc": str(doc)}
    api.run("live.open", base)
    took = []
    try:
        for row in picks:
            ask = {k: row[k] for k in ("find_phrase", "find_phrase_across") if k in row}
            start = time.perf_counter()
            got = api.run("live.select", {**base, **ask}).result
            took.append((time.perf_counter() - start) * 1000)
            if got["at"] != row["at"]:
                raise SystemExit(f"select on the novel went to {got['at']}, not {row['at']}")
    finally:
        word.close(doc)
    return took


def main() -> int:
    reason = word_suite.unavailable()
    if reason:
        print(f"Word cannot be driven here: {reason}")
        return 2
    with tempfile.TemporaryDirectory(prefix="doc-control-word-") as folder:
        home = Path(folder)
        with word_suite.hidden_word() as word:
            app = word._application()
            print(f"Word {app.Version} build {app.Build}")
            took = select_ms(word, home)
            failures = word_suite.run_all(word, home)
    for failure in failures:
        print(f"FAIL {failure}")
    p95 = statistics.quantiles(took, n=20)[-1]
    print(f"live_select_ms_p95 == {p95:.0f} (median {statistics.median(took):.0f}, not gated)")
    print(f"live_word_failures == {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
