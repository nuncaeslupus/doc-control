"""Package-level guarantees: stdlib only, version envelope."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from doc_control.cli import main


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
