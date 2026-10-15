"""Package-level guarantees: stdlib only, version envelope, packaging stays out of the core."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from doc_control.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_package_imports_without_third_party_modules() -> None:
    """Importing doc_control loads no module outside the standard library."""
    code = (
        "import sys; before = set(sys.modules); import doc_control, doc_control.cli; "
        "print('\\n'.join(sorted(set(sys.modules) - before)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    third_party = [
        m
        for m in out
        if m.split(".")[0] not in sys.stdlib_module_names and m.split(".")[0] != "doc_control"
    ]
    assert third_party == []


def test_cli_version_prints_api_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    """`doc-control version` prints an envelope with the API version."""
    assert main(["version"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["api"] == "1.0"
    assert envelope["ok"] is True
    assert envelope["result"]["api"] == "1.0"


def test_core_dependencies_stay_empty_with_package_group() -> None:
    """The runtime has no dependencies; the freezer lives in the `package` group only."""
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    assert meta["project"]["dependencies"] == []
    groups: dict[str, list[object]] = meta["dependency-groups"]
    extras: dict[str, list[object]] = meta["project"].get("optional-dependencies", {})

    def freezers(reqs: list[object]) -> list[str]:
        return [r for r in reqs if isinstance(r, str) and r.lower().startswith("pyinstaller")]

    assert freezers(groups.get("package", []))
    elsewhere = [r for name, reqs in groups.items() if name != "package" for r in freezers(reqs)]
    elsewhere += [r for reqs in extras.values() for r in freezers(reqs)]
    assert elsewhere == []


def test_capabilities_reports_the_build(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`capabilities.build` says whether this is a frozen binary, and which freezer made it."""
    assert main(["server.capabilities"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["build"] == {"frozen": False}
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "C:/bundle", raising=False)
    assert main(["server.capabilities"]) == 0
    build = json.loads(capsys.readouterr().out)["result"]["build"]
    assert build == {"frozen": True, "freezer": "pyinstaller"}
