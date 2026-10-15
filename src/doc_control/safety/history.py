"""The permanent undo log: ``log.jsonl`` in the document store, and undo on top of it.

Each write appends one JSON line (``"log_version": 1``; readers ignore unknown keys):
``op_id`` (``h-000123``, increasing, never reused, not even after a purge), ``time``,
``kind`` (``batch``, ``restore``, ``undo``, ``open``, ``purge``), author, tag, the
request, the sha256 of the document before and after, the backup taken for it and
``changes``: the exact bytes of every touched paragraph before and after, as text
(``surrogateescape``, so any byte string round-trips through JSON).

``history.undo`` swaps those bytes back: every logged "after" paragraph must still be in the
document exactly once, byte for byte, or the undo is refused (``BATCH_CONFLICT``, naming
the changes), so it can never overwrite a later edit. The result goes through
:func:`write_verified` like any other write.

Retention never touches the log. Only :func:`purge` removes lines, after a dry run
that returned a confirmation token, and it leaves a tombstone line saying what went.
Concurrent writers to one store are not serialised: the lock check and the document
replace already make a second writer fail, so two entries racing for one id are not
expected in practice.

A table row is not a paragraph: a row operation logs a change of kind ``span`` whose
``before`` and ``after`` are the whole ``w:tr`` (``before`` empty for an inserted row); undo
swaps the one place the ``after`` row still stands in ``word/document.xml``.

A change in a note, header or footer carries ``part`` and ``story`` (its address prefix,
see :mod:`doc_control.model.stories`); undo finds its paragraph in that story and
restores every part an entry touched in one verified write.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
import re
import shutil
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.loosefold import Level
from doc_control.model.search import LevelMatcher, SearchOptions
from doc_control.ooxml.splice import BODY, Container, scan
from doc_control.ooxml.zipio import Package, commit_bytes, sha256_file
from doc_control.safety import guard_write
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.store import (
    STORE_DIRNAME,
    DocStore,
    _write_text_atomic,
    custom_store_root,
    store_dir,
    user_home,
)
from doc_control.safety.verify import DOCUMENT_PART, Footprint, StoryFootprint, write_verified

LOG_VERSION = 1
UNDOABLE_KINDS = ("batch", "restore")
_ID = re.compile(r"h-(\d{6,})")
_LIVE_TEXT = re.compile(rb"<(?:[\w.-]+:)?t[\s>]")
_XML_TEXT = re.compile(r"<(?:[\w.-]+:)?(t|delText)(?:\s[^>]*)?>([^<]*)</(?:[\w.-]+:)?\1>")


def to_text(data: bytes) -> str:
    """Bytes to a JSON-safe string that :func:`to_bytes` turns back into the same bytes."""
    return data.decode("utf-8", "surrogateescape")


def to_bytes(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def op_id_of(number: int) -> str:
    return f"h-{number:06d}"


def op_number(op_id: object) -> int | None:
    m = _ID.fullmatch(op_id) if isinstance(op_id, str) else None
    return int(m[1]) if m else None


def has_live_text(paragraph_xml: bytes) -> bool:
    """True if the paragraph holds ``w:t`` text (so it is not wholly deleted)."""
    return _LIVE_TEXT.search(paragraph_xml) is not None


def xml_text(paragraph_xml: str) -> str:
    """All text of a logged paragraph, inserted and deleted, for searching."""
    return "".join(unescape(m[2]) for m in _XML_TEXT.finditer(paragraph_xml))


@dataclass(frozen=True)
class Log:
    path: Path
    entries: list[dict[str, Any]]
    skipped: int = 0  # unreadable lines (a write cut short); kept in the file, ignored here

    def get(self, op_id: str) -> dict[str, Any]:
        for entry in self.entries:
            if entry["op_id"] == op_id:
                return entry
        raise DocControlError(
            ErrorCode.HISTORY_NOT_FOUND,
            f"no history entry {op_id}",
            op_id=op_id,
            path=str(self.path),
        )

    def undone(self) -> set[str]:
        return {e["undoes"] for e in self.entries if e.get("kind") == "undo" and "undoes" in e}

    def next_number(self) -> int:
        numbers = [n for e in self.entries if (n := op_number(e["op_id"])) is not None]
        floor = [int(e.get("last_removed", 0)) for e in self.entries if e.get("kind") == "purge"]
        return max([0, *numbers, *floor]) + 1


def read_log(path: Path) -> Log:
    """The entries of ``path``, oldest first; absent or unreadable lines are skipped."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Log(path, [])
    except OSError as exc:
        raise DocControlError(
            ErrorCode.INTERNAL, f"cannot read the history log {path}: {exc}", path=str(path)
        ) from exc
    entries: list[dict[str, Any]] = []
    skipped = 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except ValueError:
            skipped += 1
            continue
        if isinstance(value, dict) and op_number(value.get("op_id")) is not None:
            entries.append(value)
        else:
            skipped += 1
    return Log(path, entries, skipped)


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=UTC)


