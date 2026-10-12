"""The ``batch`` write operation: all-or-nothing tracked edits in one verified write.

A request lists ``ops`` (``text.replace``, ``para.insert``, ``para.delete``,
``para.move``; their schemas are in :data:`OPERATION_SCHEMAS`). Every address refers to
the document as it was read. Before anything is written each operation is validated:
addresses must resolve and still match their fingerprint (``STALE_ADDRESS``), ranges
must not overlap (``BATCH_CONFLICT``, naming both operation indexes in ``edits``) and
``expect.sha256`` must match the file (``DOC_CHANGED_ON_DISK``). Any failure leaves the
document untouched. ``dry_run`` does everything but the write and returns the same
response with a per-operation preview. The result carries ``address_map`` (old to new
address of every paragraph whose index changed) and, with the envelope's ``doc``, the
new sha256.

The operation kinds are batch operations only; they are not registered as
standalone writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.ooxml.revisions import DEFAULT_AUTHOR
from doc_control.ops.comments import (
    COMMENT_SCHEMAS,
    apply_batch,
    comment_from_dict,
    is_comment_edit,
)
from doc_control.ops.tracked import OPERATION_SCHEMAS, apply_edits, edit_from_dict
from doc_control.registry import operation

ALL_SCHEMAS = {**OPERATION_SCHEMAS, **COMMENT_SCHEMAS}
BATCH_KINDS = tuple(ALL_SCHEMAS)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
        "ops": {
            "type": "array",
            "description": (
                "Operations, applied together. Every address refers to the document as read; an "
                "operation placed relative to a paragraph another operation deletes or moves "
                "is a BATCH_CONFLICT. Kinds: "
                + "; ".join(
                    f"{name} {sorted(set(s['properties']) - {'op'})}"
                    for name, s in ALL_SCHEMAS.items()
                )
                + "."
            ),
            "items": {
                "type": "object",
                "properties": {"op": {"enum": list(BATCH_KINDS)}},
                "required": ["op"],
            },
        },
        "expect": {
            "type": "object",
            "properties": {
                "sha256": {"type": "string", "minLength": 1, "description": "Hash read earlier."}
            },
            "additionalProperties": False,
        },
        "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
        "tracked": {"type": "boolean", "description": "Tracked changes (the only mode, true)."},
        "author": {"type": "string", "minLength": 1, "description": "Revision author."},
        "tag": {"type": "string", "minLength": 1, "description": "Appended to the author."},
    },
    "required": ["doc", "ops"],
    "additionalProperties": False,
}


def edits_from_request(ops: list[dict[str, Any]]) -> list[Any]:
    """Build every edit, naming the operation (``ops[i]``) that fails validation."""
    edits: list[Any] = []
    for i, raw in enumerate(ops):
        try:
            if raw.get("op") in COMMENT_SCHEMAS:
                edits.append(comment_from_dict(raw))
            else:
                edits.append(edit_from_dict(raw))
        except DocControlError as err:
            if err.code is not ErrorCode.SCHEMA_INVALID:
                raise
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID, f"ops[{i}]: {err.message}", op_index=i, **err.details
            ) from err
    return edits


@operation(
    "batch",
    "write",
    "Apply text.replace, para.insert, para.delete, para.move, comment.add, comment.reply "
    "and comment.resolve together, all or nothing; dry_run previews.",
    _SCHEMA,
)
def batch(request: dict[str, Any]) -> Reply:
    if request.get("tracked", True) is not True:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "untracked edits are not supported in v1")
    edits = edits_from_request(request["ops"])
    expect = request.get("expect", {})
    path = request["doc"]
    apply = apply_batch if any(is_comment_edit(e) for e in edits) else apply_edits
    result = apply(
        Path(path),
        edits,
        expect_sha256=expect.get("sha256"),
        author=request.get("author", DEFAULT_AUTHOR),
        tag=request.get("tag"),
        dry_run=request.get("dry_run", False),
    )
    return Reply(result.to_dict(), doc=Document(path).envelope_doc())
