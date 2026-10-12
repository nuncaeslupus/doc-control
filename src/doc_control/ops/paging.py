"""Response size cap and cursors for read operations.

A default response never exceeds :data:`RESPONSE_BUDGET` bytes (about 2,000
tokens at 4 bytes per token). Lists stop at the budget, or at the request's
``limit``, and say where to continue in ``next``. A cursor is
``c:<op>:<sha8>:<offset>``: it is tied to the document's hash, so reading on
after the file changed is ``DOC_CHANGED_ON_DISK`` rather than a silent skip.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from doc_control.envelope import Reply, success
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document

RESPONSE_BUDGET = 8000
MAX_LIMIT = 1000
_CURSOR = re.compile(r"c:(?P<op>[a-z.]+):(?P<sha>[0-9a-f]{8}):(?P<offset>0|[1-9][0-9]{0,11})")

#: Properties every list request accepts, to merge into an operation's schema.
PAGING_PROPERTIES: dict[str, Any] = {
    "limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_LIMIT,
        "description": "Most items to return; a response is also capped at about 8,000 bytes.",
    },
    "cursor": {
        "type": "string",
        "minLength": 1,
        "description": "The `next` value of the previous response, to continue the list.",
    },
}


def make_cursor(op: str, doc: Document, offset: int) -> str:
    return f"c:{op}:{doc.sha256[:8]}:{offset}"


def read_cursor(op: str, doc: Document, cursor: str | None) -> int:
    """The offset a cursor continues from (0 without one)."""
    if cursor is None:
        return 0
    m = _CURSOR.fullmatch(cursor)
    if m is None or m["op"] != op:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f"cursor is not a {op} cursor: {cursor!r}", path="$.cursor"
        )
    if m["sha"] != doc.sha256[:8]:
        raise DocControlError(
            ErrorCode.DOC_CHANGED_ON_DISK,
            f"{doc.path} changed since the cursor was issued",
            path=doc.path,
            actual_sha256=doc.sha256,
        )
    return int(m["offset"])


def size(envelope: dict[str, Any]) -> int:
    """Bytes of the envelope as the CLI prints it."""
    return len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))


def paged(
    op: str,
    doc: Document,
    request: dict[str, Any],
    key: str,
    items: Sequence[Any],
    base: dict[str, Any] | None = None,
) -> Reply:
    """A list response: ``base`` fields plus ``key`` holding the page, ``total`` and ``next``.

    Takes items from the cursor's offset while the whole envelope stays within the
    budget and the limit; always at least one, so a list always advances.
    """
    start = read_cursor(op, doc, request.get("cursor"))
    limit = request.get("limit", MAX_LIMIT)
    head: dict[str, Any] = {**(base or {}), "total": len(items), key: []}
    placeholder = make_cursor(op, doc, 10**12)
    room = RESPONSE_BUDGET - size(
        success(op, head, doc=doc.envelope_doc(), next_cursor=placeholder)
    )
    taken: list[Any] = []
    used = 0
    for item in items[start : start + limit]:
        cost = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 2  # ", " between items
        if taken and used + cost > room:
            break
        taken.append(item)
        used += cost
    end = start + len(taken)
    head[key] = taken
    return Reply(
        head,
        doc=doc.envelope_doc(),
        next_cursor=make_cursor(op, doc, end) if end < len(items) else None,
    )
