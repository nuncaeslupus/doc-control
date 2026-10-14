"""The ``live.*`` operations: a document open in Word or LibreOffice (see ``doc_control.live``).

``live.open`` attaches to the window holding the document, or opens it; the others act
on a document a live app already holds. Closing never drops the writer's unsaved work:
a changed document closes only with ``save`` or ``discard``.

``doc`` may be a bare file name (``Novel.docx``): it names the one document of that name
a live app has open, wherever it is, for a caller that does not know the path the app
sees (an assistant in a sandbox). ``live.status`` without ``doc`` lists them.

``live.select`` puts the app's selection on a paragraph, named by address or by a find
phrase from ``locate``; ``paragraphs_after`` moves that many paragraphs on from it (as in
``locate``'s ``context``).
"""

from __future__ import annotations

import os
from bisect import bisect_right
from itertools import accumulate, pairwise
from math import inf
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import adapter_for, adapters, apps, guarded, unavailable
from doc_control.live.protocol import LiveAdapter, LiveUnavailable, Span
from doc_control.model.address import Address, fingerprint, parse_address, parse_range
from doc_control.ooxml.revisions import DEFAULT_AUTHOR, format_author
from doc_control.ops.comments import AddComment
from doc_control.ops.tracked import InsertParagraph
from doc_control.registry import operation
from doc_control.safety.history import append_entry
from doc_control.safety.lock import lock_info
from doc_control.safety.store import DocStore

LIVE_KINDS = ("comment.add", "text.replace", "para.insert")

_DOC = {
    "type": "string",
    "minLength": 1,
    "description": "Path to the document, or the file name of one open in a live app.",
}
_ENGINE = {"enum": ["live"], "description": "Always the live engine; may be left out."}


def _schema(required: bool = True, **extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"doc": _DOC, "engine": _ENGINE, **extra},
        "required": ["doc"] if required else [],
        "additionalProperties": False,
    }


def _fresh(adapter: LiveAdapter, path: Path, count: int, address: Address) -> str:
    """The text of the paragraph at ``address``; ``STALE_ADDRESS`` when it reads otherwise."""
    if address.story:
        raise unavailable(f"{address}: notes, headers and footers are file engine only")
    n = address.number - 1
    text: str = guarded(adapter.text, path, Span(n, n)) if n < count else ""
    if n >= count or fingerprint(text) != address.fp:
        raise DocControlError(
            ErrorCode.STALE_ADDRESS,
            f"{address} does not match the open document",
            address=str(address),
        )
    return text


