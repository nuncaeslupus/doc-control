"""`scripts/build_winget.py` writes the winget manifest for a release (PACKAGE T12, D-P8).

A fake release (the `gh release view --json assets,url,publishedAt` shape) and a fake
SHA256SUMS.txt stand in for GitHub, so nothing here touches the network. The manifest
must carry the version from `pyproject.toml`, the per-user Inno Setup installer, and the
installer's URL and SHA-256; when `winget` is on PATH, `winget validate` must accept it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_winget.py"
ISS = ROOT / "packaging" / "installer" / "doc-control.iss"
SHA = "1918552f4bb9468c02a79d7bb2057e8da37d63caef7ab2160d22efdfced57ac3"
OTHER = "6cd6f6db4a6028e4f7070aac4c9d12f3dba3516b5e2e425d3461710a6aae871e"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_winget", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_winget"] = module
    spec.loader.exec_module(module)
    return module


def _version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as f:
        version: str = tomllib.load(f)["project"]["version"]
    return version


def _url(version: str, name: str) -> str:
    return f"https://github.com/nuncaeslupus/doc-control/releases/download/v{version}/{name}"


def _release(version: str) -> dict[str, object]:
    names = [
        f"doc-control-{version}-windows-x64.zip",
        f"doc-control-{version}-windows-x64-setup.exe",
        f"doc-control-{version}-windows-x64.mcpb",
        "SHA256SUMS.txt",
    ]
    return {
        "assets": [{"name": n, "url": _url(version, n)} for n in names],
        "url": f"https://github.com/nuncaeslupus/doc-control/releases/tag/v{version}",
        "publishedAt": "2026-10-15T09:30:00Z",
    }


def _sums(version: str) -> str:
    return (
        f"{OTHER} *doc-control-{version}-windows-x64.zip\n"
        f"{SHA} *doc-control-{version}-windows-x64-setup.exe\n"
    )


def _fields(text: str) -> dict[str, str]:
    """Top-level and installer-entry `key: value` pairs of a generated YAML file."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^(?:- |  )?([A-Za-z0-9]+): (.*)$", line)
        if match:
            out[match.group(1)] = match.group(2).strip("'")
    return out


def _manifests(version: str) -> dict[str, str]:
    files: dict[str, str] = _script().manifests_for_release(
        f"v{version}", _release(version), _sums(version)
    )
    return files


def test_winget_manifest_fields_match_release() -> None:
    version = _version()
    files = _manifests(version)
    ident = "nuncaeslupus.doc-control"
    assert sorted(files) == sorted(
        [f"{ident}.yaml", f"{ident}.installer.yaml", f"{ident}.locale.en-US.yaml"]
    )
    kinds = {"yaml": "version", "installer.yaml": "installer", "locale.en-US.yaml": "defaultLocale"}
    for name, text in files.items():
        fields = _fields(text)
        assert fields["PackageIdentifier"] == ident
        assert fields["PackageVersion"] == version
        assert fields["ManifestType"] == kinds[name[len(ident) + 1 :]]
        major, minor, _ = (int(p) for p in fields["ManifestVersion"].split("."))
        assert (major, minor) >= (1, 6)

    installer = _fields(files[f"{ident}.installer.yaml"])
    assert installer["InstallerType"] == "inno"
    assert installer["Scope"] == "user"
    assert installer["Architecture"] == "x64"
    assert installer["InstallerUrl"] == _url(
        version, f"doc-control-{version}-windows-x64-setup.exe"
    )
    assert installer["InstallerSha256"] == SHA.upper()
    assert installer["ReleaseDate"] == "2026-10-15"
    assert "- doc-control" in files[f"{ident}.installer.yaml"]

    locale = _fields(files[f"{ident}.locale.en-US.yaml"])
    assert locale["License"] == "Apache-2.0"
    assert locale["Publisher"] == "nuncaeslupus"
    assert locale["PackageName"] == "doc-control"
    assert locale["LicenseUrl"].endswith(f"/blob/v{version}/LICENSE")
    assert _fields(files[f"{ident}.yaml"])["DefaultLocale"] == "en-US"


def test_product_code_is_the_inno_uninstall_key() -> None:
    app_id = re.search(r"^AppId=\{(\{[0-9A-F-]{36}\})", ISS.read_text("utf-8"), re.M)
    assert app_id
    assert _script().product_code() == f"{app_id.group(1)}_is1"


def test_parse_sums_reads_text_and_binary_mode() -> None:
    sums = _script().parse_sums(f"{SHA.upper()}  a.exe\n{OTHER} *b c.zip\nnot a line\n")
    assert sums == {"a.exe": SHA, "b c.zip": OTHER}


def test_missing_installer_asset_or_sum_is_an_error() -> None:
    module = _script()
    version = _version()
    release = _release(version)
    release["assets"] = [a for a in release["assets"] if "setup" not in a["name"]]  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="no asset"):
        module.manifests_for_release(f"v{version}", release, _sums(version))
    with pytest.raises(ValueError, match="no line"):
        module.manifests_for_release(f"v{version}", _release(version), f"{OTHER} *x.zip\n")


def test_main_writes_the_files_offline(tmp_path: Path) -> None:
    version = _version()
    (tmp_path / "release.json").write_text(json.dumps(_release(version)), encoding="utf-8")
    (tmp_path / "SHA256SUMS.txt").write_text(_sums(version), encoding="utf-8")
    out = tmp_path / "out"
    code = _script().main(
        [
            "--tag",
            f"v{version}",
            "--release-json",
            str(tmp_path / "release.json"),
            "--sums",
            str(tmp_path / "SHA256SUMS.txt"),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(_manifests(version))


@pytest.mark.skipif(shutil.which("winget") is None, reason="winget is not installed")
def test_winget_manifest_validates(tmp_path: Path) -> None:
    _script().write_manifests(_manifests(_version()), tmp_path)
    done = subprocess.run(
        ["winget", "validate", "--manifest", str(tmp_path), "--disable-interactivity"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
