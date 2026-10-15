"""The ``live.*`` operations: a document open in Word or LibreOffice (see ``doc_control.live``).

``live.open`` attaches to the window holding the document, or opens it; the others act
on a document a live app already holds. Closing never drops the writer's unsaved work:
a changed document closes only with ``save`` or ``discard``.

``doc`` may be a bare file name (``Novel.docx``): it names the one document of that name
a live app has open, wherever it is, for a caller that does not know the path the app
sees (an assistant in a sandbox). ``live.status`` without ``doc`` lists them.

``live.select`` puts the app's selection on a paragraph, named by address or by a find
phrase from ``para.locate``; ``paragraphs_after`` moves that many paragraphs on from it (as in
``para.locate``'s ``context``). ``live.places`` hands a list of such places to the MCP App in
``doc_control/app`` (see ``doc_control.mcp``), whose buttons call ``live.select``.
"""

from __future__ import annotations

import os
from bisect import bisect_right
from itertools import accumulate, islice, pairwise
from math import inf
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.dispatch import adapter_for, adapters, apps, guarded, unavailable
from doc_control.live.protocol import LiveAdapter, LiveUnavailable, Span
from doc_control.model.address import Address, fingerprint, parse_address, parse_range
from doc_control.model.loosefold import (
    LEVELS,
    Hit,
    Level,
    exact_spans,
    fold_phrase,
    joined_hits,
    ladder,
)
from doc_control.model.wordcodes import translate_codes
from doc_control.ooxml.revisions import default_author, format_author
from doc_control.ops.comments import AddComment
from doc_control.ops.tracked import InsertParagraph
from doc_control.registry import operation
from doc_control.safety.history import append_entry
from doc_control.safety.lock import lock_info
from doc_control.safety.store import DocStore
from doc_control.update_check import attach

LIVE_KINDS = ("comment.add", "text.replace", "para.insert")

_DOC = {
    "type": "string",
    "minLength": 1,
    "description": "Path to the document, or the file name of one open in a live app.",
}
_ENGINE = {"enum": ["live"], "description": "Always the live engine; may be left out."}
_FRONT = {
    "type": "boolean",
    "description": "Bring the app's window to the front (default true); the reply's "
    "`front` says whether it came.",
}


def _front(adapter: Any, path: Path) -> bool:
    """Bring the app's window to the front. The open or select has already happened, so a
    window that cannot come (a busy app, a frame going away) is ``False``, not a failure."""
    try:
        return bool(adapter.show(path))
    except Exception:
        return False


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


def _open_documents(
    probes: list[dict[str, Any]] | None = None, strict: bool = False
) -> list[dict[str, Any]]:
    """Every document the reachable live apps have open: ``{"app", "path", "name"}``.

    An app that cannot list them gets its ``reason`` in ``probes``, or, ``strict``, raises
    ``ENGINE_UNAVAILABLE``: a name looked up while an app is busy is not "not open".
    """
    found = []
    for adapter, probe in zip(adapters(), probes or apps(), strict=True):
        try:
            paths = adapter.documents() if probe.get("reachable") else []
        except LiveUnavailable as err:
            if strict:
                raise unavailable(f"{adapter.app}: {err}") from err
            probe["reason"] = str(err)
            continue
        found += [{"app": adapter.app, "path": str(p), "name": p.name} for p in paths]
    return found


