"""Generate the winget manifest for a release's installer (PACKAGE T12, D-P8).

Usage::

    python scripts/build_winget.py --tag vX.Y.Z
    python scripts/build_winget.py --tag vX.Y.Z --release-json r.json --sums SHA256SUMS.txt

Writes the multi-file manifest (``version``, ``installer`` and ``defaultLocale``) for
``nuncaeslupus.doc-control`` to ``packaging/winget/out/<version>/`` (``--out`` moves it).
The installer URL comes from the release's assets (``gh release view <tag> --json
assets,url,publishedAt``) and its SHA-256 from the release's ``SHA256SUMS.txt`` (``gh
release download``); ``--release-json`` and ``--sums`` read both from files instead, so
nothing touches the network. The installer is the per-user Inno Setup build of
``packaging/installer/doc-control.iss``, whose ``AppId`` gives the ``ProductCode``.

``winget validate --manifest <dir>`` checks the output. Submitting it is a pull request to
``microsoft/winget-pkgs`` under ``manifests/n/nuncaeslupus/doc-control/<version>/``, made
by hand with the owner's agreement.

Standard library only; needs the ``gh`` CLI unless both files are given.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "packaging" / "installer" / "doc-control.iss"
OUT_DIR = ROOT / "packaging" / "winget" / "out"

IDENTIFIER = "nuncaeslupus.doc-control"
MANIFEST_VERSION = "1.9.0"
LOCALE = "en-US"
REPO_URL = "https://github.com/nuncaeslupus/doc-control"
PUBLISHER = "nuncaeslupus"
PUBLISHER_URL = "https://github.com/nuncaeslupus"
TAGS = ["docx", "word", "mcp", "claude", "documents", "cli"]

_SCHEMA = "https://aka.ms/winget-manifest.{kind}.{version}.schema.json"


def installer_name(version: str) -> str:
    return f"doc-control-{version}-windows-x64-setup.exe"


def product_code(iss: Path = ISS) -> str:
    """The Inno Setup uninstall key, ``{AppId}_is1``, from the installer script."""
    match = re.search(r"^AppId=\{(\{[0-9A-Fa-f-]{36}\})\s*$", iss.read_text("utf-8"), re.M)
    if not match:
        raise ValueError(f"no AppId line in {iss}")
    return f"{match.group(1)}_is1"


def project_metadata(pyproject: Path = ROOT / "pyproject.toml") -> dict[str, Any]:
    with pyproject.open("rb") as f:
        project: dict[str, Any] = tomllib.load(f)["project"]
    return project


def parse_sums(text: str) -> dict[str, str]:
    """``sha256sum`` output (text or ``*binary`` mode) as {file name: lowercase hex}."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})[ \t]+\*?(\S.*?)\s*$", line)
        if match:
            sums[match.group(2)] = match.group(1).lower()
    return sums


def installer_asset(release: dict[str, Any], version: str) -> str:
    """The download URL of the release's installer asset."""
    name = installer_name(version)
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == name:
            url = str(asset.get("url") or asset.get("browser_download_url") or "")
            if url.startswith("https://"):
                return url
            raise ValueError(f"asset {name} has no https download URL")
    raise ValueError(f"release has no asset {name}")


def _q(value: str) -> str:
    """A YAML single-quoted scalar."""
    return "'" + value.replace("'", "''") + "'"


def _document(kind: str, fields: list[str]) -> str:
    header = [
        "# Created by scripts/build_winget.py (doc-control PACKAGE T12)",
        f"# yaml-language-server: $schema={_SCHEMA.format(kind=kind, version=MANIFEST_VERSION)}",
        "",
    ]
    return "\n".join([*header, *fields, ""])


