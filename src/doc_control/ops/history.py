"""History operations: ``doc.open``, ``history.undo``, ``history.restore`` and ``history.*``.

Everything here reads or writes the document's store (see :mod:`doc_control.safety.history`
for the log format). ``history.undo`` takes back a logged ``doc.batch`` or
``history.restore`` exactly (the paragraphs it changed return byte for byte, so text and
paragraph properties are as they were) and is itself logged; it is untracked, since it
removes the tool's own tracked changes. ``history.restore`` brings a logged paragraph back
as a *tracked insertion* after the paragraph that holds its place now, so the writer sees it
and can reject it. ``history.purge`` removes history only after a dry run, and leaves a
tombstone. Reading history never creates a store, and works for a document that no longer
exists (or, with ``store``, straight from a store folder listed by ``history.orphans``).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doc_control import outputs
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
    "description": (
        "Write guard: the sha256 read earlier (doc.info, or a reply's doc.sha256), "
        'e.g. {"sha256": "9f86d081…"}; a file changed since is DOC_CHANGED_ON_DISK.'
    ),
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


def _adoptable(path: Path, sha: str) -> list[dict[str, Any]]:
    """The orphan stores ``doc.open`` offers; a scan that cannot read a folder offers none
    rather than failing the open."""
    try:
        return history.adoptable_stores(path, sha=sha)
    except (OSError, DocControlError):
        return []


_STORE_ROW: dict[str, Any] = {
    "store": outputs.string("Store folder."),
    "doc": outputs.nullable("string", "Path of the document it last recorded."),
    "doc_id": outputs.string("Id of that document's history."),
    "entries": outputs.integer("Log entries."),
    "last_time": outputs.nullable("string", "Time of the last entry (ISO 8601)."),
}
_ADOPTABLE = outputs.arr(
    "Orphan stores whose history this document could take back (history.adopt).",
    outputs.obj(
        "An orphan store.",
        **_STORE_ROW,
        sha_matched=outputs.boolean("Its last recorded state is this file."),
    ),
)
_OPEN_OUTPUT = outputs.result(
    ("backup", "op_id", "store"),
    backup=outputs.string("Path of the backup just taken."),
    op_id=outputs.nullable(
        "string", "Log entry noting the open, or null if the file is as last logged."
    ),
    store=outputs.string("The document's store folder (backups and log)."),
    adoptable=_ADOPTABLE,
    hint=outputs.string("What to do with adoptable."),
)


@operation(
    "doc.open",
    "write",
    (
        "Start a session on a document: back it up into its store (.doc-control/<file name>/ next "
        "to it; the newest 20 backups or 30 days are kept) and note it in the undo log if it "
        "changed since the last logged write. Call it once before editing; the document itself is "
        "not changed. If the document has no history yet, `adoptable` lists orphaned stores (it "
        "was moved or renamed) that history.adopt can re-attach. Returns the backup path, the log "
        "entry and the store folder."
    ),
    {
        "type": "object",
        "properties": {"doc": _DOC},
        "required": ["doc"],
        "additionalProperties": False,
    },
    output=_OPEN_OUTPUT,
)
def open_(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    if not path.is_file():
        raise DocControlError(ErrorCode.DOC_NOT_FOUND, f"no such file: {path}", path=str(path))
    sha = Package(path).sha256  # NOT_A_DOCX for anything else, before a store is made
    adoptable = [] if log_path_for(path).is_file() else _adoptable(path, sha)
    copy = backup(path)
    seen = [e for e in read_log(log_path_for(path)).entries if e.get("kind") != "purge"]
    op_id: str | None = None
    if not seen or seen[-1].get("sha256_after") != sha:  # new to the history: note it
        op_id = history.record(
            path, "open", before_sha256=sha, after_sha256=sha, backup=copy, changes=[]
        )
    result: dict[str, Any] = {"backup": str(copy), "op_id": op_id, "store": str(store_dir(path))}
    if adoptable:
        result["adoptable"] = adoptable
        result["hint"] = "history.adopt re-attaches the history of a moved or renamed document"
    return _doc_reply(str(path), result)


# -- undo -----------------------------------------------------------------------------------


def _undoable(entry: dict[str, Any]) -> bool:
    return entry.get("kind") in UNDOABLE_KINDS and bool(entry.get("changes"))


_UNDO_OUTPUT = outputs.result(
    ("undid", "dry_run", "paragraphs"),
    undid=outputs.string("Op id taken back."),
    dry_run=outputs.DRY_RUN,
    paragraphs=outputs.integer("Paragraphs returned to their prior state."),
    **outputs.COMMIT,
)


@operation(
    "history.undo",
    "write",
    (
        "Take back one logged write (doc.batch, text.replace and the other direct writes, "
        "term.rename, field and note inserts, layout.apply, history.restore), returning its "
        "paragraphs to their state before it: by default the latest one still in effect, or the "
        "`op_id` from history.list. Comment and style writes cannot be undone. Refused with "
        "BATCH_CONFLICT if a paragraph it changed was edited since (undo the later write first). "
        "Use history.restore to bring back single deleted paragraphs, revision.reject to drop "
        "tracked changes by author or id. The undo is itself logged; dry_run checks without "
        "writing; expect.sha256 refuses a changed file."
    ),
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
    output=_UNDO_OUTPUT,
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
                request={"op": "history.undo", "op_id": entry["op_id"]},
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


_RESTORE_OUTPUT = outputs.result(
    ("applied", "dry_run", "restored", "from"),
    **outputs.BATCH_FIELDS,
    restored=outputs.arr(
        "The paragraphs brought back.",
        outputs.obj(
            "A restored paragraph.",
            change=outputs.integer("Change number in the logged op."),
            after=outputs.string("Address it was inserted after."),
            text=outputs.string("Its text (clipped)."),
        ),
    ),
    **{"from": outputs.string("The logged op restored from.")},
)


@operation(
    "history.restore",
    "write",
    (
        "Bring back paragraphs a logged write deleted or changed, as a tracked insertion (tracked: "
        "false: plainly, for a change the writer approved). Give the `op_id` (from history.list or "
        "history.search); without `change`, every paragraph that op deleted comes back after the "
        "paragraph now holding its old place; `change` picks one (also an edited one) and `after` "
        "(p12#a1b2c3d4) places it when that place was edited (BATCH_CONFLICT otherwise). Use "
        "history.undo to revert a whole write instead. Backed up, logged, undoable; dry_run "
        "previews."
    ),
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
            "tracked": {
                "type": "boolean",
                "default": True,
                "description": "false: a plain write for a change the writer already approved: "
                "the paragraph comes back without revision marks (this call's own insertion "
                "accepted in the same verified write).",
            },
        },
        "required": ["doc", "op_id"],
        "additionalProperties": False,
    },
    output=_RESTORE_OUTPUT,
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
        tracked=request.get("tracked", True) is not False,
        kind="restore",
        request={"op": "history.restore", "op_id": entry["op_id"], "changes": chosen},
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
    for key in (
        "undoes",
        "restores",
        "sha256_after",
        "backup",
        "engine",
        "tracked",
        "adopted_from",
    ):
        if entry.get(key) is not None:
            out[key] = entry[key]
    return out


_LIST_OUTPUT = outputs.result(
    ("entries", "total"),
    entries=outputs.arr(
        "This page of log entries, newest first.",
        outputs.obj(
            "A logged write.",
            op_id=outputs.string("Op id (history.undo / history.restore)."),
            kind=outputs.nullable("string", "batch, restore, undo, open, purge or adopt."),
            time=outputs.nullable("string", "When (ISO 8601)."),
            author=outputs.nullable("string", "Revision author."),
            tag=outputs.nullable("string", "Author tag."),
            paragraphs=outputs.integer("Paragraphs it changed."),
            ops=outputs.strings("Operation kinds in the request."),
            undone=outputs.boolean("Whether it was taken back."),
            deleted=outputs.arr(
                "Paragraphs it deleted that history.restore can bring back (first 5).",
                outputs.obj(
                    "A deleted paragraph.",
                    change=outputs.integer("Change number."),
                    text=outputs.string("Text (clipped)."),
                ),
            ),
            undoes=outputs.string("Op id it undid."),
            restores=outputs.string("Op id it restored from."),
            sha256_after=outputs.string("File hash after it."),
            backup=outputs.string("Backup taken before it."),
            engine=outputs.string("live for a live-engine edit."),
            tracked=outputs.boolean("False for a plain write."),
            adopted_from=outputs.mapping("adopt: the store and document it came from."),
            removed=outputs.nullable("integer", "purge: entries removed."),
            scope=outputs.nullable("string", "purge: what was removed."),
        ),
    ),
    total=outputs.integer("Entries in the log (after the kind filter)."),
    unreadable_lines=outputs.integer("Log lines that could not be read."),
)


@operation(
    "history.list",
    "read",
    (
        "List the document's undo log, newest first: each write's op_id, kind, time, author, "
        "the operations it ran, whether it was undone, and the deleted paragraphs "
        "history.restore can bring back (change numbers). Filter with `kind` (batch, restore, "
        "undo, open, purge, adopt); `store` reads an orphaned store instead. Use it to find "
        "the op_id for history.undo or history.restore; history.search finds a quote in the "
        "log. Read-only; paged with limit (default 20) and `cursor` (the reply's `next`)."
    ),
    {
        "type": "object",
        "properties": {
            **_READ_PROPS,
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "description": "Most entries per page (default 20).",
            },
            "cursor": {**_OP_ID, "description": "The `next` of the previous response."},
            "kind": {
                "enum": ["batch", "restore", "undo", "open", "purge", "adopt"],
                "description": "Only entries of this kind (batch: every logged edit).",
            },
        },
        "additionalProperties": False,
    },
    output=_LIST_OUTPUT,
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


_SEARCH_OUTPUT = outputs.result(
    ("matches", "total"),
    matches=outputs.arr(
        "Hits, newest entry first (up to limit).",
        outputs.obj(
            "A hit.",
            op_id=outputs.string("The logged op it is in."),
            text=outputs.string("The text around the hit."),
            start=outputs.integer("With match_level: start of the hit in the text."),
            end=outputs.integer("With match_level: end of the hit."),
            match_level=outputs.string("With match_level: the level it matched at."),
            **{"in": outputs.string("Where: request, or change <n> before/after.")},
        ),
    ),
    total=outputs.integer("Hits in the whole log."),
    matches_by_level=outputs.mapping("With match_level: hits per level."),
)


@operation(
    "history.search",
    "read",
    (
        "Search the undo log for a quote: in each logged request and in the text of every "
        "paragraph before and after each write, newest first. Use it to find when a passage "
        "was changed or deleted, and the op_id for history.undo or history.restore; text.find "
        "searches the current document instead. Exact by default (ignore_case, ignore_accents "
        "loosen it); match_level default or loose folds case, accents and punctuation and "
        "returns each hit's offsets and level. Works on an orphaned `store` too. Read-only; "
        "limit (default 20) caps hits, `total` counts all."
    ),
    {
        "type": "object",
        "properties": {
            **_READ_PROPS,
            "query": {"type": "string", "minLength": 1, "description": "Text to find."},
            "ignore_case": {"type": "boolean", "description": "Ignore case."},
            "ignore_accents": {
                "type": "boolean",
                "description": "Ignore accents: `maria` finds `María`.",
            },
            "match_level": {
                "type": "string",
                "enum": ["exact", "default", "loose"],
                "description": "Fold the search as `text.find` does: `default` ignores case,"
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
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "description": "Most hits to return (default 20); `total` counts them all.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output=_SEARCH_OUTPUT,
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


_PURGE_OUTPUT = outputs.result(
    ("dry_run", "scope", "removed", "would_remove", "confirm"),
    dry_run=outputs.DRY_RUN,
    scope=outputs.string("What is removed: all, or before <time>."),
    removed=outputs.integer("Entries removed (0 on a dry run)."),
    would_remove=outputs.integer("Entries the scope covers."),
    first_op_id=outputs.nullable("string", "Oldest entry removed."),
    last_op_id=outputs.nullable("string", "Newest entry removed."),
    confirm=outputs.string("Token to pass as confirm to really purge."),
    tombstone=outputs.string("Op id of the purge entry left in the log."),
)


@operation(
    "history.purge",
    "write",
    (
        "Delete undo history for privacy: the whole log (`all`) or entries before a date "
        "(`before`, ISO 8601, UTC), of the document or of an orphaned `store`. Two steps: run with "
        "dry_run to see what would go and get a `confirm` token, then repeat with `confirm`; a log "
        "that changed in between is refused. Leaves a tombstone entry; backups are not removed by "
        "it. Destructive: purged entries can no longer be undone or restored. Use history.list "
        "first to check what you remove."
    ),
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
    output=_PURGE_OUTPUT,
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


_ORPHANS_OUTPUT = outputs.result(
    ("orphans", "unknown", "total"),
    orphans=outputs.arr("Stores whose document is gone.", outputs.obj("A store.", **_STORE_ROW)),
    unknown=outputs.arr("Stores that name no document.", outputs.obj("A store.", **_STORE_ROW)),
    total=outputs.integer("Orphans found."),
)


@operation(
    "history.orphans",
    "read",
    (
        "List undo stores whose document is gone (moved, renamed or deleted): the app-data stores, "
        "plus `dir`/.doc-control when `dir` is given, each with its folder, last document path, "
        "entry count and last time; `unknown` holds stores that name no document. Their history is "
        "never deleted automatically. Use history.adopt to re-attach one to a moved document, "
        "history.list or history.restore with `store` to read from it, history.purge with `store` "
        "to delete it. Read-only."
    ),
    {
        "type": "object",
        "properties": {
            "dir": {"type": "string", "minLength": 1, "description": "Also scan this folder."}
        },
        "additionalProperties": False,
    },
    output=_ORPHANS_OUTPUT,
)
def history_orphans(request: dict[str, Any]) -> Reply:
    folder = Path(request["dir"]) if "dir" in request else None
    found = history.find_orphans(folder, os.environ)
    return Reply({**found, "total": len(found["orphans"])})


_ADOPT_OUTPUT = outputs.result(
    ("adopted",),
    adopted=outputs.boolean("Whether the store was moved to the document."),
    dry_run=outputs.DRY_RUN,
    sha_matched=outputs.boolean("The file is the store's last recorded state."),
    store=outputs.string("Where the store lives now (or would)."),
    entries=outputs.integer("Log entries it holds."),
    op_id=outputs.nullable("string", "Log entry noting the adoption."),
    would_move=outputs.string("dry_run: the store that would move."),
    candidates={
        **_ADOPTABLE,
        "description": "When several stores match: pass one as `store`; nothing moved.",
    },
    **{"from": outputs.string("The orphan store adopted.")},
)


@operation(
    "history.adopt",
    "write",
    (
        "Re-attach a moved or renamed document to its old undo history, so history.undo and "
        "history.restore work again. It finds the orphaned store whose last recorded state is "
        "this file's sha256 (doc.open lists them as `adoptable`, history.orphans lists all) "
        "and moves it, backups and log, to where this document's store lives now. Pass `store` "
        "when several match (the reply lists `candidates`); `force` adopts a file edited "
        "since; a document that already has history is never overwritten (BATCH_CONFLICT). "
        "dry_run moves nothing."
    ),
    {
        "type": "object",
        "properties": {
            "doc": _DOC,
            "store": {
                **_STORE,
                "description": "The orphan store to adopt (see doc.open's `adoptable` or "
                "history.orphans); needed when several match or the old folder is far away.",
            },
            "force": {
                "type": "boolean",
                "description": "Adopt although the document's sha256 is not the store's last.",
            },
            "dry_run": {"type": "boolean", "description": "Say what would move; move nothing."},
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
    output=_ADOPT_OUTPUT,
)
def history_adopt(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    if not path.is_file():
        raise DocControlError(ErrorCode.DOC_NOT_FOUND, f"no such file: {path}", path=str(path))
    Package(path)  # NOT_A_DOCX for anything else
    force = request.get("force", False)
    dry_run = request.get("dry_run", False)
    if "store" in request:
        chosen = Path(request["store"])
    else:
        found = history.adoptable_stores(path, anything=force)
        if not found:
            raise DocControlError(
                ErrorCode.HISTORY_NOT_FOUND,
                f"no orphan store matches {path.name}; pass `store` (see history.orphans), "
                "with `force` if the file was edited since",
            )
        if len(found) > 1:
            return _doc_reply(str(path), {"adopted": False, "candidates": found})
        chosen = Path(found[0]["store"])
    result = history.adopt_store(path, chosen, force=force, dry_run=dry_run)
    return _doc_reply(str(path), result)
