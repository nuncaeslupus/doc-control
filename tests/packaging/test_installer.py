"""The Inno Setup installer script and the CI steps that build and smoke it (PACKAGE T7).

Inno Setup only runs on Windows, so these read the script and the workflow as text: the
version comes from `pyproject.toml`, the install is per user, and uninstalling takes the
`PATH` entry back out. The install itself is exercised by `package.yml` on windows-latest.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISS = ROOT / "packaging" / "installer" / "doc-control.iss"
APP_VERSION = ROOT / "packaging" / "installer" / "app_version.py"
PACKAGE_YML = ROOT / ".github" / "workflows" / "package.yml"


def _section(name: str) -> str:
    """The body of one `[Name]` section of the script, up to the next section header."""
    text = ISS.read_text("utf-8")
    match = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[\w+\]\s*$|\Z)", text, re.M | re.S)
    assert match, f"no [{name}] section in {ISS.name}"
    return match.group(1)


def _setup() -> dict[str, str]:
    """The `[Setup]` directives, keyed in lower case (Inno Setup ignores their case)."""
    found: dict[str, str] = {}
    for line in _section("Setup").splitlines():
        line = line.strip()
        if line and not line.startswith(";") and "=" in line:
            key, value = line.split("=", 1)
            found[key.strip().lower()] = value.strip()
    return found


def test_iss_version_define_matches_pyproject() -> None:
    with (ROOT / "pyproject.toml").open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    printed = subprocess.run(
        [sys.executable, str(APP_VERSION)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert printed == version
    script = ISS.read_text("utf-8")
    assert _setup()["appversion"] == "{#AppVersion}"
    assert re.search(r"^#ifndef AppVersion\s*$\s*#error", script, re.M), "no guard for the define"
    assert version not in script, "the script must not hard-code the version"
    workflow = PACKAGE_YML.read_text("utf-8")
    assert "packaging/installer/app_version.py" in workflow
    assert re.search(r'"/DAppVersion=\$version"', workflow), "ISCC must get /DAppVersion="


def test_iss_installs_per_user_without_admin() -> None:
    setup = _setup()
    assert setup["privilegesrequired"] == "lowest"
    assert "privilegesrequiredoverridesallowed" not in setup, "no all-users override"
    assert setup["defaultdirname"] == r"{localappdata}\Programs\doc-control"
    assert re.fullmatch(r"\{\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}", setup["appid"])
    assert "HKLM" not in ISS.read_text("utf-8").upper()


def test_iss_uninstall_removes_user_path_entry() -> None:
    assert _setup()["changesenvironment"] == "yes"
    code = _section("Code")
    assert "RegWriteExpandStringValue(HKCU, EnvKey, 'Path'" in code
    install = re.search(r"procedure CurStepChanged.*?\bend;\s*$", code, re.S | re.M)
    uninstall = re.search(r"procedure CurUninstallStepChanged.*?\bend;\s*$", code, re.S | re.M)
    assert install and "AddToUserPath(ExpandConstant('{app}'))" in install.group(0)
    assert uninstall and "usPostUninstall" in uninstall.group(0)
    assert "RemoveFromUserPath(ExpandConstant('{app}'))" in uninstall.group(0)
    workflow = PACKAGE_YML.read_text("utf-8")
    assert "[Environment]::GetEnvironmentVariable('Path','User')" in workflow
    assert "installer_smoke_failures ==" in workflow


def test_iss_shows_claude_desktop_snippet_and_ci_pins_inno() -> None:
    code = _section("Code")
    assert '{"mcpServers": {"doc-control": {"command": "' in code
    assert "doc-control.exe" in code and '"args": ["mcp"]' in code
    workflow = PACKAGE_YML.read_text("utf-8")
    pinned = re.search(r'INNO_VERSION: "(\d+\.\d+\.\d+)"', workflow)
    assert pinned, "Inno Setup must be pinned to an exact version"
    assert "choco install innosetup --version $env:INNO_VERSION" in workflow
