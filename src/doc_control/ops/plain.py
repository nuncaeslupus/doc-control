"""Plain writes (D-21): an edit built as tracked changes, then accepted in the same write.

``tracked: false`` on ``batch``, ``term.rename`` and ``restore`` builds the edit exactly as
the tracked edit and then accepts only the revision ids that edit minted (the accept code of
:mod:`doc_control.ops.revision`), so the written text and properties are by construction
those of "Accept" on that call's changes. Revisions already in the document stay byte for
byte; a paragraph the edit rewrites that holds any other unresolved revision is refused
(``UNSUPPORTED_CONTENT``) before anything is written.

The undo log keeps the paragraph bytes before and after, as for a tracked write, for
``history.list``. A plain paragraph carries no fresh id or date, so ``undo`` could mistake a
later identical paragraph for it: every plain entry is undone from its backup instead
(``undo_by_backup``), only while the file is exactly what that entry wrote, so there is no
out-of-order undo (``BATCH_CONFLICT``: undo later operations first).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.zipio import Package
from doc_control.ops.revision import _REVISION_TAG, ResolveRevisions, prepare_resolutions
from doc_control.ops.tracked import PreparedEdits
from doc_control.safety.history import to_bytes

__all__ = ["make_plain"]


def make_plain(pkg: Package, prepared: PreparedEdits) -> tuple[PreparedEdits, dict[str, Any]]:
    """``prepared`` with this call's own revisions accepted, and the extra fields of its log
    entry (``tracked: false``, and ``undo_by_backup`` when paragraphs were removed)."""
    own = tuple(str(i) for i in prepared.result.revision_ids)
    # Always from the backup: a plain paragraph carries no fresh w:id or date, so a later
    # paragraph with the same bytes could be mistaken for it if undo searched by bytes.
    extra: dict[str, Any] = {"tracked": False, "undo_by_backup": True}
    if not own:  # nothing was marked: the tracked and the plain edit are the same bytes
        return replace(prepared, result=replace(prepared.result, tracked=False)), extra
    touched = [
        c
        for c in prepared.changes
        if c["kind"] == "replace" and _REVISION_TAG.search(to_bytes(c["before"]))
    ]
    if touched:
        where = sorted({f"{c.get('story') or 'body'} #{c['index']}" for c in touched})
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            "a plain write cannot touch a paragraph that holds another unresolved tracked "
            f"change ({len(touched)} paragraph(s)); resolve it first or write tracked",
            paragraphs=where[:10],
        )
    view = pkg.with_parts(prepared.written)
    accepted = prepare_resolutions(view, [ResolveRevisions("accept", ids=own)])
    written = {**prepared.written, **accepted.written}
    after: dict[tuple[str, str, bytes], list[dict[str, Any]]] = defaultdict(list)
    for c in accepted.changes:
        after[(c["part"], c.get("story", ""), to_bytes(c["before"]))].append(c)
    changes: list[dict[str, Any]] = []
    for c in prepared.changes:
        key = (c["part"], c.get("story", ""), to_bytes(c["after"]))
        found = after[key].pop(0) if after.get(key) else None
        if found is None:
            changes.append(c)
        elif c["kind"] == "replace":
            changes.append({**c, "after": found["after"], "deleted": found["deleted"]})
        else:
            changes.append({**c, "after": found["after"]})
    result = replace(prepared.result, revision_ids=[], tracked=False)
    new_data = written.get("word/document.xml", prepared.new_data)
    return replace(
        prepared, new_data=new_data, result=result, changes=changes, written=written
    ), extra