def _named(doc: str, probes: list[dict[str, Any]] | None = None) -> Path:
    """``doc`` as a path; a bare file name is the one open document of that name, if any.

    A file of that name in the working directory counts as a candidate too, so a bare
    name never acts on another document when it could mean that one.
    """
    path = Path(doc)
    if path.name != doc or os.sep in doc or "/" in doc:
        return path
    want = os.path.normcase(doc)
    matches = {
        os.path.normcase(str(Path(d["path"]).resolve())): d["path"]
        for d in _open_documents(probes, strict=True)
        if os.path.normcase(d["name"]) == want
    }
    if matches and path.is_file():
        matches.setdefault(os.path.normcase(str(path.resolve())), str(path.resolve()))
    if len(matches) > 1:
        raise DocControlError(
            ErrorCode.DOC_AMBIGUOUS,
            f"{len(matches)} documents are named {doc}",
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
    (
        "Check which live apps (Word, LibreOffice) are running and reachable and which "
        "documents they have open; with `doc`, which app holds it, its full path and "
        "whether it has unsaved changes. Call it before live.open. Read-only."
    ),
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
    return attach(out)


@operation(
    "live.open",
    "write",
    (
        "Attach to Word or LibreOffice for live editing: find the app window already "
        "holding the document, or open it in the app (`app`). Call it before the other "
        "live operations (engine live). It opens or focuses a window but does not change "
        "the document."
    ),
    _schema(
        app={"type": "string", "minLength": 1, "description": "`word` or `libreoffice`."},
        front=_FRONT,
    ),
)
def open_(request: dict[str, Any]) -> dict[str, Any]:
    path = _named(request["doc"])
    if not path.is_file():
        raise DocControlError(ErrorCode.DOC_NOT_FOUND, f"no such file: {path}", path=str(path))
    adapter = adapter_for(path, request.get("app"))
    guarded(adapter.open, path)
    out = {"app": adapter.app, "path": str(path.resolve()), "dirty": guarded(adapter.dirty, path)}
    if request.get("front", True):
        out["front"] = _front(adapter, path)
    return out


@operation(
    "live.save",
    "write",
    (
        "Save the document open in Word or LibreOffice (attached with live.open) to its "
        "file, from inside the app. Call it after live edits (doc.batch with engine live) to "
        "keep them; live.close with save: true saves and closes in one call."
    ),
    _schema(),
)
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


# Enough hits to tell one from several and to list five candidates.
_HIT_LIMIT = 8


def _found(
    texts: list[str], phrase: str, across: str | None, requested: Level = "default"
) -> tuple[int, Level, str]:
    """The paragraph (from 0) a find phrase names, the level it matched at and its text.

    ``requested`` ``exact`` is ``para.locate``'s rule of today: one casefold match in the
    joined text, the phrase taken as given. Otherwise the ladder runs from ``exact`` to
    ``loose`` over the joined text, and the first level with any hit must have exactly
    one. A phrase holding a caret is tried raw, verbatim, before its Word codes are
    translated. The ``case`` level is today's casefold rule first, and the ``case`` fold
    (format characters dropped, whitespace collapsed) only when that finds nothing.
    """
    if requested == "exact":
        if across and "\r" not in phrase:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID, "find_phrase_across needs a phrase with a paragraph mark"
            )
        spans = _casefold_spans(texts, phrase, 2)
        if not spans:
            raise DocControlError(
                ErrorCode.ANCHOR_NOT_FOUND, f"no match for {phrase!r}", tried=["exact"]
            )
        if len(spans) > 1:
            raise DocControlError(ErrorCode.ANCHOR_AMBIGUOUS, f"more than one match for {phrase!r}")
        joined = "\r".join(texts) + "\r"
        start, end = spans[0]
        level: Level = "exact" if joined[start:end] == phrase else "case"
        found = [Hit(0, start, end, level)]
    else:
        translated = translate_codes(phrase)
        if across and "\r" not in translated:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID, "find_phrase_across needs a phrase with a paragraph mark"
            )
        joined = "\r".join(texts) + "\r"
        found = _ladder_hits(texts, joined, phrase, translated)
        if not found:
            raise DocControlError(
                ErrorCode.ANCHOR_NOT_FOUND,
                f"no match for {phrase!r}",
                tried=list(ladder(requested, retry=True)),
            )
    starts = list(accumulate((len(text) + 1 for text in texts), initial=0))

    def paragraph(hit: Hit) -> int:
        # A phrase that opens with the paragraph before (or with its mark) names the next one.
        return (
            bisect_right(starts, hit.start)
            - 1
            + (across in ("previous", "both") or joined[hit.start] == "\r")
        )

    # A hit naming no paragraph (past the last mark) is no candidate.
    named = [hit for hit in found if paragraph(hit) < len(texts)]
    if len(named) > 1:
        raise DocControlError(
            ErrorCode.ANCHOR_AMBIGUOUS,
            f"more than one match for {phrase!r} at match level {named[0].level!r}",
            match_level=named[0].level,
            candidates=[
                {
                    "at": str(Address(paragraph(hit) + 1, fingerprint(texts[paragraph(hit)]))),
                    "matched_text": joined[hit.start : hit.end],
                }
                for hit in named[:5]
            ],
        )
    hit = named[0] if named else found[0]
    return paragraph(hit), hit.level, joined[hit.start : hit.end]


