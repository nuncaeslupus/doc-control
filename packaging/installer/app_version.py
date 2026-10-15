"""Print the version in `pyproject.toml`; package.yml passes it to ISCC as `/DAppVersion=`."""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def app_version(pyproject: Path = PYPROJECT) -> str:
    with pyproject.open("rb") as f:
        version: str = tomllib.load(f)["project"]["version"]
    return version


if __name__ == "__main__":
    print(app_version())
