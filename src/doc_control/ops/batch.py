"""The ``batch`` write operation: all-or-nothing tracked edits in one verified write.

A request lists ``ops`` (``text.replace``, ``para.insert``, ``para.delete``,
``para.move``, ``style.apply``, ``format.apply``, ``revision.accept``, ``revision.reject``;
``comment.add``, ``comment.reply``, ``comment.resolve``; their schemas are in
:data:`ALL_SCHEMAS`). Every address refers to
the document as it was read. Before anything is written each operation is validated:
addresses must resolve and still match their fingerprint (``STALE_ADDRESS``), ranges
must not overlap (``BATCH_CONFLICT``, naming both operation indexes in ``edits``) and
``expect.sha256`` must match the file (``DOC_CHANGED_ON_DISK``). Any failure leaves the
document untouched. ``dry_run`` does everything but the write and returns the same
response with a per-operation preview. The result carries ``address_map`` (old to new
address of every paragraph whose index changed) and, with the envelope's ``doc``, the
new sha256.

The text operation kinds are batch operations only; they are not registered as
standalone writes. The style kinds (``style.create``, ``style.update``, ``style.rename``,
``style.merge``, see :mod:`doc_control.ops.stylewrite`) are batch kinds too, but a batch
holds either text kinds or style kinds, never both: style definition changes are not
tracked changes, so a style batch is one verified write of ``word/styles.xml`` (and the
references to renamed or merged styles) with the same dry run, ``expect`` and backup.
``revision.accept`` and ``revision.reject`` (see :mod:`doc_control.ops.revision`) are batch
kinds that resolve revisions; they cannot share a batch with other kinds. Nor can
``table.row.insert`` and ``table.row.delete`` (see :mod:`doc_control.ops.tablerows`), which
add or delete whole table rows as tracked changes.
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
from doc_control.ops.revision import (
    OPERATION_SCHEMAS as REVISION_SCHEMAS,
)
from doc_control.ops.revision import apply_resolutions, resolution_from_dict
from doc_control.ops.stylewrite import STYLE_KINDS, apply_style_ops, op_schema
from doc_control.ops.tablerows import ROW_SCHEMAS, apply_row_ops, row_op_from_dict
from doc_control.ops.tracked import OPERATION_SCHEMAS, apply_edits, edit_from_dict
from doc_control.registry import operation

ALL_SCHEMAS = {
    **OPERATION_SCHEMAS,
    **COMMENT_SCHEMAS,
    **{k: op_schema(k) for k in STYLE_KINDS},
    **REVISION_SCHEMAS,
    **ROW_SCHEMAS,
}
BATCH_KINDS = tuple(ALL_SCHEMAS)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc": {
            "type": "string",
            "minLength": 1,
            "description": "Path to the .docx (live: or the file name of one open in a live app).",
        },
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
        "engine": {
            "enum": ["file", "live"],
            "description": "`live` edits the document open in Word or LibreOffice (see live.open).",
        },
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
    (
        "The main editing tool: apply several edits as tracked changes, all or nothing. "
        "One family per batch: text.replace, para.insert, para.delete, para.move, "
        "style.apply, format.apply, comment.add, comment.reply and comment.resolve; or "
        "table.row.insert and table.row.delete (tracked table rows); or "
        "style.create/update/rename/merge; or revision.accept and revision.reject (by "
        "ids, author, address range or all). Addresses refer to the document as read; "
        "expect.sha256 refuses a file changed since; dry_run previews. The file is backed "
        "up first and undo takes the batch back."
    ),
    _SCHEMA,
)
def batch(request: dict[str, Any]) -> Reply:
    if request.get("tracked", True) is not True:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "untracked edits are not supported in v1")
    if request.get("engine") == "live":
        from doc_control.ops.live import live_batch

        return Reply(live_batch(request))
    expect = request.get("expect", {})
    path = request["doc"]
    kinds = {op.get("op") in REVISION_SCHEMAS for op in request["ops"]}
    if True in kinds:
        if False in kinds:
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                "revision.accept/revision.reject cannot be combined with text or paragraph "
                "edits in one batch: send them as separate batches",
            )
        resolutions = []
        for i, raw in enumerate(request["ops"]):
            try:
                resolutions.append(resolution_from_dict(raw))
            except DocControlError as err:
                if err.code is not ErrorCode.SCHEMA_INVALID:
                    raise
                raise DocControlError(
                    ErrorCode.SCHEMA_INVALID, f"ops[{i}]: {err.message}", op_index=i
                ) from err
        res = apply_resolutions(
            Path(path),
            resolutions,
            expect_sha256=expect.get("sha256"),
            dry_run=request.get("dry_run", False),
        )
        return Reply(res.to_dict(), doc=Document(path).envelope_doc())
    row_ops = [isinstance(o, dict) and o.get("op") in ROW_SCHEMAS for o in request["ops"]]
    if any(row_ops):
        if not all(row_ops):
            raise DocControlError(
                ErrorCode.BATCH_CONFLICT,
                "table.row.insert/table.row.delete cannot be combined with other operations "
                "in one batch: send them as separate batches",
            )
        parsed = []
        for i, raw in enumerate(request["ops"]):
            try:
                parsed.append(row_op_from_dict(raw))
            except DocControlError as err:
                if err.code is not ErrorCode.SCHEMA_INVALID:
                    raise
                raise DocControlError(
                    ErrorCode.SCHEMA_INVALID, f"ops[{i}]: {err.message}", op_index=i
                ) from err
        rows = apply_row_ops(
            Path(path),
            parsed,
            expect_sha256=expect.get("sha256"),
            author=request.get("author", DEFAULT_AUTHOR),
            tag=request.get("tag"),
            dry_run=request.get("dry_run", False),
        )
        return Reply(rows.to_dict(), doc=Document(path).envelope_doc())
    style_ops = [isinstance(o, dict) and o.get("op") in STYLE_KINDS for o in request["ops"]]
    if any(style_ops):
        if not all(style_ops):
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                "style operations and text operations cannot share a batch",
            )
        styled = apply_style_ops(
            Path(path),
            request["ops"],
            expect_sha256=expect.get("sha256"),
            dry_run=request.get("dry_run", False),
        )
        return Reply(styled, doc=Document(path).envelope_doc())
    edits = edits_from_request(request["ops"])
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