def _ladder_hits(texts: list[str], joined: str, phrase: str, translated: str) -> list[Hit]:
    """The hits of the first level of the ladder that has any, over ``joined``."""

    def verbatim(needle: str) -> list[Hit]:
        spans = exact_spans(joined, needle)
        return [Hit(0, start, end, "exact") for start, end in islice(spans, _HIT_LIMIT)]

    found = verbatim(phrase) if "^" in phrase else []
    if not found:
        fold_phrase(translated, "exact")
        found = verbatim(translated)
    if not found:
        fold_phrase(translated, "case")
        spans = _casefold_spans(texts, translated, _HIT_LIMIT)
        found = [Hit(0, start, end, "case") for start, end in spans]
    for level in LEVELS[1:]:
        if found:
            break
        found = joined_hits(texts, translated, level, limit=_HIT_LIMIT)
    return found


def _casefold_spans(texts: list[str], phrase: str, limit: int) -> list[tuple[int, int]]:
    """Today's rule: the first ``limit`` casefold matches in the joined text, overlapping.

    Spans are original offsets in ``"\\r".join(texts) + "\\r"``. casefold maps each
    character on its own, so only the paragraphs a match starts and ends in are mapped
    back, one character at a time, and only when casefold changed their length.
    """
    needle = phrase.casefold()
    if not needle:
        return []
    raw = "\r".join(texts) + "\r"
    whole = raw.casefold()
    spans: list[tuple[int, int]] = []
    if len(whole) == len(raw):  # every character casefolds to one: offsets are the same
        at = whole.find(needle)
        while at >= 0 and len(spans) < limit:
            spans.append((at, at + len(needle)))
            at = whole.find(needle, at + 1)
        return spans
    folded = [text.casefold() for text in texts]
    joined = "\r".join(folded) + "\r"
    fstarts = list(accumulate((len(text) + 1 for text in folded), initial=0))
    starts = list(accumulate((len(text) + 1 for text in texts), initial=0))

    def original(at: int) -> int:
        n = bisect_right(fstarts, at) - 1
        offset, text = at - fstarts[n], texts[n]
        if offset >= len(folded[n]) or len(folded[n]) == len(text):
            return starts[n] + min(offset, len(text))
        for i, ch in enumerate(text):
            offset -= len(ch.casefold())
            if offset < 0:
                return starts[n] + i
        return starts[n] + len(text)  # pragma: no cover - offset is inside the text

    at = joined.find(needle)
    while at >= 0 and len(spans) < limit:
        spans.append((original(at), original(at + len(needle) - 1) + 1))
        at = joined.find(needle, at + 1)
    return spans


