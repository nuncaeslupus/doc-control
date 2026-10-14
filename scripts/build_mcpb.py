"""Build the Claude Desktop bundle (`.mcpb`) around the frozen Windows build (PACKAGE T6).

Usage::

    python scripts/build_mcpb.py --dist build-out/doc-control \\
        --out build-out/doc-control-X.Y.Z-windows-x64.mcpb
    python scripts/build_mcpb.py --manifest-only            # the filled manifest, on stdout

The manifest is ``packaging/mcpb/manifest.json`` with two fields filled at build:
``version`` from ``pyproject.toml`` and ``tools`` from the operation registry (the MCP
tool name of every operation, its summary as the description), so it always equals the
server's ``tools/list``. The bundle is a zip of ``manifest.json``, ``icon.png`` and the
one-folder build under ``server/<folder name>/``; the manifest's ``entry_point`` must be
one of its files. ``mcpb validate`` (pinned, in CI) is the arbiter of the schema.

Standard library plus ``doc_control`` itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

from doc_control.mcp import tool_name
from doc_control.registry import load_operations

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "packaging" / "mcpb"


def project_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    with pyproject.open("rb") as f:
        version: str = tomllib.load(f)["project"]["version"]
    return version


def registry_tools() -> list[dict[str, str]]:
    """One ``{name, description}`` per operation, sorted like ``tools/list``."""
    ops = sorted(load_operations().values(), key=lambda op: op.name)
    return [{"name": tool_name(op.name), "description": op.summary} for op in ops]


def build_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(
        (TEMPLATE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    manifest["version"] = project_version()
    manifest["tools"] = registry_tools()
    return manifest


def write_bundle(dist: Path, out: Path, manifest: dict[str, Any]) -> None:
    entry = manifest["server"]["entry_point"]
    prefix = f"server/{dist.name}/"
    files = sorted(p for p in dist.rglob("*") if p.is_file())
    members = {prefix + p.relative_to(dist).as_posix(): p for p in files}
    if entry not in members:
        raise SystemExit(f"{dist} has no {entry.removeprefix(prefix)}: the manifest names {entry}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        bundle.write(TEMPLATE_DIR / manifest["icon"], manifest["icon"])
        for name, path in members.items():
            bundle.write(path, name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, help="the one-folder build (build-out/doc-control)")
    parser.add_argument("--out", type=Path, help="the .mcpb to write")
    parser.add_argument(
        "--manifest-only", action="store_true", help="print the filled manifest and stop"
    )
    args = parser.parse_args(argv)
    manifest = build_manifest()
    if args.manifest_only:
        print(json.dumps(manifest, indent=2))
        return 0
    if args.dist is None or args.out is None:
        parser.error("--dist and --out are required unless --manifest-only")
    write_bundle(args.dist, args.out, manifest)
    print(f"{args.out}: {len(manifest['tools'])} tools, version {manifest['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
