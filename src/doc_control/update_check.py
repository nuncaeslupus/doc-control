"""Opt-in update notice (D-P7): off by default, and then no network request at all.

Switched on with ``DOC_CONTROL_UPDATE_CHECK=1`` (the ``.mcpb`` setting ``update_check``
sets it), one GET of the GitHub Releases API a day, cached as JSON in the user's local
app data. It reports; it never downloads or installs. Every failure is quiet.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from doc_control import __version__

ENV_SWITCH = "DOC_CONTROL_UPDATE_CHECK"
RELEASES_URL = "https://api.github.com/repos/nuncaeslupus/doc-control/releases/latest"
TIMEOUT_SECONDS = 3
CACHE_SECONDS = 24 * 60 * 60
_TRUE = {"1", "true", "yes", "on"}


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only when the user opted in."""
    env = os.environ if env is None else env
    return env.get(ENV_SWITCH, "").strip().lower() in _TRUE


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Local (not roaming) app data: ``DOC_CONTROL_CACHE_DIR`` or the platform default."""
    env = os.environ if env is None else env
    if env.get("DOC_CONTROL_CACHE_DIR"):
        return Path(env["DOC_CONTROL_CACHE_DIR"]).expanduser()
    if sys.platform.startswith("win"):
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else Path("~/AppData/Local").expanduser()) / "doc-control"
    if sys.platform == "darwin":
        return Path("~/Library/Caches").expanduser() / "doc-control"
    xdg = env.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path("~/.cache").expanduser()) / "doc-control"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("checked_at"), (int, float)):
        return data
    return None


def _fetch() -> dict[str, Any]:
    request = urllib.request.Request(
        RELEASES_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "doc-control"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        release = json.loads(response.read().decode("utf-8"))
    latest = str(release["tag_name"]).lstrip("v")
    mcpb_url = next(
        (
            a["browser_download_url"]
            for a in release.get("assets", [])
            if str(a.get("name", "")).endswith(".mcpb")
        ),
        None,
    )
    return {"latest": latest, "mcpb_url": mcpb_url}


def update_info(
    env: Mapping[str, str] | None = None, now: float | None = None
) -> dict[str, Any] | None:
    """The ``update`` field, or ``None`` when the notice is off (then nothing is touched)."""
    env = os.environ if env is None else env
    if not enabled(env):
        return None
    now = time.time() if now is None else now
    try:
        path = cache_dir(env) / "update-check.json"
        cached = _read_cache(path)
        if cached is None or not 0 <= now - cached["checked_at"] < CACHE_SECONDS:
            cached = {**_fetch(), "checked_at": now}
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(cached), encoding="utf-8")
            except OSError:
                pass  # a cache that cannot be written only costs a request tomorrow
        return {
            "current": __version__,
            "latest": cached.get("latest"),
            "mcpb_url": cached.get("mcpb_url"),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cached["checked_at"])),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def attach(result: dict[str, Any]) -> dict[str, Any]:
    """Add ``update`` to a result when the notice is on."""
    info = update_info()
    if info is not None:
        result["update"] = info
    return result