def _open_documents(probes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every document the reachable live apps have open: ``{"app", "path", "name"}``."""
    found = []
    for adapter, probe in zip(adapters(), probes or apps(), strict=True):
        try:
            paths = adapter.documents() if probe.get("reachable") else []
        except LiveUnavailable:
            continue  # the app went away after its probe
        found += [{"app": adapter.app, "path": str(p), "name": p.name} for p in paths]
    return found


def _named(doc: str, probes: list[dict[str, Any]] | None = None) -> Path:
    """``doc`` as a path; a bare file name is the one open document of that name, if any."""
    path = Path(doc)
    if path.name != doc or os.sep in doc or "/" in doc:
        return path
    want = os.path.normcase(doc)
    matches = {
        os.path.normcase(str(Path(d["path"]).resolve())): d["path"]
        for d in _open_documents(probes)
        if os.path.normcase(d["name"]) == want
    }
    if len(matches) > 1:
        raise DocControlError(
            ErrorCode.DOC_AMBIGUOUS,
            f"{len(matches)} open documents are named {doc}",
            candidates=sorted(matches.values()),
        )
    return Path(next(iter(matches.values()))) if matches else path


def held(request: dict[str, Any]) -> tuple[LiveAdapter, Path]:
    """The adapter that has the request's document open."""
    path = _named(request["doc"])
    adapter = adapter_for(path)
    if not guarded(adapter.is_open, path):
        raise unavailable(f"{path.name} is not open in a live app: call live.open first")
    return adapter, path


@operation(
    "live.status",
    "read",
    "Live apps (Word, LibreOffice) found and reachable, and the documents they have open; "
    "with `doc`, which app has it open, its full path, and whether it has unsaved changes.",
    _schema(required=False),
)
def status(request: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"apps": apps(), "open_in": None, "dirty": False}
    if "doc" not in request:
        out["documents"] = _open_documents(out["apps"])
    else:
        path = _named(request["doc"], out["apps"])
        out["path"] = str(path.resolve())
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
    path = _named(request["doc"])
    if not path.is_file():
        raise DocControlError(ErrorCode.DOC_NOT_FOUND, f"no such file: {path}", path=str(path))
    adapter = adapter_for(path, request.get("app"))
    guarded(adapter.open, path)
    return {"app": adapter.app, "path": str(path.resolve()), "dirty": guarded(adapter.dirty, path)}


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
    if across and "\r" not in phrase:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, "find_phrase_across needs a phrase with a paragraph mark"
        )
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
        _fresh(adapter, path, len(texts), address)
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
    """``batch`` with ``engine: "live"``: comments and tracked edits in the open document.

    Addresses refer to the document as read, so the edits are applied from the end of
    the document backwards. Each one is logged to history (``engine: live``) without a
    backup: until the next save, taking one back is the app's own undo.
    """
    from doc_control.ops.batch import edits_from_request  # batch imports this module too

    adapter, path = held(request)
    for guard in ("dry_run", "expect"):
        if request.get(guard):
            raise unavailable(f"{guard} is not supported live")
    ops = request["ops"]
    for i, raw in enumerate(ops):
        if raw.get("op") not in LIVE_KINDS:
            raise unavailable("operation not supported live", op_index=i, op=raw.get("op"))
    author = format_author(
        request.get("author") or os.environ.get("DOC_CONTROL_LIVE_AUTHOR") or DEFAULT_AUTHOR,
        request.get("tag"),
    )
    count = len(guarded(adapter.paragraphs, path))
    # (start, end, index in the request, adapter call, where)
    plan: list[tuple[tuple[float, float], tuple[float, float], int, Any, Any]] = []
    for i, edit in enumerate(edits_from_request(ops)):
        if isinstance(edit, InsertParagraph):
            if (edit.after is None) == (edit.before is None):
                raise DocControlError(
                    ErrorCode.SCHEMA_INVALID, f"ops[{i}]: pass after or before, one of them"
                )
            address = parse_address(edit.after or edit.before or "")
            _fresh(adapter, path, count, address)
            after = address.number - (1 if edit.after else 2)
            if any(step[4] == after for step in plan if step[3] == adapter.tracked_insert):
                raise DocControlError(
                    ErrorCode.BATCH_CONFLICT, f"ops[{i}] inserts where an earlier one does"
                )
            plan.append(((after, inf), (after, inf), i, adapter.tracked_insert, after))
            continue
        rng = parse_range(edit.at)
        if rng.sentence is not None:
            raise unavailable("sentence addresses are not supported live", op_index=i)
        first = _fresh(adapter, path, count, rng.start)
        last = first if rng.end == rng.start else _fresh(adapter, path, count, rng.end)
        a, b = rng.start_offset or 0, rng.end_offset
        if a > len(first) or (b is not None and b > len(last)):
            raise DocControlError(
                ErrorCode.ADDRESS_INVALID, f"ops[{i}]: {edit.at} runs past its paragraph"
            )
        span = Span(rng.start.number - 1, rng.end.number - 1, a, b)
        guarded(adapter.editable, path, span, not isinstance(edit, AddComment))
        call = adapter.comment if isinstance(edit, AddComment) else adapter.tracked_replace
        plan.append(((span.start, a), (span.end, inf if b is None else b), i, call, span))
    plan.sort(key=lambda step: (step[0], step[2]), reverse=True)
    for later, earlier in pairwise(plan):
        if earlier[1] > later[0]:
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                f"ops[{earlier[2]}] and ops[{later[2]}] overlap: send them as separate batches",
            )
    store = DocStore.open(path)
    op_ids: dict[int, str] = {}
    # ponytail: not all-or-nothing. Every address and span is checked before the first
    # edit, so a batch should stop half-way only when the app goes away; wrap it in the
    # app's undo context if a writer is ever left with half a batch.
    for _, _, i, call, where in plan:
        guarded(call, path, where, ops[i]["text"], author)
        entry = append_entry(
            store,
            {"kind": "batch", "engine": "live", "author": author, "request": [ops[i]]},
            None,
        )
        op_ids[i] = entry["op_id"]
    return {
        "engine": "live",
        "applied": len(plan),
        "author": author,
        "op_ids": [op_ids[i] for i in range(len(plan))],
    }
