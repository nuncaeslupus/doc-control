"""`scripts/build_mcpb.py` builds the Claude Desktop bundle (PACKAGE T6).

The manifest's `tools` come from the registry, so they must equal what the server answers
to `tools/list`; its `version` comes from `pyproject.toml`. A bundle built over a fake
one-folder build holds `manifest.json` at its root and the exe where the manifest says.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from doc_control.mcp import handle
from doc_control.registry import load_operations

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_mcpb.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_mcpb", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_mcpb"] = module
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = _script().build_manifest()
    return manifest


def test_manifest_tools_equal_mcp_tools_list() -> None:
    reply = handle(load_operations(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply is not None
    served = sorted(tool["name"] for tool in reply["result"]["tools"])
    tools = _manifest()["tools"]
    assert sorted(tool["name"] for tool in tools) == served
    assert all(isinstance(t["description"], str) and t["description"] for t in tools)


def test_manifest_version_matches_pyproject() -> None:
    with (ROOT / "pyproject.toml").open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    assert _manifest()["version"] == version


def test_manifest_is_a_windows_binary_server() -> None:
    manifest = _manifest()
    server = manifest["server"]
    assert server["type"] == "binary"
    assert server["mcp_config"]["args"] == ["mcp"]
    assert server["mcp_config"]["command"] == "${__dirname}/" + server["entry_point"]
    assert manifest["compatibility"]["platforms"] == ["win32"]
    assert (ROOT / "packaging" / "mcpb" / manifest["icon"]).is_file()


def test_bundle_over_a_fake_dist_has_manifest_and_exe(tmp_path: Path) -> None:
    dist = tmp_path / "build-out" / "doc-control"
    (dist / "_internal").mkdir(parents=True)
    (dist / "doc-control.exe").write_bytes(b"MZ fake")
    (dist / "_internal" / "python312.dll").write_bytes(b"dll")
    out = tmp_path / "doc-control.mcpb"

    assert _script().main(["--dist", str(dist), "--out", str(out)]) == 0

    with zipfile.ZipFile(out) as bundle:
        names = set(bundle.namelist())
        manifest = json.loads(bundle.read("manifest.json"))
        entry = manifest["server"]["entry_point"]
        assert entry in names
        assert bundle.read(entry) == b"MZ fake"
        assert "server/doc-control/_internal/python312.dll" in names
        assert manifest["icon"] in names


def test_bundle_refuses_a_dist_without_the_exe(tmp_path: Path) -> None:
    dist = tmp_path / "doc-control"
    dist.mkdir()
    with pytest.raises(SystemExit):
        _script().main(["--dist", str(dist), "--out", str(tmp_path / "x.mcpb")])


def test_manifest_only_prints_the_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    assert _script().main(["--manifest-only"]) == 0
    assert json.loads(capsys.readouterr().out) == _manifest()
