"""The ``live.*`` operations: a document open in Word or LibreOffice (see ``doc_control.live``).

``live.open`` attaches to the window holding the document, or opens it; the others act
on a document a live app already holds. Closing never drops the writer's unsaved work:
a changed document closes only with ``save`` or ``discard``.

``live.select`` puts the app's selection on a paragraph, named by address or by a find
phrase from ``locate``; ``paragraphs_after`` moves that many paragraphs on from it (as in
``locate``'s ``context``).
"""

from __future__ import annotations

from bisect import bisect_right
from itertools import accumulate
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import adapter_for, adapters, apps, guarded, unavailable
from doc_control.live.protocol import LiveAdapter, LiveUnavailable, Span
from doc_control.model.address import Address, fingerprint, parse_address
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
        for adapter, probe in zip(adapters(), out["apps"], strict=True):
            try:
                if probe.get("reachable") and adapter.is_open(path):
                    out["open_in"] = {"app": adapter.app, "controlled": True}
                    out["dirty"] = adapter.dirty(path)
                    break
            except LiveUnavailable:
                continue  # the app went away after its probe: not open there
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


def _found(texts: list[str], phrase: str, across: str | None) -> int:
    """The paragraph (from 0) a find phrase names: its one match, under ``locate``'s rules."""
    folded = [text.casefold() for text in texts]
    joined, needle = "\r".join(folded) + "\r", phrase.casefold()
    at = joined.find(needle)
    if at < 0:
        raise DocControlError(ErrorCode.ANCHOR_NOT_FOUND, f"no match for {phrase!r}")
    if joined.find(needle, at + 1) >= 0:
        raise DocControlError(ErrorCode.ANCHOR_AMBIGUOUS, f"more than one match for {phrase!r}")
    starts = list(accumulate((len(text) + 1 for text in folded), initial=0))
    # A phrase that opens with the paragraph before (or with its mark) names the next one.
    return bisect_right(starts, at) - 1 + (across in ("previous", "both") or needle[0] == "\r")


@operation(
    "live.select",
    "write",
    "Select a paragraph in the app's window, by address (`at`) or by a `locate` find "
    "phrase, and return its text and address.",
    _schema(
        at={"type": "string", "description": "Paragraph address, e.g. `p142#a7b2a7b2`."},
        find_phrase={
            "type": "string",
            "minLength": 1,
            "description": "A phrase that occurs once in the document (`\\r` for a paragraph "
            "mark), as `locate` gives it.",
        },
        find_phrase_across={
            "enum": ["previous", "next", "both"],
            "description": "`locate`'s `find_phrase_across`, for a phrase spanning a mark.",
        },
        paragraphs_after={
            "type": "integer",
            "minimum": 0,
            "description": "Select the paragraph this many after the one named.",
        },
    ),
)
def select(request: dict[str, Any]) -> dict[str, Any]:
    adapter, path = held(request)
    if ("at" in request) == ("find_phrase" in request):
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "pass at or find_phrase, one of them")
    texts = guarded(adapter.paragraphs, path)
    if "at" in request:
        address = parse_address(request["at"])
        n = address.number - 1
        if n >= len(texts) or fingerprint(guarded(adapter.text, path, Span(n, n))) != address.fp:
            raise DocControlError(
                ErrorCode.STALE_ADDRESS,
                f"{address} does not match the open document",
                address=str(address),
            )
    else:
        n = _found(texts, request["find_phrase"], request.get("find_phrase_across"))
    n += request.get("paragraphs_after", 0)
    if n >= len(texts):
        raise DocControlError(
            ErrorCode.ANCHOR_NOT_FOUND, f"the open document has no paragraph {n + 1}"
        )
    text = guarded(adapter.select, path, Span(n, n))
    return {"text": text, "at": str(Address(n + 1, fingerprint(text)))}


@operation(
    "live.selection",
    "read",
    "The text selected in the app's window, and the paragraph the cursor is in.",
    _schema(),
)
def selection(request: dict[str, Any]) -> dict[str, Any]:
    adapter, path = held(request)
    chosen, n = guarded(adapter.selection, path)
    text = guarded(adapter.text, path, Span(n, n))
    return {
        "text": chosen,
        "paragraph": {"at": str(Address(n + 1, fingerprint(text))), "text": text},
    }


def live_batch(request: dict[str, Any]) -> dict[str, Any]:
    """``batch`` with ``engine: "live"``."""
    held(request)
    raise unavailable("operation not supported live")