def _stamp(when: datetime | None) -> str:
    return _now(when).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(entry: Mapping[str, Any]) -> bytes:
    return (json.dumps(entry, separators=(",", ":")) + "\n").encode("ascii")


def append_entry(
    store: DocStore, fields: Mapping[str, Any], now: datetime | None
) -> dict[str, Any]:
    """Append one entry with the next op id; returns the entry as written."""
    log = read_log(store.log_path)
    entry: dict[str, Any] = {
        "log_version": LOG_VERSION,
        "op_id": op_id_of(log.next_number()),
        "time": _stamp(now),
        "doc_id": store.doc_id,
        **fields,
    }
    data = _line(entry)
    try:
        with store.log_path.open("ab+") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell():
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    data = b"\n" + data  # a torn last line must not swallow this one
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise DocControlError(
            ErrorCode.INTERNAL,
            f"cannot append to the history log {store.log_path}: {exc}",
            path=str(store.log_path),
        ) from exc
    return entry


def record(
    doc_path: Path,
    kind: str,
    *,
    before_sha256: str,
    after_sha256: str,
    backup: Path | None,
    changes: Sequence[Mapping[str, Any]],
    request: Any = None,
    author: str | None = None,
    tag: str | None = None,
    footprint: Any = None,
    extra: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Log one write of ``doc_path``; returns its op id."""
    store = DocStore.open(doc_path, env)
    fields: dict[str, Any] = {
        "kind": kind,
        "doc_path": str(doc_path.resolve()),
        "author": author,
        "tag": tag,
        "request": request,
        "footprint": footprint,
        "sha256_before": before_sha256,
        "sha256_after": after_sha256,
        "backup": backup.name if backup else None,
        "changes": list(changes),
        **(extra or {}),
    }
    return str(append_entry(store, fields, now)["op_id"])


# -- undo -----------------------------------------------------------------------------------


def _container(pkg: Package, story: str, part: str) -> Container:
    from doc_control.model.stories import find_story

    try:
        found = find_story(pkg, story)
    except DocControlError as exc:
        if exc.code is not ErrorCode.ANCHOR_NOT_FOUND:
            raise  # a malformed part stays what it is
        raise DocControlError(
            ErrorCode.BATCH_CONFLICT,
            f"{story} is gone since; undo later operations first (history.list)",
            story=story,
        ) from exc
    if found.part != part:
        raise DocControlError(
            ErrorCode.BATCH_CONFLICT,
            f"{story} is now in {found.part}, not {part}; undo later operations first",
            story=story,
        )
    return found.container


@dataclass(frozen=True)
class UndoOutcome:
    before_sha256: str
    after_sha256: str | None
    backup: Path | None
    reverted: list[dict[str, Any]] = field(default_factory=list)


def _conflict(entry: Mapping[str, Any], missing: list[int], why: str) -> DocControlError:
    return DocControlError(
        ErrorCode.BATCH_CONFLICT,
        f"cannot undo {entry['op_id']}: {why}; undo later operations first (history.list)",
        op_id=entry["op_id"],
        changes=missing,
    )


def undo_entry(
    doc_path: Path,
    entry: Mapping[str, Any],
    *,
    expect_sha256: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    stage: Callable[[str], None] | None = None,
) -> UndoOutcome:
    """Put every paragraph ``entry`` changed back as it was, in one verified write.

    Entries that also wrote other parts (comments) are refused: only paragraphs are logged.
    An entry marked ``undo_by_backup`` (``note.add``, which may create a part) is undone by
    putting its backup back, but only while the file is exactly what that entry wrote.
    """
    if entry.get("undo_by_backup"):
        return _undo_by_backup(
            doc_path, entry, expect_sha256=expect_sha256, dry_run=dry_run, now=now, env=env
        )
    if entry.get("parts"):
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            f"{entry['op_id']} also changed {', '.join(entry['parts'])}, which the history "
            "does not keep; resolve or edit its comments in Word instead of undoing it",
            op_id=entry["op_id"],
            parts=entry["parts"],
        )
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    # (part, story) -> the story's paragraph bytes, located once
    located: dict[tuple[str, str], tuple[bytes, Any, dict[bytes, list[int]]]] = {}

    def locate(part: str, story: str) -> tuple[bytes, Any, dict[bytes, list[int]]]:
        key = (part, story)
        if key not in located:
            container = BODY if not story else _container(pkg, story, part)
            data = pkg.read(part)
            spans = scan(data, container, part).paragraphs
            where: dict[bytes, list[int]] = {}
            for i, span in enumerate(spans):
                where.setdefault(data[span.start : span.end], []).append(i)
            located[key] = (data, (container, spans), where)
        return located[key]

    # (part, story) -> paragraph index in the current part -> bytes it becomes
    swaps: dict[tuple[str, str], dict[int, bytes]] = {}
    reverted: list[dict[str, Any]] = []
    missing: list[int] = []
    row_cuts: list[tuple[int, int, bytes]] = []  # whole-row swaps in the body
    for number, change in enumerate(entry.get("changes", [])):
        part, story = change.get("part", DOCUMENT_PART), change.get("story", "")
        if change.get("kind") == "span":
            data = pkg.read(DOCUMENT_PART)
            after = to_bytes(change["after"])
            at = data.find(after)
            if not after or at < 0 or data.find(after, at + 1) >= 0:
                missing.append(number)
                continue
            row_cuts.append((at, at + len(after), to_bytes(change["before"])))
            reverted.append(
                {
                    "kind": "revert",
                    "span": True,
                    "before": change["after"],
                    "after": change["before"],
                }
            )
            continue
        if part != DOCUMENT_PART and not story:
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT, f"change {number} is in {change['part']}"
            )
        _, _, where = locate(part, story)
        own = swaps.setdefault((part, story), {})
        found = where.get(to_bytes(change["after"]), [])
        if len(found) != 1 or found[0] in own:
            missing.append(number)
            continue
        restored = to_bytes(change["before"]) if change["kind"] == "replace" else b""
        own[found[0]] = restored
        reverted.append(
            {
                "kind": "revert",
                **({"part": part, "story": story} if story else {}),
                "index": found[0],
                "before": change["after"],
                "after": to_text(restored),
            }
        )
    if missing:
        raise _conflict(entry, missing, "a paragraph it changed was edited since or is gone")
    if not any(swaps.values()) and not row_cuts:
        raise DocControlError(
            ErrorCode.HISTORY_NOT_FOUND,
            f"{entry['op_id']} changed no paragraph",
            op_id=entry["op_id"],
        )
    # Each part rewritten once, with the swaps of every story it holds.
    by_part: dict[str, list[tuple[int, int, bytes]]] = {}
    for (part, story), own in swaps.items():
        _, (_, spans), _ = located[(part, story)]
        by_part.setdefault(part, []).extend(
            (spans[i].start, spans[i].end, b) for i, b in own.items()
        )
    if row_cuts:
        by_part.setdefault(DOCUMENT_PART, []).extend(row_cuts)
    replacements: dict[str, bytes] = {}
    for part, cuts in by_part.items():
        cuts.sort()
        for (_, end, _), (start, _, _) in itertools.pairwise(cuts):
            if start < end:
                raise _conflict(entry, [], "its paragraphs are nested")
        data = pkg.read(part)
        out = bytearray()
        pos = 0
        for start, end, new in cuts:
            out += data[pos:start] + new
            pos = end
        out += data[pos:]
        replacements[part] = bytes(out)
    body = swaps.get((DOCUMENT_PART, ""), {})
    footprint = Footprint(
        replaced=frozenset(body),
        spans=frozenset((start, end) for start, end, _ in row_cuts),
        stories=tuple(
            StoryFootprint(part, located[(part, story)][1][0], replaced=frozenset(own))
            for (part, story), own in swaps.items()
            if story
        ),
    )
    if dry_run:
        return UndoOutcome(pkg.sha256, None, None, reverted)
    written = write_verified(
        doc_path,
        replacements,
        footprint,
        pkg.sha256,
        now=now,
        env=env,
        stage=stage,
    )
    return UndoOutcome(written.before_sha256, written.after_sha256, written.backup, reverted)


def _undo_by_backup(
    doc_path: Path,
    entry: Mapping[str, Any],
    *,
    expect_sha256: str | None,
    dry_run: bool,
    now: datetime | None,
    env: Mapping[str, str] | None,
) -> UndoOutcome:
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    current = sha256_file(doc_path)
    if current != entry["sha256_after"]:
        raise _conflict(entry, [], "the document was edited since; it adds a part as a whole")
    saved = DocStore.open(doc_path, env).backups_dir / str(entry.get("backup"))
    # Read now: the backup guard_write takes below may prune this one.
    payload = saved.read_bytes() if saved.is_file() else b""
    if hashlib.sha256(payload).hexdigest() != entry["sha256_before"]:
        raise DocControlError(
            ErrorCode.HISTORY_NOT_FOUND,
            f"the backup {entry.get('backup')} that {entry['op_id']} needs is gone",
            op_id=entry["op_id"],
        )
    reverted = [
        {"kind": "revert", "index": c["index"], "before": c["after"], "after": c["before"]}
        for c in entry.get("changes", [])
        if c.get("kind") == "replace"
    ]
    if dry_run:
        return UndoOutcome(current, None, None, reverted)
    taken = guard_write(doc_path, current, now=now, env=env)
    check_unlocked(doc_path)
    check_changed(doc_path, current)
    try:
        commit_bytes(payload, doc_path, None)
    except PermissionError as exc:
        raise DocControlError(
            ErrorCode.DOC_LOCKED,
            f"could not replace {doc_path.name}, it is held open: {exc}",
            path=str(doc_path),
        ) from exc
    return UndoOutcome(current, sha256_file(doc_path), taken, reverted)


# -- search ---------------------------------------------------------------------------------


def fold(text: str, *, ignore_case: bool, ignore_accents: bool) -> str:
    if ignore_accents:
        text = "".join(
            c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)
        )
    return text.casefold() if ignore_case else text


def search_entry(
    entry: Mapping[str, Any],
    query: str,
    *,
    ignore_case: bool,
    ignore_accents: bool,
    width: int = 40,
    match_level: Level | None = None,
) -> list[dict[str, Any]]:
    """Where ``query`` occurs in one entry: its request, and each paragraph before and after.

    Without ``match_level``, the first hit in each source. With it, every hit, found by
    ``text.find``'s :class:`~doc_control.model.search.LevelMatcher` (a folded level implies both
    folds), each with its offsets in the source (``start``, ``end``), its ``match_level``
    and ``text`` cut around the hit itself.
    """
    sources: list[tuple[str, str]] = [
        ("request", json.dumps(entry.get("request"), ensure_ascii=False))
    ]
    for number, change in enumerate(entry.get("changes", [])):
        if change.get("kind") == "replace":
            sources.append((f"change {number} before", str(change.get("text_before", ""))))
        sources.append((f"change {number} after", xml_text(str(change.get("after", "")))))
    if match_level is not None:
        return _levelled_hits(
            entry["op_id"],
            sources,
            LevelMatcher(
                SearchOptions(query, ignore_case=ignore_case, ignore_accents=ignore_accents),
                match_level,
            ),
            width,
        )
    needle = fold(query, ignore_case=ignore_case, ignore_accents=ignore_accents)
    hits: list[dict[str, Any]] = []
    for where, text in sources:
        folded = fold(text, ignore_case=ignore_case, ignore_accents=ignore_accents)
        at = folded.find(needle)
        if at < 0:
            continue
        # Folding can change the length (accent stripping): then there is no reliable offset.
        start = max(0, at - width) if len(folded) == len(text) else 0
        hits.append({"op_id": entry["op_id"], "in": where, "text": text[start : start + 2 * width]})
    return hits


def _levelled_hits(
    op_id: str, sources: Sequence[tuple[str, str]], matcher: LevelMatcher, width: int
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for where, text in sources:
        for start, end in matcher.spans(text):
            lo = max(0, start - width)
            hits.append(
                {
                    "op_id": op_id,
                    "in": where,
                    "text": text[lo : lo + 2 * width],
                    "start": start,
                    "end": end,
                    "match_level": matcher.levels[start, end],
                }
            )
    return hits


# -- purge ----------------------------------------------------------------------------------


def purge(
    log_path: Path,
    *,
    before: datetime | None,
    everything: bool,
    dry_run: bool,
    confirm: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Remove history (``everything`` or entries older than ``before``), leaving a tombstone.

    A dry run lists what would go and returns ``confirm``; the real run needs that token
    back, so it removes exactly what the dry run showed. Tombstones are never removed.
    """
    if everything == (before is not None):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, "give exactly one of `all: true` and `before`"
        )
    log = read_log(log_path)
    cutoff = _stamp(before) if before is not None else None
    doomed = [
        e
        for e in log.entries
        if e.get("kind") != "purge" and (cutoff is None or str(e.get("time", "")) < cutoff)
    ]
    ids = [e["op_id"] for e in doomed]
    scope = "all" if everything else f"before {cutoff}"
    token = hashlib.sha256(f"{scope}|{','.join(ids)}".encode()).hexdigest()[:12]
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "scope": scope,
        "removed": 0 if dry_run else len(ids),
        "would_remove": len(ids),
        "first_op_id": ids[0] if ids else None,
        "last_op_id": ids[-1] if ids else None,
        "confirm": token,
    }
    if dry_run or not ids:
        return summary
    if confirm != token:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            "purging needs `confirm` from a dry run of the same request"
            + (" (the history changed since)" if confirm else ""),
            path="$.confirm",
        )
    gone = set(ids)
    kept = [ln for ln in log_path.read_bytes().split(b"\n") if ln.strip() and not _is_in(ln, gone)]
    last_removed = max(n for i in ids if (n := op_number(i)) is not None)
    stone = {
        "log_version": LOG_VERSION,
        "op_id": op_id_of(max(log.next_number(), last_removed + 1)),
        "time": _stamp(now),
        "kind": "purge",
        "scope": scope,
        "removed": len(ids),
        "first_op_id": ids[0],
        "last_op_id": ids[-1],
        "last_removed": last_removed,
    }
    text = b"".join(ln + b"\n" for ln in kept) + _line(stone)
    _write_text_atomic(log_path, text.decode("utf-8", "replace"))
    summary["tombstone"] = stone["op_id"]
    return summary


