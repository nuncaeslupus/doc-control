"""Body reads must not slow down because documents can now have stories (STORIES T11).

Gate: ``paragraphs_list_ratio <= 1.10``. ``paragraphs.list`` (body only, the default
``stories``) on the corpus novel is timed on this tree and on the 0.6.0 release, measured
live on the same machine: the release is checked out into a temporary ``git worktree`` and
timed in a subprocess with its own ``src`` on ``PYTHONPATH`` (each probe checks it imported
the ``doc_control`` it was pointed at). The two are interleaved and the gate is the median
of the per-round ratios, so machine load and drift hit both alike. Opt-in like every
wall-clock test (``make bench``); it runs when named by node id. Named that way (a task
gate), a missing baseline (no git, not Linux, no ``v0.6.0`` tag in a shallow clone) fails
instead of skipping: a skip exits 0 and would pass the gate unmeasured.
"""

from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.corpus.generator import generate

ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_TAG = "v0.6.0"
MAX_RATIO = 1.10
ROUNDS = 11
RUNS_PER_ROUND = 3

# Opens the document afresh each run (the index build is part of a cold body read) and
# prints where doc_control came from, then the best time of the runs, in seconds. Uses
# only API that 0.6.0 already has.
_PROBE = """
import sys, time
import doc_control
from doc_control import api
path, store, runs = sys.argv[1], sys.argv[2], int(sys.argv[3])
best = float("inf")
for _ in range(runs):
    with api.open(path, store=store) as doc:
        start = time.perf_counter()
        rows = doc.paragraphs()
        best = min(best, time.perf_counter() - start)
assert rows
print(doc_control.__file__)
print(best)
"""


def _probe(src: Path, docx: Path, store: Path) -> float:
    env = {**os.environ, "PYTHONPATH": str(src), "DOC_CONTROL_STORE": "appdata"}
    env["DOC_CONTROL_HOME"] = str(store.parent / "home")
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, str(docx), str(store), str(RUNS_PER_ROUND)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        cwd=str(store.parent),
    )
    origin, best = out.stdout.strip().splitlines()[-2:]
    assert Path(origin).resolve().is_relative_to(src.resolve()), (
        f"probe for {src} imported doc_control from {origin}"
    )
    return float(best)


def _unavailable(gate: bool, reason: str) -> None:
    if gate:
        pytest.fail(f"paragraphs_list_ratio unmeasured: {reason}", pytrace=False)
    pytest.skip(reason)


def _add_baseline(tag: str, gate: bool) -> Path:
    """Check ``tag`` out into a temporary worktree; skip, or fail under a gate, if impossible."""
    if sys.platform != "linux":
        _unavailable(gate, "needs git and a POSIX temp tree")
    if shutil.which("git") is None:
        _unavailable(gate, f"git is needed to check out the {tag} baseline")
    baseline = Path(tempfile.mkdtemp(prefix=f"doc-control-{tag}-"))
    shutil.rmtree(baseline)  # `git worktree add` wants to create it
    added = subprocess.run(
        ["git", "worktree", "add", "--detach", str(baseline), tag],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        _unavailable(gate, f"baseline {tag} is not available: {added.stderr.strip()}")
    return baseline


def test_bench_without_baseline_fails_under_gate() -> None:
    missing = "v0.0.0-no-such-baseline"
    with pytest.raises(pytest.fail.Exception, match="unmeasured"):
        _add_baseline(missing, gate=True)
    with pytest.raises(pytest.skip.Exception):
        _add_baseline(missing, gate=False)


@pytest.mark.bench
def test_body_read_time_within_ratio(tmp_path: Path, gate_mode: bool) -> None:
    baseline = _add_baseline(BASELINE_TAG, gate_mode)
    try:
        docx = tmp_path / "novel.docx"
        docx.write_bytes(generate("novel", 1))
        new_src, old_src = ROOT / "src", baseline / "src"
        ratios: list[float] = []
        _probe(old_src, docx, tmp_path / "warm")  # warm file caches and bytecode
        _probe(new_src, docx, tmp_path / "warm")
        for k in range(ROUNDS):
            # Interleave, alternating who goes first, so drift does not favour one side.
            order = (("old", old_src), ("new", new_src))
            times = {
                name: _probe(src, docx, tmp_path / f"store-{name}-{k}")
                for name, src in (order if k % 2 == 0 else order[::-1])
            }
            ratios.append(times["new"] / times["old"])
        ratio = statistics.median(ratios)
        print(f"paragraphs.list on the novel, per-round ratios vs {BASELINE_TAG}:")
        print(" ".join(f"{r:.3f}" for r in ratios))
        print(f"paragraphs_list_ratio == {ratio:.3f} (median of {ROUNDS}, limit {MAX_RATIO})")
        assert ratio <= MAX_RATIO
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(baseline)],
            cwd=ROOT,
            capture_output=True,
        )
        shutil.rmtree(baseline, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)
