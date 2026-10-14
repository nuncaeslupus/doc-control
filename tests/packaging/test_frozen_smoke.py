"""`scripts/frozen_smoke.py` drives a doc-control binary from outside (PACKAGE T3).

The "exe" here is the running interpreter plus an argument list (`--exe PY -- ARGS`), so
the same tests run on Linux and Windows: `-m doc_control` for the real thing, or a small
stub script that answers like doc-control but breaks one thing.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

from doc_control.mcp import tool_name
from doc_control.registry import OPERATION_MODULES

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "frozen_smoke.py"

# doc-control, except that `tools/list` answers a JSON-RPC error; `capabilities` also
# records the environment it was given, for the PATH test.
STUB = """
import json, os, sys

def main():
    dump = os.environ.get("SMOKE_STUB_ENV_DUMP")
    if dump and sys.argv[1:2] == ["capabilities"]:
        keys = ("PATH", "PYTHONHOME", "PYTHONPATH")
        with open(dump, "w", encoding="utf-8") as f:
            json.dump({k: os.environ.get(k) for k in keys}, f)
    if sys.argv[1:2] != ["mcp"]:
        from doc_control.cli import main as cli
        return cli(sys.argv[1:])
    from doc_control.mcp import handle
    from doc_control.registry import load_operations
    registry = load_operations()
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        message = json.loads(line)
        if message.get("method") == "tools/list":
            reply = {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32603, "message": "stub: tools/list is broken"}}
        else:
            reply = handle(registry, message)
        if reply is not None:
            sys.stdout.buffer.write(json.dumps(reply).encode() + b"\\n")
            sys.stdout.buffer.flush()
    return 0

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
"""


def _smoke(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        encoding="utf-8",
        timeout=600,
        check=False,
        env=env,
    )


def _stub(tmp_path: Path) -> Path:
    stub = tmp_path / "stub_doc_control.py"
    stub.write_text(STUB, encoding="utf-8")
    return stub


def test_frozen_smoke_counts_each_failing_check(tmp_path: Path) -> None:
    proc = _smoke("--exe", sys.executable, "--", str(_stub(tmp_path)))
    out = proc.stdout + proc.stderr
    assert "frozen_smoke_failures == 1" in proc.stdout, out
    assert proc.returncode == 1, out
    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAIL")]
    assert len(failed) == 1 and "tools/list" in failed[0], out


def test_strip_python_path_removes_every_python_dir(tmp_path: Path) -> None:
    fake = tmp_path / "fake-python"
    fake.mkdir()
    (fake / ("python.exe" if os.name == "nt" else "python")).write_bytes(b"")
    launcher = tmp_path / "fake-launcher"
    launcher.mkdir()
    (launcher / "py.exe").write_bytes(b"")
    plain = tmp_path / "plain"
    plain.mkdir()
    dump = tmp_path / "env.json"
    env = dict(os.environ)
    entries = [str(fake), str(launcher), str(plain), str(Path(sys.executable).parent)]
    env["PATH"] = os.pathsep.join([*entries, env.get("PATH", "")])
    # PYTHONHOME is not set here: a wrong one would break the smoke script's own interpreter.
    env |= {"PYTHONPATH": str(tmp_path), "SMOKE_STUB_ENV_DUMP": str(dump)}
    _smoke("--exe", sys.executable, "--strip-python-path", "--", str(_stub(tmp_path)), env=env)
    seen = json.loads(dump.read_text("utf-8"))
    assert seen["PYTHONHOME"] is None and seen["PYTHONPATH"] is None
    child_path = [entry for entry in seen["PATH"].split(os.pathsep) if entry]
    assert str(plain) in child_path
    module = runpy.run_path(str(SCRIPT))
    for entry in child_path:
        assert not module["holds_python"](entry), entry
    assert str(fake) not in child_path and str(launcher) not in child_path
    assert "PYTHONHOME" not in module["child_env"]({"PATH": "", "PYTHONHOME": "x"}, True)


def test_frozen_smoke_passes_on_python_m_doc_control() -> None:
    proc = _smoke("--exe", sys.executable, "--", "-m", "doc_control")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert proc.stdout.splitlines()[-1] == "frozen_smoke_failures == 0", out
    # Not frozen: `--frozen` turns the build check on and fails only that check.
    proc = _smoke("--exe", sys.executable, "--frozen", "--", "-m", "doc_control")
    assert "frozen_smoke_failures == 1" in proc.stdout, proc.stdout + proc.stderr


def test_every_op_family_has_a_call() -> None:
    module: dict[str, Any] = runpy.run_path(str(SCRIPT))
    calls: dict[str, tuple[str, dict[str, Any]]] = module["FAMILY_CALLS"]
    assert set(calls) == set(OPERATION_MODULES.values())
    tools = {tool_name(op): family for op, family in OPERATION_MODULES.items()}
    for family, (tool, _) in calls.items():
        assert tools.get(tool) == family, (family, tool)


def test_package_workflow_runs_the_frozen_smoke() -> None:
    package = (ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")
    command = (
        "scripts/frozen_smoke.py --exe build-out/doc-control/doc-control.exe "
        "--strip-python-path --frozen"
    )
    assert command in package
