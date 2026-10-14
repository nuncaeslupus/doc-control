"""Body reads must not slow down because documents can now have stories (STORIES T11).

Gate: ``paragraphs_list_ratio <= 1.10``. ``paragraphs.list`` (body only, the default
``stories``) on the corpus novel is timed on this tree and on the 0.6.0 release, measured
live on the same machine: the release is checked out into a temporary ``git worktree`` and
timed in a subprocess with its own ``src`` on ``PYTHONPATH``. The two are interleaved and
the best of several runs is kept, so machine load hits both alike. Opt-in like every
wall-clock test (``make bench``); it runs when named by node id.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.corpus.generator import generate

ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_TAG = "v0.6.0"
MAX_RATIO = 1.10
ROUNDS = 7
RUNS_PER_ROUND = 3

# Opens the document afresh each run (the index build is part of a cold body read) and
# prints the best time of the runs, in seconds. Uses only API that 0.6.0 already has.
_PROBE = """
import sys, time
from doc_control import api
path, store, runs = sys.argv[1], sys.argv[2], int(sys.argv[3])
best = float("inf")
for _ in range(runs):
    with api.open(path, store=store) as doc:
        start = time.perf_counter()
        rows = doc.paragraphs()
        best = min(best, time.perf_counter() - start)
assert rows
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
    return float(out.stdout.strip().splitlines()[-1])


@pytest.mark.bench
@pytest.mark.skipif(sys.platform != "linux", reason="needs git and a POSIX temp tree")
def test_body_read_time_within_ratio(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is needed to check out the 0.6.0 baseline")
    baseline = Path(tempfile.mkdtemp(prefix="doc-control-0.6.0-"))
    shutil.rmtree(baseline)  # `git worktree add` wants to create it
    added = subprocess.run(
        ["git", "worktree", "add", "--detach", str(baseline), BASELINE_TAG],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        pytest.skip(f"baseline {BASELINE_TAG} is not available: {added.stderr.strip()}")
    try:
        docx = tmp_path / "novel.docx"
        docx.write_bytes(generate("novel", 1))
        new_src, old_src = ROOT / "src", baseline / "src"
        new_best = old_best = float("inf")
        _probe(old_src, docx, tmp_path / "warm")  # warm file caches and bytecode
        _probe(new_src, docx, tmp_path / "warm")
        for k in range(ROUNDS):
            # Interleave, alternating who goes first, so drift does not favour one side.
            order = (("old", old_src), ("new", new_src))
            for name, src in order if k % 2 == 0 else order[::-1]:
                t = _probe(src, docx, tmp_path / f"store-{name}-{k}")
                if name == "old":
                    old_best = min(old_best, t)
                else:
                    new_best = min(new_best, t)
        ratio = new_best / old_best
        print(
            f"paragraphs.list on the novel: {BASELINE_TAG} {old_best * 1000:.1f} ms, "
            f"now {new_best * 1000:.1f} ms"
        )
        print(f"paragraphs_list_ratio == {ratio:.3f} (limit {MAX_RATIO})")
        assert ratio <= MAX_RATIO
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(baseline)],
            cwd=ROOT,
            capture_output=True,
        )
        shutil.rmtree(baseline, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)
