"""check_release_assets.py counts missing assets and assets without a checksum line."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_release_assets.py"
SHA = "a" * 64

NAMES = [
    "doc-control-1.2.3-windows-x64.zip",
    "doc-control-1.2.3-windows-x64-setup.exe",
    "doc-control-1.2.3-windows-x64.mcpb",
    "SHA256SUMS.txt",
]


def _run(
    tmp_path: Path, assets: list[str], sum_lines: list[str]
) -> subprocess.CompletedProcess[str]:
    body = "Notes\n\n## Checksums\n\n" + "\n".join(f"{SHA}  {n}" for n in sum_lines) + "\n"
    path = tmp_path / "release.json"
    path.write_text(json.dumps({"assets": [{"name": n} for n in assets], "body": body}))
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v1.2.3", "--json-file", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_complete_release_is_clean(tmp_path: Path) -> None:
    done = _run(tmp_path, NAMES, NAMES[:3])
    assert "release_artifacts_missing == 0" in done.stdout
    assert done.returncode == 0


def test_check_release_assets_flags_missing_and_unchecksummed(tmp_path: Path) -> None:
    # No .mcpb asset (1) and no checksum line for the installer (1).
    done = _run(tmp_path, [n for n in NAMES if not n.endswith(".mcpb")], [NAMES[0], NAMES[2]])
    assert "release_artifacts_missing == 2" in done.stdout
    assert done.returncode == 1


def test_missing_asset_counts_once(tmp_path: Path) -> None:
    done = _run(tmp_path, NAMES[:1], [])
    # three assets missing, the zip has no checksum line: 3 + 1
    assert "release_artifacts_missing == 4" in done.stdout