def _is_in(line: bytes, ids: set[str]) -> bool:
    try:
        value = json.loads(line.decode("utf-8"))
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("op_id") in ids and value.get("kind") != "purge"


# -- stores and orphans ----------------------------------------------------------------------


def log_path_for(doc_path: Path, env: Mapping[str, str] | None = None) -> Path:
    return store_dir(doc_path, env) / "log.jsonl"


def last_doc_path(log: Log) -> str | None:
    for entry in reversed(log.entries):
        if isinstance(entry.get("doc_path"), str):
            return str(entry["doc_path"])
    return None


def describe_store(root: Path) -> dict[str, Any]:
    """One store as ``history.orphans`` reports it."""
    log = read_log(root / "log.jsonl")
    real = [e for e in log.entries if e.get("kind") != "purge"]
    doc_id: str | None = None
    with contextlib.suppress(OSError):
        doc_id = (root / "id").read_text(encoding="utf-8").strip() or None
    return {
        "store": str(root),
        "doc": last_doc_path(log),
        "doc_id": doc_id,
        "entries": len(real),
        "last_time": real[-1]["time"] if real else None,
    }


def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_dir())
    except OSError:
        return []


def find_orphans(
    folder: Path | None = None, env: Mapping[str, str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Stores whose document is gone (``orphans``) or unknown (``unknown``).

    Looks in the app-data stores, in a caller-chosen store root (``stores_in`` or
    ``DOC_CONTROL_STORE_DIR``) and, with ``folder``, in ``folder/.doc-control``.
    Nothing is ever deleted here.
    """
    orphans: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    roots = [user_home(env) / "stores"]
    custom = custom_store_root(env)
    if custom is not None:
        roots.append(custom)
    candidates: list[tuple[Path, Path | None]] = [
        (root, None) for base in roots for root in _subdirs(base)
    ]
    if folder is not None:
        candidates += [(root, folder / root.name) for root in _subdirs(folder / STORE_DIRNAME)]
    for root, sibling in candidates:
        info = describe_store(root)
        doc = sibling if sibling is not None else (Path(info["doc"]) if info["doc"] else None)
        if doc is None:
            unknown.append(info)
        elif not doc.exists():
            orphans.append({**info, "doc": str(doc)})
    return {"orphans": orphans, "unknown": unknown}


# -- adopting a moved document's store (D-22) ------------------------------------------------

TAIL_BYTES = 1 << 16


def _tail_entry(root: Path) -> dict[str, Any] | None:
    """The last real entry of the store at ``root``, read from the end of the log when it can."""
    path = root / "log.jsonl"
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return None
    lines = [ln for ln in tail.split(b"\n") if ln.strip()]
    if lines and size > TAIL_BYTES:
        lines = lines[1:]  # the first line of a cut read may be partial
    for line in reversed(lines):
        try:
            value = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(value, dict) and op_number(value.get("op_id")) is not None:
            if value.get("kind") != "purge":
                return value
            break
    real = [e for e in read_log(path).entries if e.get("kind") != "purge"]
    return real[-1] if real else None


def _store_folders(doc: Path, env: Mapping[str, str] | None) -> list[Path]:
    """Folders whose subfolders may hold a store: the app-data and custom roots, the
    document's folder and its neighbours (siblings, parent), and folders app-data logs name."""
    parent = doc.resolve().parent
    seen: dict[Path, None] = {}
    roots: list[Path] = [user_home(env) / "stores"]
    custom = custom_store_root(env)
    if custom is not None:
        roots.append(custom)
    for base in roots:
        seen.setdefault(base, None)
    seen.setdefault(parent / STORE_DIRNAME, None)
    seen.setdefault(parent.parent / STORE_DIRNAME, None)
    for near in (parent, parent.parent):
        for sub in _subdirs(near):
            if not sub.name.startswith("."):
                seen.setdefault(sub / STORE_DIRNAME, None)
    for base in roots:
        for root in _subdirs(base):
            entry = _tail_entry(root)
            named = entry.get("doc_path") if entry else None
            if isinstance(named, str):
                seen.setdefault(Path(named).parent / STORE_DIRNAME, None)
    return list(seen)


def _orphaned(root: Path, entry: dict[str, Any]) -> bool:
    """Whether the document the store belonged to is gone."""
    named = entry.get("doc_path")
    if isinstance(named, str):
        return not Path(named).exists()
    return not (root.parent.parent / root.name).exists()


def adoptable_stores(
    doc: Path, env: Mapping[str, str] | None = None, *, anything: bool = False
) -> list[dict[str, Any]]:
    """Orphan stores the document could take over: last ``sha256_after`` equal to its sha256
    (every orphan with ``anything``). Reads only the end of each log; nothing is changed."""
    doc = doc.resolve()
    sha = sha256_file(doc)
    own = store_dir(doc, env).resolve()
    found: list[dict[str, Any]] = []
    done: set[Path] = set()
    for folder in _store_folders(doc, env):
        for root in _subdirs(folder):
            key = root.resolve()
            if key in done or key == own or not (root / "log.jsonl").is_file():
                continue
            done.add(key)
            entry = _tail_entry(root)
            if entry is None or not _orphaned(root, entry):
                continue
            matched = entry.get("sha256_after") == sha
            if matched or anything:
                found.append({**describe_store(root), "sha_matched": matched})
    return found


def _holds_history(root: Path) -> bool:
    """Whether a store has entries other than the ``open`` notes a fresh store gets."""
    return any(e.get("kind") != "open" for e in read_log(root / "log.jsonl").entries)


def adopt_store(
    doc: Path,
    store: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Move ``store`` to ``doc``'s store location and log an ``adopt`` entry."""
    doc = doc.resolve()
    store = store.resolve()
    if not (store / "log.jsonl").is_file():
        raise DocControlError(
            ErrorCode.HISTORY_NOT_FOUND, f"not a store folder: {store}", path=str(store)
        )
    log = read_log(store / "log.jsonl")
    real = [e for e in log.entries if e.get("kind") != "purge"]
    last_sha = real[-1].get("sha256_after") if real else None
    matched = last_sha is not None and last_sha == sha256_file(doc)
    if not matched and not force:
        raise DocControlError(
            ErrorCode.BATCH_CONFLICT,
            f"{doc.name} differs from the store's last recorded state; pass `force` to adopt it",
            store=str(store),
        )
    target = store_dir(doc, env)
    if target.resolve() == store:
        raise DocControlError(
            ErrorCode.BATCH_CONFLICT,
            "that store already belongs to this document",
            store=str(store),
        )
    if target.exists() and _holds_history(target):
        raise DocControlError(
            ErrorCode.BATCH_CONFLICT,
            f"{doc.name} already has a store with history at {target}; nothing was moved",
            store=str(target),
        )
    result: dict[str, Any] = {
        "adopted": False,
        "dry_run": dry_run,
        "sha_matched": matched,
        "from": str(store),
        "store": str(target),
        "entries": len(real),
    }
    if dry_run:
        return {**result, "would_move": str(store)}
    kept: list[tuple[str, bytes]] = []
    if target.exists():  # a fresh store (`open` notes, a backup): its backups come along
        kept = [(p.name, p.read_bytes()) for p in (target / "backups").glob("*") if p.is_file()]
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        store.rename(target)
    except OSError:
        shutil.copytree(store, target)
        shutil.rmtree(store)
    (target / "backups").mkdir(exist_ok=True)
    for name, data in kept:
        if not (target / "backups" / name).exists():
            (target / "backups" / name).write_bytes(data)
    sha = sha256_file(doc)
    result["op_id"] = record(
        doc,
        "adopt",
        before_sha256=sha,
        after_sha256=sha,
        backup=None,
        changes=[],
        request={"op": "history.adopt", "force": force},
        extra={
            "adopted_from": {"store": str(store), "doc": last_doc_path(log)},
            "sha_matched": matched,
        },
        env=env,
    )
    return {**result, "adopted": True}