def build_manifests(
    version: str,
    url: str,
    sha256: str,
    *,
    release_date: str | None = None,
    release_url: str | None = None,
    project: dict[str, Any] | None = None,
    code: str | None = None,
) -> dict[str, str]:
    """{file name: YAML text} for the three manifest files of one version."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise ValueError(f"not a SHA-256: {sha256!r}")
    project = project if project is not None else project_metadata()
    code = code if code is not None else product_code()
    ident = [f"PackageIdentifier: {IDENTIFIER}", f"PackageVersion: {_q(version)}"]
    tail = [f"ManifestVersion: {MANIFEST_VERSION}"]

    version_doc = _document(
        "version",
        [*ident, f"DefaultLocale: {LOCALE}", "ManifestType: version", *tail],
    )

    installer_fields = [
        *ident,
        "InstallerType: inno",
        "Scope: user",
        "InstallModes:",
        "- interactive",
        "- silent",
        "- silentWithProgress",
        "UpgradeBehavior: install",
        "Commands:",
        "- doc-control",
        f"ProductCode: {_q(code)}",
    ]
    if release_date:
        installer_fields.append(f"ReleaseDate: {release_date[:10]}")
    installer_fields += [
        "Installers:",
        "- Architecture: x64",
        f"  InstallerUrl: {url}",
        f"  InstallerSha256: {sha256.upper()}",
        "ManifestType: installer",
        *tail,
    ]
    installer_doc = _document("installer", installer_fields)

    locale_fields = [
        *ident,
        f"PackageLocale: {LOCALE}",
        f"Publisher: {PUBLISHER}",
        f"PublisherUrl: {PUBLISHER_URL}",
        f"PublisherSupportUrl: {REPO_URL}/issues",
        f"PackageName: {project['name']}",
        f"PackageUrl: {REPO_URL}",
        f"License: {project['license']}",
        f"LicenseUrl: {REPO_URL}/blob/v{version}/LICENSE",
        f"ShortDescription: {_q(str(project['description']))}",
        "Moniker: doc-control",
        "Tags:",
        *(f"- {tag}" for tag in TAGS),
    ]
    if release_url:
        locale_fields.append(f"ReleaseNotesUrl: {release_url}")
    locale_fields += ["ManifestType: defaultLocale", *tail]
    locale_doc = _document("defaultLocale", locale_fields)

    return {
        f"{IDENTIFIER}.yaml": version_doc,
        f"{IDENTIFIER}.installer.yaml": installer_doc,
        f"{IDENTIFIER}.locale.{LOCALE}.yaml": locale_doc,
    }


def manifests_for_release(tag: str, release: dict[str, Any], sums_text: str) -> dict[str, str]:
    version = tag[1:] if tag.startswith("v") else tag
    url = installer_asset(release, version)
    sha = parse_sums(sums_text).get(installer_name(version))
    if sha is None:
        raise ValueError(f"SHA256SUMS.txt has no line for {installer_name(version)}")
    return build_manifests(
        version,
        url,
        sha,
        release_date=release.get("publishedAt") or None,
        release_url=release.get("url") or None,
    )


def write_manifests(files: dict[str, str], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files.items():
        path = out / name
        path.write_bytes(text.encode("utf-8"))
        written.append(path)
    return written


def _gh(*args: str) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"gh {' '.join(args)} failed")
    return done.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--tag", required=True, help="release tag, vX.Y.Z")
    parser.add_argument(
        "--release-json", help="read `gh release view --json assets,url,publishedAt` from here"
    )
    parser.add_argument("--sums", help="read the release's SHA256SUMS.txt from here")
    parser.add_argument("--out", help="output folder (default packaging/winget/out/<version>)")
    args = parser.parse_args(argv)
    version = args.tag[1:] if args.tag.startswith("v") else args.tag
    try:
        if args.release_json:
            raw = Path(args.release_json).read_text(encoding="utf-8")
        else:
            raw = _gh("release", "view", args.tag, "--json", "assets,url,publishedAt")
        if args.sums:
            sums_text = Path(args.sums).read_text(encoding="utf-8")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                _gh("release", "download", args.tag, "-p", "SHA256SUMS.txt", "-D", tmp)
                sums_text = (Path(tmp) / "SHA256SUMS.txt").read_text(encoding="utf-8")
        files = manifests_for_release(args.tag, json.loads(raw), sums_text)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"build_winget: {exc}", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else OUT_DIR / version
    for path in write_manifests(files, out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
