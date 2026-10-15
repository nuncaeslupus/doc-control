"""Remove an old ``uvx doc-control`` server from Claude Desktop's config (PACKAGE T14, D-P9).

Usage::

    python scripts/remove_uvx_entry.py                 # find the config, ask, then remove
    python scripts/remove_uvx_entry.py --dry-run       # say what it would remove; change nothing
    python scripts/remove_uvx_entry.py --config PATH [--yes]

Someone who used doc-control through ``uvx`` before installing the Windows build has a
``claude_desktop_config.json`` server whose command is ``uvx`` (or ``uv tool run``) with a
``doc-control`` argument. Once the ``.mcpb`` extension is installed that entry is a second,
older copy of the same tools. This finds such entries, shows them, asks, writes a backup
(``claude_desktop_config.json.bak``, never overwriting an older one) and removes only those
entries; every other server and setting is kept. Claude Desktop reads the file at start, so
it then has to be restarted.

Without ``--config`` it looks where Claude Desktop keeps the file: ``%APPDATA%\\Claude`` and,
for the Microsoft Store build, ``%LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude``.

The installer runs this as ``doc-control.exe --remove-uvx-entry --pause`` from its finish page
(``packaging/entry.py`` dispatches to it), so a machine without Python can use it too.

Exit 0 when nothing failed (including when there was nothing to remove), 1 otherwise.
Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

CONFIG_NAME = "claude_desktop_config.json"
RESTART = (
    "Restart Claude Desktop (quit it from its tray icon, then open it again) "
    "so it reads the changed config."
)
# A requirement naming the doc-control package: `doc-control`, `doc-control==0.6.4`,
# `doc-control[live]`, `doc-control@latest`, `doc-control>=0.6`.
PACKAGE = re.compile(r"doc-control(\[[^\]]*\])?\s*([=<>!~@].*)?", re.IGNORECASE)


def _command_name(command: str) -> str:
    name = re.split(r"[\\/]", command.strip().strip('"'))[-1].lower()
    return name.removesuffix(".exe")


def is_uvx_doc_control(server: Any) -> bool:
    """Whether one ``mcpServers`` value runs doc-control through uvx (or ``uv tool run``)."""
    if not isinstance(server, dict):
        return False
    command, args = server.get("command"), server.get("args", [])
    if not isinstance(command, str) or not isinstance(args, list):
        return False
    if not all(isinstance(arg, str) for arg in args):
        return False
    name = _command_name(command)
    if name == "uv" and args[:2] == ["tool", "run"]:
        args = args[2:]
    elif name != "uvx":
        return False
    return any(PACKAGE.fullmatch(arg.strip()) for arg in args)


def uvx_entries(config: Any) -> list[str]:
    """The names of the uvx doc-control servers in a parsed config, in file order."""
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        return []
    return [name for name, server in servers.items() if is_uvx_doc_control(server)]


def config_candidates(env: Mapping[str, str]) -> list[Path]:
    """The Claude Desktop config files that exist, from the given environment."""
    found: list[Path] = []
    appdata = env.get("APPDATA")
    if appdata:
        found.append(Path(appdata) / "Claude" / CONFIG_NAME)
    localappdata = env.get("LOCALAPPDATA")
    if localappdata:
        packages = Path(localappdata) / "Packages"
        if packages.is_dir():
            for package in sorted(packages.glob("Claude_*")):
                found.append(package / "LocalCache" / "Roaming" / "Claude" / CONFIG_NAME)
    return [path for path in found if path.is_file()]


def backup_path(config: Path) -> Path:
    """``<config>.bak``, or ``.bak.N`` with the first free N: an older backup is kept."""
    candidate = config.with_name(config.name + ".bak")
    n = 1
    while candidate.exists():
        candidate = config.with_name(f"{config.name}.bak.{n}")
        n += 1
    return candidate


def remove_entries(config_path: Path, raw: bytes, config: dict[str, Any], names: list[str]) -> Path:
    """Write the backup of ``raw``, then the config without ``names``; return the backup."""
    backup = backup_path(config_path)
    backup.write_bytes(raw)
    servers = config["mcpServers"]
    config["mcpServers"] = {k: v for k, v in servers.items() if k not in names}
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(config_path)
    return backup


def _ask_yes(ask: Callable[[str], str], prompt: str) -> bool:
    try:
        answer = ask(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes", "s", "si", "sí"}


def process(
    config_path: Path, *, dry_run: bool, yes: bool, ask: Callable[[str], str]
) -> tuple[bool, bool]:
    """Handle one config file; return (ok, changed)."""
    try:
        raw = config_path.read_bytes()
        config = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"Could not read {config_path}: {exc}")
        return False, False
    names = uvx_entries(config)
    if not names:
        print(f"No uvx doc-control entry in {config_path}.")
        return True, False
    print(f"In {config_path}:")
    for name in names:
        server = config["mcpServers"][name]
        print(f'  "{name}": {server.get("command")} {" ".join(server.get("args", []))}')
    if dry_run:
        print("Would remove the entries above (dry run: nothing changed).")
        return True, False
    if not yes:
        # Claude Desktop rewrites its config on exit: an edit made while it runs can be lost.
        print("Quit Claude Desktop first (its tray icon > Quit) if it is running.")
    if not yes and not _ask_yes(ask, "Remove them? A backup is kept. [y/N] "):
        print("Left unchanged.")
        return True, False
    try:
        backup = remove_entries(config_path, raw, config, names)
    except OSError as exc:
        print(f"Could not write {config_path}: {exc}")
        return False, False
    print(f"Removed. The previous file is saved as {backup}.")
    return True, True


def main(argv: Sequence[str] | None = None, ask: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(
        prog="remove_uvx_entry",
        description="Remove an old uvx doc-control server from claude_desktop_config.json.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        action="append",
        help="the config file (repeatable); default: where Claude Desktop keeps it",
    )
    parser.add_argument("--dry-run", action="store_true", help="say what would go; change nothing")
    parser.add_argument("--yes", action="store_true", help="do not ask before removing")
    parser.add_argument("--pause", action="store_true", help="wait for Enter before exiting")
    args = parser.parse_args(argv)

    paths: list[Path] = args.config or config_candidates(os.environ)
    ok, changed = True, False
    if not paths:
        print(f"No Claude Desktop {CONFIG_NAME} found: nothing to remove.")
    for path in paths:
        one_ok, one_changed = process(path, dry_run=args.dry_run, yes=args.yes, ask=ask)
        ok, changed = ok and one_ok, changed or one_changed
    if changed:
        print(RESTART)
    if args.pause:
        with contextlib.suppress(EOFError):
            ask("Press Enter to close.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
