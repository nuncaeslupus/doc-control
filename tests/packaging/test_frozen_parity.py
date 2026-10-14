"""`scripts/frozen_parity.py` compares a doc-control binary with in-process doc_control (T4).

As in `test_frozen_smoke.py`, the "exe" is the running interpreter plus an argument list
(`--exe PY -- ARGS`): `-m doc_control` for the real thing, or a stub that alters one result.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "frozen_parity.py"

# doc-control, except that one `find` (the kitchen-sink fixture, query "the") answers a
# different text for its first match.
STUB = """
import sys
from doc_control import cli

_execute = cli.execute

def execute(registry, op, request):
    envelope, code = _execute(registry, op, request)
    if (op == "find" and str(request.get("doc", "")).endswith("kitchen_sink.docx")
            and request.get("query") == "the" and envelope.get("ok")):
        envelope["result"]["matches"][0]["text"] = "teh"
    return envelope, code

cli.execute = execute

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(cli.main(sys.argv[1:]))
"""


def _parity(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        encoding="utf-8",
        timeout=600,
        check=False,
    )


def test_parity_reports_a_changed_find_result(tmp_path: Path) -> None:
    stub = tmp_path / "stub_doc_control.py"
    stub.write_text(STUB, encoding="utf-8")
    proc = _parity("--exe", sys.executable, "--", str(stub))
    out = proc.stdout + proc.stderr
    assert proc.stdout.splitlines()[-1] == "frozen_vs_python_result_diffs == 1", out
    assert proc.returncode == 1, out
    diffs = [line for line in proc.stdout.splitlines() if line.startswith("DIFF")]
    assert len(diffs) == 1 and "kitchen_sink" in diffs[0] and "teh" in diffs[0], out


def test_parity_passes_on_python_m_doc_control() -> None:
    proc = _parity("--exe", sys.executable, "--", "-m", "doc_control")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert proc.stdout.splitlines()[-1] == "frozen_vs_python_result_diffs == 0", out


def test_package_workflow_runs_the_parity_check() -> None:
    package = (ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")
    command = "scripts/frozen_parity.py --exe build-out/doc-control/doc-control.exe"
    assert command in package
    smoke = package.index("scripts/frozen_smoke.py --exe")
    assert package.index(command) > smoke
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert r"scripts/frozen_parity\.py$" in ci
