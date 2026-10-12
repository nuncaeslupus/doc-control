"""``diff``: paragraph alignment between two versions of a document, read-only.

Request: ``old`` and ``new`` (paths to ``.docx`` files), optional ``limit`` and
``cursor`` (the envelope's ``next`` of the previous page; the cursor is tied to the old
file's hash and to the new file's, so a changed file is not paged on silently).

Result: ``old`` / ``new`` (path, sha256, paragraphs), ``summary`` (paragraphs by kind),
``total`` (records), ``changes`` (one page, capped like every list) and ``address_map``
(old address -> new address, or ``null`` for a paragraph that is gone, for the records
of that page). The envelope's ``doc`` is the old file.

Records, in the order of the old version (an insertion right after its predecessor):

* ``{"kind": "modified", "old": ["p3#..."], "new": ["p3#..."], "similarity": 0.93}``
  (``"moved": true`` when it also changed place); ``moved``, ``split``
  (one old, several new), ``merged`` (several old, one new), ``deleted`` (``new`` empty)
  and ``inserted`` (``old`` empty) look alike.
* ``{"kind": "unchanged", "old": [first], "new": [first], "count": n}``: a run of ``n``
  consecutive unchanged paragraphs whose number all shifted by the same amount. Their
  fingerprints are the same on both sides, so paragraph ``p<k>#<fp>`` of the run is
  ``p<k + shift>#<fp>``. Unchanged paragraphs that kept their address are not listed.

Address map for a split: the old address maps to the first new part (the record lists
all parts); for a merge every old part maps to the merged paragraph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply, success
from doc_control.model.align import UNCHANGED, Alignment, Change, align
from doc_control.model.document import Document
from doc_control.model.index import DocumentIndex
from doc_control.ops.paging import (
    PAGING_PROPERTIES,
    RESPONSE_BUDGET,
    make_cursor,
    read_cursor,
    size,
)
from doc_control.registry import operation


def diff_documents(old: Path, new: Path) -> tuple[DocumentIndex, DocumentIndex, Alignment]:
    """Index both files (read-only) and align their paragraphs."""
    index_old, index_new = Document(str(old)).index, Document(str(new)).index
    return index_old, index_new, align(index_old.paragraphs, index_new.paragraphs)


def address_map(
    alignment: Alignment, old: DocumentIndex, new: DocumentIndex
) -> dict[str, str | None]:
    """The full cross-version map: every old address to its new address, or ``None``."""
    out: dict[str, str | None] = {}
    for change in alignment.changes:
        target = str(new.paragraphs[change.new[0]].address) if change.new else None
        for i in change.old:
            out[str(old.paragraphs[i].address)] = target
    return out


def _records(alignment: Alignment, old: DocumentIndex, new: DocumentIndex) -> list[dict[str, Any]]:
    """Output records: unchanged paragraphs that kept their address are left out."""
    out: list[dict[str, Any]] = []
    run: dict[str, Any] | None = None
    next_old = shift = 0
    for change in alignment.changes:
        if change.kind != UNCHANGED:
            run = None
            out.append(_record(change, old, new))
            continue
        i, j = change.old[0], change.new[0]
        if i == j:
            run = None
        elif run is not None and j - i == shift and next_old == i:
            run["count"] += 1
            next_old = i + 1
        else:
            shift, next_old = j - i, i + 1
            run = {
                "kind": UNCHANGED,
                "old": [str(old.paragraphs[i].address)],
                "new": [str(new.paragraphs[j].address)],
                "count": 1,
            }
            out.append(run)
    return out


def _record(change: Change, old: DocumentIndex, new: DocumentIndex) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "kind": change.kind,
        "old": [str(old.paragraphs[i].address) for i in change.old],
        "new": [str(new.paragraphs[j].address) for j in change.new],
    }
    if change.similarity is not None:
        rec["similarity"] = change.similarity
    if change.moved:
        rec["moved"] = True
    return rec


def _page_map(records: list[dict[str, Any]]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for rec in records:
        if rec["kind"] == UNCHANGED:
            continue
        target = rec["new"][0] if rec["new"] else None
        for address in rec["old"]:
            out[address] = target
    return out


@operation(
    "diff",
    "read",
    "Paragraph alignment between two versions of a document, with a cross-version address map.",
    {
        "type": "object",
        "properties": {
            "old": {"type": "string", "description": "Path of the earlier version (.docx)."},
            "new": {"type": "string", "description": "Path of the later version (.docx)."},
            **PAGING_PROPERTIES,
        },
        "required": ["old", "new"],
        "additionalProperties": False,
    },
)
def diff(request: dict[str, Any]) -> Reply:
    old, new = Document(request["old"]), Document(request["new"])
    alignment = align(old.index.paragraphs, new.index.paragraphs)
    records = _records(alignment, old.index, new.index)

    # The cursor must also notice the new file changing: fold its hash into the scope.
    scoped = {**request, "new_sha256": new.sha256}
    start = read_cursor("diff", old, scoped)
    limit = request.get("limit", len(records))
    result: dict[str, Any] = {
        "old": old.envelope_doc(),
        "new": new.envelope_doc(),
        "summary": alignment.counts(),
        "total": len(records),
        "changes": [],
        "address_map": {},
    }
    placeholder = make_cursor("diff", old, scoped, 10**12)
    room = RESPONSE_BUDGET - size(
        success("diff", result, doc=old.envelope_doc(), next_cursor=placeholder)
    )
    taken: list[dict[str, Any]] = []
    used = 0
    for rec in records[start : start + limit]:
        cost = 2 * len(json.dumps(rec, ensure_ascii=False).encode("utf-8")) + 4  # record + map
        if taken and used + cost > room:
            break
        taken.append(rec)
        used += cost
    end = start + len(taken)
    result["changes"] = taken
    result["address_map"] = _page_map(taken)
    cursor = make_cursor("diff", old, scoped, end) if end < len(records) else None
    return Reply(result, doc=old.envelope_doc(), next_cursor=cursor)