@operation(
    "live.select",
    "write",
    (
        "Show the user a place: select a paragraph (or put the cursor at it) in the Word "
        "or LibreOffice window, by address (`at`) or by a para.locate find phrase, and return "
        "its text and address. Changes only the selection, never the text; the document "
        "must be attached with live.open."
    ),
    _schema(
        at={"type": "string", "description": "Paragraph address, e.g. `p142#a7b2a7b2`."},
        find_phrase={
            "type": "string",
            "minLength": 1,
            "description": "A phrase that occurs once in the document (`\\r` for a paragraph "
            "mark), as `para.locate` gives it.",
        },
        find_phrase_across={
            "enum": ["previous", "next", "both"],
            "description": "`para.locate`'s `find_phrase_across`, for a phrase spanning a mark.",
        },
        match_level={
            "enum": ["exact", "default", "loose"],
            "description": "How loosely `find_phrase` matches. `default` (the default) "
            "ignores case, accents and punctuation; `loose` symbols too. Either tries "
            "the levels from verbatim on and takes the first that matches, so one "
            "verbatim paragraph beats looser ones. `exact` is `para.locate`'s rule: case "
            "ignored, nothing else, no retry. Two meanings of exact: the request's "
            "`exact` allows a case difference, the reply's does not. The reply's "
            "`match_level` (`exact` character for character, `case`, `default`, "
            "`loose`) names the level that matched, with `matched_text` when it is "
            "not `exact`.",
        },
        mode={
            "enum": ["select", "cursor"],
            "description": "`select` (default) selects the paragraph; `cursor` only puts "
            "the cursor at its start, so typing or Delete cannot replace it.",
        },
        front=_FRONT,
        paragraphs_after={
            "type": "integer",
            "minimum": 0,
            "description": "Select the paragraph this many after the one named.",
        },
    ),
)
def select(request: dict[str, Any]) -> dict[str, Any]:
    adapter, path = held(request)
    cursor = request.get("mode") == "cursor"
    if ("at" in request) == ("find_phrase" in request):
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "pass at or find_phrase, one of them")
    texts = guarded(adapter.paragraphs, path)
    if "at" in request:
        address = parse_address(request["at"])
        n = address.number - 1
        _fresh(adapter, path, len(texts), address)
    else:
        n, level, matched = _found(
            texts,
            request["find_phrase"],
            request.get("find_phrase_across"),
            request.get("match_level", "default"),
        )
    n += request.get("paragraphs_after", 0)
    if n >= len(texts):
        raise DocControlError(
            ErrorCode.ANCHOR_NOT_FOUND, f"the open document has no paragraph {n + 1}"
        )
    text = guarded(adapter.select, path, Span(n, n), cursor)
    out: dict[str, Any] = {"text": text, "at": str(Address(n + 1, fingerprint(text)))}
    if "find_phrase" in request:
        out["match_level"] = level
        if level != "exact":
            out["matched_text"] = matched
    if request.get("front", True):
        out["front"] = _front(adapter, path)
    return out


_PLACE = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "minLength": 1, "description": "The button's text."},
        "at": {
            "type": "string",
            "minLength": 1,
            "description": "Paragraph address, e.g. `p142#a7b2a7b2`.",
        },
        "find_phrase": {
            "type": "string",
            "minLength": 1,
            "description": "A `para.locate` find phrase; used when `at` is missing or stale.",
        },
        "find_phrase_across": {"enum": ["previous", "next", "both"]},
    },
    "required": ["label"],
    "additionalProperties": False,
}


@operation(
    "live.places",
    "read",
    "Show a list of places in the document as buttons; in a client that renders MCP Apps, "
    "a click calls `live.select` on that place directly, with no assistant turn.",
    _schema(
        places={
            "type": "array",
            "items": _PLACE,
            "description": "The buttons, in order: a `label` and `at` or `find_phrase` each "
            "(with both, `find_phrase` is tried when `at` fails).",
        },
        mode={
            "enum": ["cursor", "select"],
            "description": "What a click does: `cursor` (default) puts the cursor at the "
            "place's start; `select` selects its paragraph.",
        },
    )
    | {"required": ["doc", "places"]},
)
def places(request: dict[str, Any]) -> dict[str, Any]:
    if not request["places"]:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "$.places: pass at least one place")
    for n, place in enumerate(request["places"]):
        if "at" not in place and "find_phrase" not in place:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID, f"$.places[{n}]: pass at or find_phrase"
            )
        if "find_phrase_across" in place and "find_phrase" not in place:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID, f"$.places[{n}]: find_phrase_across needs find_phrase"
            )
    return {
        "doc": request["doc"],
        "mode": request.get("mode", "cursor"),
        "places": request["places"],
    }


