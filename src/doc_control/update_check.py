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
RETRY_SECONDS = 60 * 60  # after a failure or a release without its .mcpb yet
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
    """The cache, or ``None`` when missing or corrupt (any wrong type means refetch)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    checked, ttl = data.get("checked_at"), data.get("ttl", CACHE_SECONDS)
    for number in (checked, ttl):
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return None
    for key in ("latest", "mcpb_url", "error"):
        if data.get(key) is not None and not isinstance(data[key], str):
            return None
    return data


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    """Atomic: a temp file beside the cache, then a rename over it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # a cache that cannot be written only costs another request


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
    """The ``update`` field, or ``None`` when the notice is off (then nothing is touched).

    A failure is cached too (with the last good answer kept), and so is a release whose
    ``.mcpb`` is not attached yet, both for an hour: no call waits on the network twice
    in a window.
    """
    env = os.environ if env is None else env
    if not enabled(env):
        return None
    now = time.time() if now is None else now
    try:
        path = cache_dir(env) / "update-check.json"
        cached = _read_cache(path)
        if cached is None or not 0 <= now - cached["checked_at"] < cached.get("ttl", CACHE_SECONDS):
            try:
                fresh = _fetch()
                ttl = CACHE_SECONDS if fresh["mcpb_url"] else RETRY_SECONDS
                cached = {**fresh, "checked_at": now, "ttl": ttl}
            except Exception as exc:
                last = cached or {}
                cached = {
                    "latest": last.get("latest"),
                    "mcpb_url": last.get("mcpb_url"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "checked_at": now,
                    "ttl": RETRY_SECONDS,
                }
            _write_cache(path, cached)
        if cached.get("error") and cached.get("latest") is None:
            return {"error": cached["error"]}
        out = {
            "current": __version__,
            "latest": cached.get("latest"),
            "mcpb_url": cached.get("mcpb_url"),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cached["checked_at"])),
        }
        if cached.get("error"):
            out["error"] = cached["error"]
        return out
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def attach(result: dict[str, Any]) -> dict[str, Any]:
    """Add ``update`` to a result when the notice is on."""
    info = update_info()
    if info is not None:
        result["update"] = info
    return result
