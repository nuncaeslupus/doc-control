"""History operations: ``open``, ``undo``, ``restore`` and ``history.*``.

Everything here reads or writes the document's store (see :mod:`doc_control.safety.history`
for the log format). ``undo`` takes back a logged ``batch`` or ``restore`` exactly (the
paragraphs it changed return byte for byte, so text and paragraph properties are as they
were) and is itself logged; it is untracked, since it removes the tool's own tracked
changes. ``restore`` brings a logged paragraph back as a *tracked insertion* after the
paragraph that holds its place now, so the writer sees it and can reject it. ``history.purge``
removes history only after a dry run, and leaves a tombstone. Reading history never creates a
store, and works for a document that no longer exists (or, with ``store``, straight from a
store folder listed by ``history.orphans``).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.inprocess import limit as list_limit
from doc_control.model.document import Document
from doc_control.model.index import DocumentIndex
from doc_control.model.loosefold import LEVELS, Level
from doc_control.model.search import LevelMatcher, SearchOptions
from doc_control.model.wordcodes import translate_codes
from doc_control.ooxml.revisions import DEFAULT_AUTHOR
from doc_control.ooxml.splice import scan
from doc_control.ooxml.zipio import Package
from doc_control.ops.find import check_level
from doc_control.ops.tracked import InsertParagraph, apply_edits
from doc_control.registry import operation
from doc_control.safety import history
from doc_control.safety.history import (
    UNDOABLE_KINDS,
    Log,
    log_path_for,
    read_log,
    to_bytes,
    undo_entry,
)
from doc_control.safety.store import backup, store_dir
from doc_control.safety.verify import DOCUMENT_PART

_DOC: dict[str, Any] = {"type": "string", "minLength": 1, "description": "Path to the .docx."}
_STORE: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "A store folder (see history.orphans) to read instead of the document's own.",
}
_OP_ID: dict[str, Any] = {"type": "string", "minLength": 3, "description": "Op id, e.g. h-000123."}
_EXPECT: dict[str, Any] = {
    "type": "object",
    "properties": {"sha256": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
PREVIEW = 120


def _log(request: dict[str, Any]) -> Log:
    """The log named by ``store`` or ``doc``; reading creates nothing."""
    if "store" in request:
        return read_log(Path(request["store"]) / "log.jsonl")
    if "doc" in request:
        return read_log(log_path_for(Path(request["doc"])))
    raise DocControlError(ErrorCode.SCHEMA_INVALID, "give `doc` or `store`")


def _doc_reply(path: str, result: dict[str, Any]) -> Reply:
    return Reply(result, doc=Document(path).envelope_doc())


def _short(text: str) -> str:
    return text if len(text) <= PREVIEW else text[: PREVIEW - 1] + "…"


# -- open -----------------------------------------------------------------------------------


@operation(
    "open",
    "write",
    "Back up the document into its store (retention applies) and note it in the history; "
    "call it when a session starts. The document is not changed.",
    {
        "type": "object",
        "properties": {"doc": _DOC},
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def open_(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    if not path.is_file():
        raise DocControlError(ErrorCode.DOC_NOT_FOUND, f"no such file: {path}", path=str(path))
    sha = Package(path).sha256  # NOT_A_DOCX for anything else, before a store is made
    copy = backup(path)
    seen = [e for e in read_log(log_path_for(path)).entries if e.get("kind") != "purge"]
    op_id: str | None = None
    if not seen or seen[-1].get("sha256_after") != sha:  # new to the history: note it
        op_id = history.record(
            path, "open", before_sha256=sha, after_sha256=sha, backup=copy, changes=[]
        )
    return _doc_reply(
        str(path), {"backup": str(copy), "op_id": op_id, "store": str(store_dir(path))}
    )


# -- undo -----------------------------------------------------------------------------------


def _undoable(entry: dict[str, Any]) -> bool:
    return entry.get("kind") in UNDOABLE_KINDS and bool(entry.get("changes"))


@operation(
    "undo",
    "write",
    "Take back a logged batch or restore (the latest one that is still in effect by default), "
    "returning its paragraphs to their prior state. Refused if they were edited since.",
    {
        "type": "object",
        "properties": {
            "doc": _DOC,
            "op_id": {**_OP_ID, "description": "The op to undo (default: the latest in effect)."},
            "expect": _EXPECT,
            "dry_run": {"type": "boolean", "description": "Check and preview; write nothing."},
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def undo(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    log = read_log(log_path_for(path))
    undone = log.undone()
    op_id = request.get("op_id")
    if op_id is None:
        pending = [e for e in log.entries if _undoable(e) and e["op_id"] not in undone]
        if not pending:
            raise DocControlError(ErrorCode.HISTORY_NOT_FOUND, "nothing to undo")
        entry = pending[-1]
    else:
        entry = log.get(op_id)
        if not _undoable(entry):
            raise DocControlError(
                ErrorCode.HISTORY_NOT_FOUND,
                f"{op_id} is a {entry.get('kind')} entry; only batch and restore can be undone",
                op_id=op_id,
            )
        if op_id in undone:
            raise DocControlError(
                ErrorCode.HISTORY_NOT_FOUND, f"{op_id} was already undone", op_id=op_id
            )
    dry_run = request.get("dry_run", False)
    outcome = undo_entry(
        path,
        entry,
        expect_sha256=request.get("expect", {}).get("sha256"),
        dry_run=dry_run,
        env=None,
    )
    warnings: list[str] = []
    result: dict[str, Any] = {
        "undid": entry["op_id"],
        "dry_run": dry_run,
        "paragraphs": len(outcome.reverted),
    }
    if not dry_run:
        assert outcome.after_sha256 is not None
        try:
            result["op_id"] = history.record(
                path,
                "undo",
                before_sha256=outcome.before_sha256,
                after_sha256=outcome.after_sha256,
                backup=outcome.backup,
                changes=outcome.reverted,
                request={"op": "undo", "op_id": entry["op_id"]},
                extra={"undoes": entry["op_id"]},
            )
        except (DocControlError, OSError) as exc:
            result["op_id"] = None
            warnings.append(
                f"HISTORY_NOT_RECORDED: the undo succeeded but could not be logged: {exc}"
            )
        result["sha256"] = outcome.after_sha256
        result["backup"] = str(outcome.backup)
    return Reply(result, doc=Document(str(path)).envelope_doc(), warnings=warnings)


# -- restore --------------------------------------------------------------------------------


def _restorable(change: dict[str, Any]) -> bool:
    return change.get("kind") == "replace" and bool(change.get("text_before"))


@operation(
    "restore",
    "write",
    "Bring a logged paragraph back as a tracked insertion after the paragraph that holds its "
    "place now. Without `change`, every paragraph the op deleted.",
    {
        "type": "object",
        "properties": {
            "doc": _DOC,
            "store": _STORE,
            "op_id": {**_OP_ID, "description": "The logged op the paragraph was changed by."},
            "change": {
                "type": "integer",
                "minimum": 0,
                "description": "Index of one change of the op (history.list shows them).",
            },
            "after": {
                "type": "string",
                "minLength": 1,
                "description": "Insert after this paragraph address instead of the old place "
                "(needed when that place was edited, or the history comes from `store`).",
            },
            "expect": _EXPECT,
            "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
            "author": {"type": "string", "minLength": 1, "description": "Revision author."},
            "tag": {"type": "string", "minLength": 1, "description": "Appended to the author."},
        },
        "required": ["doc", "op_id"],
        "additionalProperties": False,
    },
)
def restore(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    entry = _log(request).get(request["op_id"])
    changes = entry.get("changes", [])
    if "change" in request:
        number = request["change"]
        if not 0 <= number < len(changes) or not _restorable(changes[number]):
            raise DocControlError(
                ErrorCode.HISTORY_NOT_FOUND,
                f"{entry['op_id']} has no restorable paragraph at change {number}",
                op_id=entry["op_id"],
            )
        chosen = [number]
    else:
        chosen = [n for n, c in enumerate(changes) if _restorable(c) and c.get("deleted")]
    if not chosen:
        raise DocControlError(
            ErrorCode.HISTORY_NOT_FOUND,
            f"{entry['op_id']} deleted no paragraph; pass `change` to restore an edited one",
            op_id=entry["op_id"],
        )
    if "after" in request and len(chosen) != 1:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "`after` needs exactly one `change`")
    pkg = Package(path) if path.is_file() else None
    if pkg is None:
        raise DocControlError(ErrorCode.DOC_NOT_FOUND, f"no such file: {path}", path=str(path))
    anchors = _anchors(pkg, changes, chosen, request)
    edits = [InsertParagraph(changes[n]["text_before"], after=anchors[n]) for n in chosen]
    dry_run = request.get("dry_run", False)
    done = apply_edits(
        path,
        edits,
        expect_sha256=request.get("expect", {}).get("sha256"),
        author=request.get("author", DEFAULT_AUTHOR),
        tag=request.get("tag"),
        dry_run=dry_run,
        kind="restore",
        request={"op": "restore", "op_id": entry["op_id"], "changes": chosen},
        log_extra={"restores": entry["op_id"]},
    )
    result = done.to_dict()
    result["restored"] = [
        {"change": n, "after": anchors[n], "text": _short(changes[n]["text_before"])}
        for n in chosen
    ]
    result["from"] = entry["op_id"]
    return _doc_reply(str(path), result)


def _anchors(
    pkg: Package, changes: list[dict[str, Any]], chosen: list[int], request: dict[str, Any]
) -> dict[int, str]:
    """For each chosen change, the address of the paragraph to insert after."""
    if "after" in request:
        return {chosen[0]: request["after"]}
    data = pkg.read(DOCUMENT_PART)
    spans = scan(data).paragraphs
    index = DocumentIndex.from_package(pkg)
    by_span = {p.span_index: p for p in index.paragraphs}
    out: dict[int, str] = {}
    for n in chosen:
        target = to_bytes(changes[n]["after"])
        found = [i for i, s in enumerate(spans) if data[s.start : s.end] == target]
        para = by_span.get(found[0]) if len(found) == 1 else None
        if para is None:
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                f"the place of change {n} was edited since; pass `after` with an address",
                change=n,
            )
        out[n] = str(para.address)
    return out


# -- history.* ------------------------------------------------------------------------------

_READ_PROPS: dict[str, Any] = {"doc": _DOC, "store": _STORE}


def _summary(entry: dict[str, Any], undone: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "op_id": entry["op_id"],
        "kind": entry.get("kind"),
        "time": entry.get("time"),
    }
    if entry.get("kind") == "purge":
        return {**out, "removed": entry.get("removed"), "scope": entry.get("scope")}
    out.update(author=entry.get("author"), tag=entry.get("tag"))
    changes = entry.get("changes", [])
    out["paragraphs"] = len(changes)
    request = entry.get("request")
    if isinstance(request, list):
        out["ops"] = [r.get("op") for r in request if isinstance(r, dict)]
    if entry.get("kind") in UNDOABLE_KINDS:
        out["undone"] = entry["op_id"] in undone
        out["deleted"] = [
            {"change": n, "text": _short(c["text_before"])}
            for n, c in enumerate(changes)
            if _restorable(c) and c.get("deleted")
        ][:5]
    for key in ("undoes", "restores", "sha256_after", "backup", "engine"):
        if entry.get(key) is not None:
            out[key] = entry[key]
    return out


@operation(
    "history.list",
    "read",
    (
        "List a document's undo log, newest first: each write's op_id, what it changed, "
        "whether it was undone, and the deleted paragraphs restore can bring back. Use it "
        "to find the op_id for undo or restore; history.search finds a quote in the log. "
        "Read-only, paged."
    ),
    {
        "type": "object",
        "properties": {
            **_READ_PROPS,
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
            "cursor": {**_OP_ID, "description": "The `next` of the previous response."},
            "kind": {"enum": ["batch", "restore", "undo", "open", "purge"]},
        },
        "additionalProperties": False,
    },
)
def history_list(request: dict[str, Any]) -> Reply:
    log = _log(request)
    undone = log.undone()
    kind = request.get("kind")
    rows = [e for e in reversed(log.entries) if kind is None or e.get("kind") == kind]
    cursor = request.get("cursor")
    if cursor is not None and history.op_number(cursor) is None:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"bad cursor: {cursor!r}", path="$.cursor")
    if cursor is not None:
        rows = [
            e
            for e in rows
            if (history.op_number(e["op_id"]) or 0) < (history.op_number(cursor) or 0)
        ]
    limit = list_limit(request, DEFAULT_LIMIT)
    page = rows[:limit]
    result: dict[str, Any] = {
        "entries": [_summary(e, undone) for e in page],
        "total": len(rows),
    }
    if log.skipped:
        result["unreadable_lines"] = log.skipped
    more = len(rows) > limit
    return Reply(result, next_cursor=page[-1]["op_id"] if more and page else None)


@operation(
    "history.search",
    "read",
    (
        "Search a document's undo log for a quote: in the requests, and in the text of "
        "every paragraph before and after each write. Use it to find when a passage was "
        "changed or deleted, and the op_id to undo or restore it. Exact unless "
        "`match_level` folds it; then every hit, with its offsets and the level it "
        "matched at. Read-only."
    ),
    {
        "type": "object",
        "properties": {
            **_READ_PROPS,
            "query": {"type": "string", "minLength": 1, "description": "Text to find."},
            "ignore_case": {"type": "boolean"},
            "ignore_accents": {"type": "boolean"},
            "match_level": {
                "type": "string",
                "enum": ["exact", "default", "loose"],
                "description": "Fold the search as `find` does: `default` ignores case,"
                " accents and punctuation (symbols such as `$` still count), `loose` drops"
                " symbols too; `exact` keeps the flags. When passed, Word's Find codes in"
                " `query` are read as their characters, every hit in a text is returned"
                " (not only the first, `exact` included), each with `start` and `end` (its"
                " offsets in that text: the request's JSON or the paragraph before or after"
                " the write, as `in` names it; not offsets into the cut `text`),"
                " `match_level` (the strictest level it meets) and `text` cut around"
                " it, and the reply gains `matches_by_level`, adding up to `total`. Without"
                " it, the first hit per text, with no `start`, `end` or level. Not"
                " with `ignore_case` or `ignore_accents` false at a folded level.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def history_search(request: dict[str, Any]) -> Reply:
    level: Level | None = request.get("match_level")
    folds = level in ("default", "loose")
    query: str = request["query"]
    if level is not None:
        check_level(request, level)
        query = translate_codes(query)
        LevelMatcher(SearchOptions(query), level)  # a bad phrase fails here, not per entry
    log = _log(request)
    limit = list_limit(request, DEFAULT_LIMIT)
    hits: list[dict[str, Any]] = []
    total = 0
    by_level: dict[str, int] = {}
    for entry in reversed(log.entries):
        if entry.get("kind") == "purge":
            continue
        found = history.search_entry(
            entry,
            query,
            ignore_case=folds or request.get("ignore_case", False),
            ignore_accents=folds or request.get("ignore_accents", False),
            match_level=level,
        )
        total += len(found)
        for hit in found if level is not None else ():
            by_level[hit["match_level"]] = by_level.get(hit["match_level"], 0) + 1
        hits.extend(found[: max(0, limit - len(hits))])
    result: dict[str, Any] = {"matches": hits, "total": total}
    if level is not None:
        result["matches_by_level"] = {k: by_level[k] for k in LEVELS if k in by_level}
    return Reply(result)


@operation(
    "history.purge",
    "write",
    "Remove undo history (all, or before a date) for privacy, leaving a tombstone. Run with "
    "dry_run first, then repeat with its `confirm`.",
    {
        "type": "object",
        "properties": {
            **_READ_PROPS,
            "all": {"type": "boolean", "description": "Remove the whole log."},
            "before": {
                "type": "string",
                "minLength": 1,
                "description": "Remove entries older than this ISO date or time (UTC).",
            },
            "dry_run": {"type": "boolean", "description": "List what would go; returns confirm."},
            "confirm": {"type": "string", "minLength": 1, "description": "From the dry run."},
        },
        "additionalProperties": False,
    },
)
def history_purge(request: dict[str, Any]) -> Reply:
    if "store" in request:
        log_path = Path(request["store"]) / "log.jsonl"
    elif "doc" in request:
        log_path = log_path_for(Path(request["doc"]))
    else:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "give `doc` or `store`")
    before = _parse_when(request["before"]) if "before" in request else None
    result = history.purge(
        log_path,
        before=before,
        everything=request.get("all", False),
        dry_run=request.get("dry_run", False),
        confirm=request.get("confirm"),
    )
    return Reply(result)


def _parse_when(text: str) -> datetime:
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f"`before` is not an ISO date: {text!r}", path="$.before"
        ) from exc
    return when if when.tzinfo else when.replace(tzinfo=UTC)


@operation(
    "history.orphans",
    "read",
    "List stores whose document is gone (app-data stores, and `dir`/.doc-control). Their "
    "history is never deleted automatically: restore from it with `store`, or purge it.",
    {
        "type": "object",
        "properties": {
            "dir": {"type": "string", "minLength": 1, "description": "Also scan this folder."}
        },
        "additionalProperties": False,
    },
)
def history_orphans(request: dict[str, Any]) -> Reply:
    folder = Path(request["dir"]) if "dir" in request else None
    found = history.find_orphans(folder, os.environ)
    return Reply({**found, "total": len(found["orphans"])})
