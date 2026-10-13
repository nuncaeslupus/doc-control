"""The ``live.*`` operations: a document open in Word or LibreOffice (see ``doc_control.live``).

``live.open`` attaches to the window holding the document, or opens it; the others act
on a document a live app already holds. Closing never drops the writer's unsaved work:
a changed document closes only with ``save`` or ``discard``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import adapter_for, adapters, apps, guarded, unavailable
from doc_control.live.protocol import LiveAdapter
from doc_control.registry import operation
from doc_control.safety.lock import lock_info

_DOC = {"type": "string", "minLength": 1, "description": "Path to the document."}
_ENGINE = {"enum": ["live"], "description": "Always the live engine; may be left out."}


def _schema(required: bool = True, **extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"doc": _DOC, "engine": _ENGINE, **extra},
        "required": ["doc"] if required else [],
        "additionalProperties": False,
    }


def held(request: dict[str, Any]) -> tuple[LiveAdapter, Path]:
    """The adapter that has the request's document open."""
    path = Path(request["doc"])
    adapter = adapter_for(path)
    if not guarded(adapter.is_open, path):
        raise unavailable(f"{path.name} is not open in a live app: call live.open first")
    return adapter, path


@operation(
    "live.status",
    "read",
    "Live apps (Word, LibreOffice) found and reachable; with `doc`, which app has it open "
    "and whether it has unsaved changes.",
    _schema(required=False),
)
def status(request: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"apps": apps(), "open_in": None, "dirty": False}
    if "doc" in request:
        path = Path(request["doc"])
        for adapter in adapters():
            if adapter.probe().get("reachable") and guarded(adapter.is_open, path):
                out["open_in"] = {"app": adapter.app, "controlled": True}
                out["dirty"] = guarded(adapter.dirty, path)
                break
        else:
            lock = lock_info(path)
            if lock is not None:
                # Open in a window this engine does not drive (or a lock left behind).
                out["open_in"] = {"app": lock["app"], "controlled": False, "stale": lock["stale"]}
    return out


@operation(
    "live.open",
    "write",
    "Attach to the app window holding the document, or open it in a live app.",
    _schema(app={"type": "string", "minLength": 1, "description": "`word` or `libreoffice`."}),
)
def open_(request: dict[str, Any]) -> dict[str, Any]:
    path = Path(request["doc"])
    if not path.is_file():
        raise DocControlError(ErrorCode.DOC_NOT_FOUND, f"no such file: {path}", path=str(path))
    adapter = adapter_for(path, request.get("app"))
    guarded(adapter.open, path)
    return {"app": adapter.app, "dirty": guarded(adapter.dirty, path)}


@operation("live.save", "write", "Save the open document in its app.", _schema())
def save(request: dict[str, Any]) -> dict[str, Any]:
    adapter, path = held(request)
    guarded(adapter.save, path)
    return {"saved": True}


@operation(
    "live.close",
    "write",
    "Close the open document; with unsaved changes it needs `save` or `discard`.",
    _schema(
        save={"type": "boolean", "description": "Save, then close."},
        discard={"type": "boolean", "description": "Close and drop the unsaved changes."},
    ),
)
def close(request: dict[str, Any]) -> dict[str, Any]:
    adapter, path = held(request)
    save_first, discard = request.get("save", False), request.get("discard", False)
    if save_first and discard:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "pass save or discard, not both")
    if save_first:
        guarded(adapter.save, path)
    elif not discard and guarded(adapter.dirty, path):
        raise DocControlError(
            ErrorCode.UNSAVED_CHANGES,
            f"{path.name} has unsaved changes; it stays open",
            path=str(path),
        )
    guarded(adapter.close, path)
    return {"closed": True, "saved": save_first}


def live_batch(request: dict[str, Any]) -> dict[str, Any]:
    """``batch`` with ``engine: "live"``."""
    held(request)
    raise unavailable("operation not supported live")
