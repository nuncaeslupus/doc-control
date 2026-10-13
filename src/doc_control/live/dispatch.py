"""Pick the app adapter for a live call and turn its failures into error envelopes."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.protocol import LiveAdapter, LiveUnavailable

#: App name to the module (in ``doc_control.live``) whose ``adapter()`` builds its adapter.
ADAPTER_MODULES: dict[str, str] = {"libreoffice": "lo_uno"}

_override: list[LiveAdapter] | None = None


@contextmanager
def using(*adapters: LiveAdapter) -> Iterator[None]:
    """Run live calls against ``adapters`` only (tests; none means no app at all)."""
    global _override
    previous, _override = _override, list(adapters)
    try:
        yield
    finally:
        _override = previous


def adapters() -> list[LiveAdapter]:
    if _override is not None:
        return list(_override)
    found: list[LiveAdapter] = []
    for module in ADAPTER_MODULES.values():
        found.append(importlib.import_module(f"doc_control.live.{module}").adapter())
    return found


def apps() -> list[dict[str, Any]]:
    """Every known app with whether it is installed and reachable; never raises."""
    found = []
    for a in adapters():
        try:
            found.append(a.probe())
        except Exception as err:  # a broken install must not take `capabilities` down
            found.append({"app": a.app, "reachable": False, "reason": str(err)})
    return found


def unavailable(reason: str, **details: Any) -> DocControlError:
    return DocControlError(
        ErrorCode.ENGINE_UNAVAILABLE,
        f"live engine unavailable: {reason}",
        reason=reason,
        **details,
    )


def adapter_for(path: Path | None = None, app: str | None = None) -> LiveAdapter:
    """The adapter holding ``path`` open, else the first reachable one (``app`` narrows it)."""
    known = adapters()
    wanted = [a for a in known if app in (None, a.app)]
    if not wanted:
        names = ", ".join(a.app for a in known) or "none"
        raise unavailable(
            f"no live app named {app!r} (known: {names})"
            if app
            else "no live app is supported here"
        )
    probes = [(a, a.probe()) for a in wanted]
    reachable = [a for a, p in probes if p.get("reachable")]
    if not reachable:
        reasons = "; ".join(f"{p['app']}: {p.get('reason', 'not reachable')}" for _, p in probes)
        raise unavailable(reasons, apps=[p for _, p in probes])
    if path is not None:
        for a in reachable:
            if guarded(a.is_open, path):
                return a
    return reachable[0]


def guarded(call: Any, *args: Any) -> Any:
    """Run an adapter call; an app that went away is ``ENGINE_UNAVAILABLE``, not a traceback."""
    try:
        return call(*args)
    except LiveUnavailable as err:
        raise unavailable(str(err)) from err
