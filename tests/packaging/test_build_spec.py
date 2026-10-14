"""The PyInstaller build file names what a freezer cannot see, and CI runs it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from doc_control.live.dispatch import ADAPTER_MODULES
from doc_control.registry import OPERATION_MODULES

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "packaging" / "doc-control.spec"
WORKFLOWS = ROOT / ".github" / "workflows"


def _analysis() -> dict[str, Any]:
    """Run the build file with PyInstaller's classes stubbed; the Analysis arguments."""
    seen: dict[str, Any] = {}

    class Analysis:
        def __init__(self, scripts: list[str], **kwargs: Any) -> None:
            seen.update(kwargs, scripts=scripts)
            self.pure: list[Any] = []
            self.scripts: list[Any] = []
            self.binaries: list[Any] = []
            self.datas: list[Any] = []

    def build(*args: Any, **kwargs: Any) -> None:
        return None

    names = {"SPECPATH": str(SPEC.parent), "Analysis": Analysis}
    names |= {"PYZ": build, "EXE": build, "COLLECT": build}
    exec(compile(SPEC.read_text("utf-8"), str(SPEC), "exec"), names)
    return seen


def test_spec_names_hidden_imports_data_and_excludes() -> None:
    seen = _analysis()
    hidden = set(seen["hiddenimports"])
    assert {"pythoncom", "pywintypes", "win32com.client", "winreg"} <= hidden
    assert {f"doc_control.ops.{m}" for m in OPERATION_MODULES.values()} <= hidden
    assert {f"doc_control.live.{m}" for m in ADAPTER_MODULES.values()} <= hidden
    assert not any(name.startswith("tests") for name in hidden)
    assert {"tkinter", "tests"} <= set(seen["excludes"])
    assert [Path(s) for s in seen["scripts"]] == [ROOT / "packaging" / "entry.py"]
    found = {dest: list(Path(src).parent.glob(Path(src).name)) for src, dest in seen["datas"]}
    recipes = found["doc_control/recipes/bundled"]
    assert recipes and all(path.suffix == ".json" for path in recipes)
    assert found["doc_control/app"] == [ROOT / "src" / "doc_control" / "app" / "places.html"]


def test_package_workflow_is_called_from_ci_and_checks_size() -> None:
    package = (WORKFLOWS / "package.yml").read_text("utf-8")
    assert "workflow_call:" in package and "push:" not in package
    assert "windows-latest" in package and "packaging/doc-control.spec" in package
    assert 'MAX_FROZEN_MB: "40"' in package and "frozen_exe_size_mb=" in package
    ci = (WORKFLOWS / "ci.yml").read_text("utf-8")
    assert "uses: ./.github/workflows/package.yml" in ci
    assert "needs.packaging-inputs.outputs.changed == 'true'" in ci


def test_entry_calls_freeze_support_before_main() -> None:
    entry = (ROOT / "packaging" / "entry.py").read_text("utf-8")
    assert entry.index("multiprocessing.freeze_support()") < entry.index("sys.exit(main())")


def test_smoke_runs_capabilities_and_a_regex_find() -> None:
    package = (WORKFLOWS / "package.yml").read_text("utf-8")
    assert "packaging/smoke.py --frozen build-out/doc-control/doc-control.exe" in package
    proc = subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "smoke.py"), sys.executable, "-m", "doc_control"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "regex find ok" in proc.stdout