@operation(
    "live.get_selection",
    "read",
    (
        "Read what the user has selected in Word or LibreOffice right now, and the "
        "paragraph (with its address) the cursor is in. Use it when the user points at "
        'text ("this paragraph", "here"). Read-only; the document must be attached with '
        "live.open."
    ),
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
    """``doc.batch`` with ``engine: "live"``: comments and tracked edits in the open document.

    Addresses refer to the document as read, so the edits are applied from the end of
    the document backwards. Each one is logged to history (``engine: live``) without a
    backup: until the next save, taking one back is the app's own undo.

    ``tracked: false`` (D-21) writes the text edits plainly: the app's change tracking is
    off for each edit and put back as it was after. As on the file engine, comments are
    refused (``SCHEMA_INVALID``), and so is (``UNSUPPORTED_CONTENT``), before anything is
    written, an edit of a paragraph holding another unresolved tracked change, or an
    insertion next to one. Its log entries carry ``tracked: false`` and, as every live
    entry, no paragraph bytes: ``history.undo`` never matches them (the app's undo does).
    """
    from doc_control.ops.batch import edits_from_request  # batch imports this module too
    from doc_control.ops.tracked import story_of

    for i, raw in enumerate(request["ops"]):
        for key in ("at", "after", "before"):
            value = raw.get(key) if isinstance(raw, dict) else None
            if isinstance(value, str) and story_of(value):
                raise unavailable(
                    f"ops[{i}]: {value}: notes, headers and footers are file engine only",
                    op_index=i,
                )
    adapter, path = held(request)
    for guard in ("dry_run", "expect"):
        if request.get(guard):
            raise unavailable(f"{guard} is not supported live")
    ops = request["ops"]
    for i, raw in enumerate(ops):
        if raw.get("op") not in LIVE_KINDS:
            raise unavailable("operation not supported live", op_index=i, op=raw.get("op"))
    plain = request.get("tracked", True) is False
    if plain and any(raw.get("op") == "comment.add" for raw in ops):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, "tracked: false does not apply to comment operations"
        )
    author = format_author(
        request.get("author")
        or os.environ.get("DOC_CONTROL_LIVE_AUTHOR", "").strip()
        or default_author(),
        request.get("tag"),
    )
    count = len(guarded(adapter.paragraphs, path))
    # (start, end, index in the request, adapter call, where)
    plan: list[tuple[tuple[float, float], tuple[float, float], int, Any, Any]] = []
    revised: set[int] = set()  # paragraphs (from 0) a plain write may not touch
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
            if plain and guarded(adapter.unresolved, path, Span(max(after, 0), max(after, 0))):
                revised.add(max(after, 0))
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
        if plain and guarded(adapter.unresolved, path, span):
            revised.update(range(span.start, span.end + 1))
        call = adapter.comment if isinstance(edit, AddComment) else adapter.tracked_replace
        plan.append(((span.start, a), (span.end, inf if b is None else b), i, call, span))
    plan.sort(key=lambda step: (step[0], step[2]), reverse=True)
    for later, earlier in pairwise(plan):
        if earlier[1] > later[0]:
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                f"ops[{earlier[2]}] and ops[{later[2]}] overlap: send them as separate batches",
            )
    if revised:
        # The file engine's refusal (ops.plain), word for word: other consumers match it.
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            "a plain write cannot touch a paragraph that holds another unresolved tracked "
            f"change ({len(revised)} paragraph(s)); resolve it first or write tracked",
            paragraphs=[f"body #{n}" for n in sorted(revised)][:10],
        )
    store = DocStore.open(path)
    op_ids: dict[int, str] = {}
    extra: dict[str, Any] = {"tracked": False} if plain else {}
    # ponytail: not all-or-nothing. Every address and span is checked before the first
    # edit, so a batch should stop half-way only when the app goes away; wrap it in the
    # app's undo context if a writer is ever left with half a batch.
    for _, _, i, call, where in plan:
        # Only a plain write passes the flag: an adapter written before D-21 still works.
        guarded(call, path, where, ops[i]["text"], author, *((False,) if plain else ()))
        entry = append_entry(
            store,
            {"kind": "batch", "engine": "live", "author": author, "request": [ops[i]], **extra},
            None,
        )
        op_ids[i] = entry["op_id"]
    return {
        "engine": "live",
        "applied": len(plan),
        "author": author,
        **extra,
        "op_ids": [op_ids[i] for i in range(len(plan))],
    }
