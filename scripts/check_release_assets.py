"""Check a GitHub release carries the Windows assets, each with a checksum (PACKAGE T8).

Usage::

    python scripts/check_release_assets.py --tag vX.Y.Z

Reads ``gh release view <tag> --json assets,body`` (or ``--json-file`` holding that output)
and prints ``release_artifacts_missing == N``: one for every expected asset that is not
attached, plus one for every attached zip, installer or ``.mcpb`` whose ``sha256  name``
line is absent from the release notes. Exit 0 iff N == 0.

Standard library only; needs the ``gh`` CLI unless ``--json-file`` is given.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def expected_assets(version: str) -> tuple[list[str], list[str]]:
    """Return (checksummed assets, all expected assets) for a version."""
    base = f"doc-control-{version}-windows-x64"
    summed = [f"{base}.zip", f"{base}-setup.exe", f"{base}.mcpb"]
    return summed, [*summed, "SHA256SUMS.txt"]


def count_missing(tag: str, release: dict[str, object]) -> int:
    version = tag[1:] if tag.startswith("v") else tag
    summed, everything = expected_assets(version)
    attached = {
        str(a.get("name"))
        for a in release.get("assets", [])
        if isinstance(a, dict)  # type: ignore[attr-defined]
    }
    body = str(release.get("body") or "")
    missing = sum(1 for name in everything if name not in attached)
    for name in summed:
        if name in attached and not re.search(
            rf"^[0-9a-fA-F]{{64}}[ \t]+\*?{re.escape(name)}[ \t]*$", body, re.M
        ):
            missing += 1
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag, vX.Y.Z")
    parser.add_argument("--json-file", help="read `gh release view --json assets,body` from here")
    args = parser.parse_args(argv)
    if args.json_file:
        raw = Path(args.json_file).read_text(encoding="utf-8")
    else:
        done = subprocess.run(
            ["gh", "release", "view", args.tag, "--json", "assets,body"],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            print(done.stderr.strip(), file=sys.stderr)
            return 2
        raw = done.stdout
    missing = count_missing(args.tag, json.loads(raw))
    print(f"release_artifacts_missing == {missing}")
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
