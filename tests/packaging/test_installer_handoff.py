"""The installer hands the `.mcpb` to Claude Desktop and can drop an old uvx entry (PACKAGE T14).

Gate `installer_handoff_failures == 0` (D-P9). Inno Setup only runs on Windows and the handoff
itself opens Claude Desktop, so the script and the workflow are read as text here: the
`.iss` installs the `.mcpb` and offers the handoff, unchecked, on its finish page.
`package.yml`'s installer smoke checks the installed bundle and the frozen remover on
windows-latest. `scripts/remove_uvx_entry.py` runs here on fake configs under `tmp_path`;
nothing reads or writes a real Claude Desktop config.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ISS = ROOT / "packaging" / "installer" / "doc-control.iss"
ENTRY = ROOT / "packaging" / "entry.py"
SPEC = ROOT / "packaging" / "doc-control.spec"
PACKAGE_YML = ROOT / ".github" / "workflows" / "package.yml"
SCRIPT = ROOT / "scripts" / "remove_uvx_entry.py"
LAUNCHER = r"{localappdata}\Microsoft\WindowsApps\claude-desktop.exe"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("remove_uvx_entry", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rm = _script()


def _section(name: str) -> str:
    text = ISS.read_text("utf-8")
    match = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[\w+\]\s*$|\Z)", text, re.M | re.S)
    assert match, f"no [{name}] section in {ISS.name}"
    return match.group(1)


def _entries(name: str) -> list[dict[str, str]]:
    """The `Key: value; Key: value` lines of one section, keys in lower case."""
    found: list[dict[str, str]] = []
    for line in _section(name).splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        entry: dict[str, str] = {}
        for part in re.findall(r'(\w+):\s*("(?:[^"]|"")*"|[^;]*)', line):
            key, value = part
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1].replace('""', '"')
            entry[key.lower()] = value
        found.append(entry)
    return found


def _flags(entry: dict[str, str]) -> set[str]:
    return set(entry.get("flags", "").lower().split())


def test_iss_ships_the_mcpb_and_offers_handoff() -> None:
    text = ISS.read_text("utf-8")
    define = re.search(r'^#define McpbName "doc-control-" \+ AppVersion \+ "(.*)"\s*$', text, re.M)
    assert define and define.group(1) == "-windows-x64.mcpb", "the T8 asset name"
    assert f'#define ClaudeLauncher "{LAUNCHER}"' in text

    files = _entries("Files")
    mcpb = [f for f in files if f["source"] == r"..\..\build-out\{#McpbName}"]
    assert mcpb and mcpb[0]["destdir"] == "{app}"

    runs = _entries("Run")
    for run in runs:
        flags = _flags(run)
        assert {"postinstall", "unchecked", "skipifsilent"} <= flags, run
    launch, explorer, remover = runs
    assert launch["filename"] == "{#ClaudeLauncher}"
    assert launch["parameters"] == '"{app}\\{#McpbName}"'
    assert launch["check"] == "HasClaudeLauncher"
    assert launch["description"].startswith("Add to Claude Desktop")
    assert explorer["filename"] == r"{win}\explorer.exe"
    assert explorer["parameters"] == '/select,"{app}\\{#McpbName}"'
    assert explorer["check"] == "not HasClaudeLauncher", "the fallback when the alias is missing"
    assert explorer["description"].startswith("Add to Claude Desktop")
    assert "drag it onto Claude Desktop > Settings > Extensions" in explorer["description"]
    assert remover["filename"] == r"{app}\doc-control.exe"
    assert remover["parameters"] == "--remove-uvx-entry --pause"
    assert remover["check"] == "HasUvxEntry"
    assert ".bak" in remover["description"] and "restart Claude Desktop" in remover["description"]

    code = _section("Code")
    assert "FileExists(ExpandConstant('{#ClaudeLauncher}'))" in code
    assert r"{userappdata}\Claude\claude_desktop_config.json" in code
    assert r"\LocalCache\Roaming\Claude\claude_desktop_config.json" in code
    # The installer itself only reads Claude Desktop's config; the remover writes it.
    assert "SaveStringToFile" not in code and "DeleteFile" not in code


def test_frozen_exe_dispatches_the_remover() -> None:
    entry = ENTRY.read_text("utf-8")
    assert 'REMOVE_UVX_ENTRY = "--remove-uvx-entry"' in entry
    assert entry.index("multiprocessing.freeze_support()") < entry.index("REMOVE_UVX_ENTRY]")
    assert entry.index("REMOVE_UVX_ENTRY]") < entry.index("sys.exit(main())")
    spec = SPEC.read_text("utf-8")
    assert 'INSTALLER_HELPERS = ["remove_uvx_entry"]' in spec
    assert "pathex=[str(SRC), str(SCRIPTS)]" in spec
    assert "hiddenimports=PYWIN32 + OWN + INSTALLER_HELPERS" in spec


def test_entry_runs_the_remover_unfrozen(tmp_path: Path) -> None:
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps(_fake_config()), encoding="utf-8")
    before = config.read_bytes()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "scripts"), str(ROOT / "src")]))
    done = subprocess.run(
        [sys.executable, str(ENTRY), "--remove-uvx-entry", "--dry-run", "--config", str(config)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "Would remove" in done.stdout and '"doc-control"' in done.stdout
    assert config.read_bytes() == before


def test_package_workflow_builds_the_mcpb_before_the_installer_and_smokes_handoff() -> None:
    workflow = PACKAGE_YML.read_text("utf-8")
    built = workflow.index("scripts/build_mcpb.py --dist")
    assert built < workflow.index("packaging/installer/doc-control.iss")
    assert "installer_handoff_failures == $handoff" in workflow
    assert "--remove-uvx-entry --dry-run --config $fake" in workflow
    assert "Get-FileHash $installedMcpb" in workflow


def _fake_config() -> dict[str, Any]:
    return {
        "mcpServers": {
            "doc-control": {"command": "uvx", "args": ["doc-control", "mcp"]},
            "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-fs"]},
            "doc-control-exe": {"command": r"C:\Tools\doc-control.exe", "args": ["mcp"]},
        },
        "preferences": {"menuBarEnabled": False, "nota": "año"},
    }


def _answers(*replies: str) -> tuple[Callable[[str], str], list[str]]:
    prompts: list[str] = []
    queue = list(replies)

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return queue.pop(0)

    return ask, prompts


def test_uvx_entry_removal_backs_up_and_asks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps(_fake_config(), indent=4, ensure_ascii=False), encoding="utf-8")
    original = config.read_bytes()
    backup = tmp_path / "claude_desktop_config.json.bak"

    # --dry-run: reports, never asks, changes nothing.
    ask, prompts = _answers()
    assert rm.main(["--config", str(config), "--dry-run"], ask=ask) == 0
    assert prompts == [] and config.read_bytes() == original and not backup.exists()
    assert "Would remove" in capsys.readouterr().out

    # Asked and declined: nothing changes.
    ask, prompts = _answers("n")
    assert rm.main(["--config", str(config)], ask=ask) == 0
    assert len(prompts) == 1 and "[y/N]" in prompts[0]
    assert config.read_bytes() == original and not backup.exists()

    # Asked and accepted: a byte-for-byte backup, and only the uvx entry gone.
    ask, prompts = _answers("y")
    assert rm.main(["--config", str(config)], ask=ask) == 0
    assert len(prompts) == 1
    assert backup.read_bytes() == original
    after = json.loads(config.read_text("utf-8"))
    expected = _fake_config()
    del expected["mcpServers"]["doc-control"]
    assert after == expected
    out = capsys.readouterr().out
    assert "Restart Claude Desktop" in out and str(backup) in out

    # Run again: nothing left to remove, no second backup, no restart notice.
    ask, prompts = _answers()
    assert rm.main(["--config", str(config), "--yes"], ask=ask) == 0
    assert prompts == [] and not (tmp_path / "claude_desktop_config.json.bak.1").exists()
    assert "Restart" not in capsys.readouterr().out


def test_removal_keeps_an_older_backup(tmp_path: Path) -> None:
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps(_fake_config()), encoding="utf-8")
    older = tmp_path / "claude_desktop_config.json.bak"
    older.write_text("older", encoding="utf-8")
    assert rm.main(["--config", str(config), "--yes"], ask=_answers()[0]) == 0
    assert older.read_text("utf-8") == "older"
    assert (tmp_path / "claude_desktop_config.json.bak.1").is_file()


def test_unreadable_config_is_left_alone_and_fails(tmp_path: Path) -> None:
    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{not json", encoding="utf-8")
    assert rm.main(["--config", str(config), "--yes"], ask=_answers()[0]) == 1
    assert config.read_text("utf-8") == "{not json"
    assert not list(tmp_path.glob("*.bak*"))


def test_pause_waits_for_enter(tmp_path: Path) -> None:
    config = tmp_path / "claude_desktop_config.json"
    config.write_text('{"mcpServers": {}}', encoding="utf-8")
    ask, prompts = _answers("")
    assert rm.main(["--config", str(config), "--pause"], ask=ask) == 0
    assert prompts == ["Press Enter to close."]


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ({"command": "uvx", "args": ["doc-control", "mcp"]}, True),
        ({"command": "uvx.exe", "args": ["doc-control==0.6.4", "mcp"]}, True),
        ({"command": r"C:\Users\x\.local\bin\uvx.exe", "args": ["doc-control[live]", "mcp"]}, True),
        ({"command": "uvx", "args": ["--from", "doc-control@latest", "doc-control", "mcp"]}, True),
        ({"command": "uv", "args": ["tool", "run", "doc-control", "mcp"]}, True),
        ({"command": "uvx", "args": ["other-server"]}, False),
        ({"command": "uvx", "args": ["doc-control-extra"]}, False),
        ({"command": "uv", "args": ["run", "doc-control", "mcp"]}, False),
        ({"command": "doc-control.exe", "args": ["mcp"]}, False),
        ({"command": "uvx"}, False),
        ({"command": "uvx", "args": "doc-control"}, False),
        ("uvx doc-control", False),
    ],
)
def test_matcher_finds_only_uvx_doc_control(server: Any, expected: bool) -> None:
    assert rm.is_uvx_doc_control(server) is expected


def test_config_candidates_cover_appdata_and_the_store_build(tmp_path: Path) -> None:
    roaming = tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"
    store = (
        tmp_path / "Local" / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    ) / "claude_desktop_config.json"
    (tmp_path / "Local" / "Packages" / "Other_1").mkdir(parents=True)
    env = {"APPDATA": str(tmp_path / "Roaming"), "LOCALAPPDATA": str(tmp_path / "Local")}
    assert rm.config_candidates(env) == []
    for path in (roaming, store):
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
    assert rm.config_candidates(env) == [roaming, store]
    assert rm.config_candidates({}) == []
