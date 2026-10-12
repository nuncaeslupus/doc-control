"""``diff``: paragraph alignment between two versions of a document, read-only.

Request: ``old`` and ``new`` (paths to ``.docx`` files), optional ``limit`` (records per
page, default 200) and ``cursor`` (the ``next`` of the previous page).

Result: ``old`` / ``new`` (path, sha256, paragraphs), ``summary`` (paragraphs by kind),
``changes`` (one page) and ``address_map`` (old address -> new address, or ``null``
for a paragraph that is gone, for the records of that page) plus ``next`` (the cursor,
``null`` on the last page; the envelope's own ``next`` takes it over once operations
return a reply object).

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

import hashlib
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.align import UNCHANGED, Alignment, Change, align
from doc_control.model.index import DocumentIndex
from doc_control.ooxml.zipio import Package
from doc_control.registry import operation

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000


def diff_documents(old: Path, new: Path) -> tuple[DocumentIndex, DocumentIndex, Alignment]:
    """Index both files (read-only) and align their paragraphs."""
    index_old = DocumentIndex.from_package(Package(old))
    index_new = DocumentIndex.from_package(Package(new))
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
    shift = 0
    for change in alignment.changes:
        if change.kind == UNCHANGED:
            i, j = change.old[0], change.new[0]
            if i == j:
                run = None
            elif run is not None and j - i == shift and run["_next"] == i:
                run["count"] += 1
                run["_next"] = i + 1
            else:
                shift = j - i
                run = {
                    "kind": UNCHANGED,
                    "old": [str(old.paragraphs[i].address)],
                    "new": [str(new.paragraphs[j].address)],
                    "count": 1,
                    "_next": i + 1,
                }
                out.append(run)
            continue
        run = None
        out.append(_record(change, old, new))
    for rec in out:
        rec.pop("_next", None)
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


def _doc_info(path: Path, index: DocumentIndex, sha256: str) -> dict[str, Any]:
    return {"path": str(path), "sha256": sha256, "paragraphs": len(index)}


def _offset(cursor: str | None, token: str) -> int:
    if cursor is None:
        return 0
    prefix = f"c:diff:{token}:"
    if cursor.startswith(prefix) and cursor[len(prefix) :].isdigit():
        return int(cursor[len(prefix) :])
    raise DocControlError(
        ErrorCode.SCHEMA_INVALID, "cursor does not belong to this comparison", cursor=cursor
    )


@operation(
    "diff",
    "read",
    "Paragraph alignment between two versions of a document, with a cross-version address map.",
    {
        "type": "object",
        "properties": {
            "old": {"type": "string", "description": "Path of the earlier version (.docx)."},
            "new": {"type": "string", "description": "Path of the later version (.docx)."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "description": f"Records per page (default {DEFAULT_LIMIT}).",
            },
            "cursor": {"type": "string", "description": "The `next` of the previous page."},
        },
        "required": ["old", "new"],
        "additionalProperties": False,
    },
)
def diff(request: dict[str, Any]) -> dict[str, Any]:
    path_old, path_new = Path(request["old"]), Path(request["new"])
    pkg_old, pkg_new = Package(path_old), Package(path_new)
    index_old = DocumentIndex.from_package(pkg_old)
    index_new = DocumentIndex.from_package(pkg_new)
    alignment = align(index_old.paragraphs, index_new.paragraphs)
    records = _records(alignment, index_old, index_new)

    token = hashlib.sha256((pkg_old.sha256 + pkg_new.sha256).encode()).hexdigest()[:8]
    start = _offset(request.get("cursor"), token)
    limit = request.get("limit", DEFAULT_LIMIT)
    page = records[start : start + limit]
    more = start + limit < len(records)
    return {
        "old": _doc_info(path_old, index_old, pkg_old.sha256),
        "new": _doc_info(path_new, index_new, pkg_new.sha256),
        "summary": alignment.counts(),
        "changes": page,
        "address_map": _page_map(page),
        "next": f"c:diff:{token}:{start + limit}" if more else None,
    }
