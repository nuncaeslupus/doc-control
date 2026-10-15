"""Direct write tools: one edit as an operation of its own, run through ``doc.batch``.

``text.replace``, ``para.insert``, ``para.delete``, ``comment.add``, ``revision.accept``
and ``revision.reject`` each take the fields of that ``doc.batch`` kind (its schema in
:data:`doc_control.ops.batch.ALL_SCHEMAS`) plus the batch envelope (``doc``, ``expect``,
``dry_run``, ``author``, ``tag``, ``engine``), and answer exactly as ``doc.batch`` does with
that one edit in ``ops``. ``engine`` is offered only for the kinds the live engine applies
(:data:`doc_control.ops.live.LIVE_KINDS`); the others refuse ``engine: live`` as an
operation without a live engine. The revision kinds have an ``author`` of their own (whose
revisions to resolve), so they take no envelope ``author`` or ``tag``, which only stamp new
revisions, and no ``DOC_CONTROL_AUTHOR`` default is filled in for them.
"""

from __future__ import annotations

from typing import Any

from doc_control import outputs
from doc_control.envelope import Reply
from doc_control.ops.batch import ALL_SCHEMAS, BATCH_SCHEMA, batch
from doc_control.ops.live import LIVE_KINDS
from doc_control.registry import Handler, operation

_ENVELOPE = ("doc", "expect", "dry_run", "tracked", "author", "tag", "engine")
_PLAIN_KINDS = ("text.replace", "para.insert", "para.delete")
_ONE = "Use doc.batch to apply several edits all or nothing."
_TRACKED = (
    "A tracked change: on the file engine the file is backed up first, history.undo takes "
    "it back, dry_run previews and expect.sha256 refuses a file changed since. "
    "tracked: false writes it plainly instead (a change the writer already approved; no "
    "revision marks; file engine)."
)
_RESOLVES = (
    "Not itself tracked: on the file engine the file is backed up first and the write is "
    "logged for history.undo, dry_run previews and expect.sha256 refuses a file changed since."
)

#: Each direct write tool: the ``doc.batch`` kind it applies, and its summary.
DIRECT: dict[str, str] = {
    "text.replace": (
        "Replace one range (p3#fp:10-15, a whole paragraph p3#fp, or p3#fp:10..p5#fp:2) "
        f"with new text; empty text deletes it. {_TRACKED} {_ONE}"
    ),
    "para.insert": (
        "Insert one new paragraph after or before a paragraph (give one of them), as a "
        f"tracked insertion. {_TRACKED} {_ONE}"
    ),
    "para.delete": (
        "Delete a paragraph (p3#fp) or a run of paragraphs (p3#fp..p5#fp), as a tracked "
        f"deletion. {_TRACKED} {_ONE}"
    ),
    "comment.add": (
        "Add a comment on a range (p3#fp, p3#fp:10-15 or across paragraphs), by the "
        "author. On the file engine the file is backed up first, dry_run previews and "
        f"expect.sha256 refuses a file changed since. {_ONE}"
    ),
    "revision.accept": (
        "Accept tracked changes: by ids (from revision.list), by revision author, in a "
        f"paragraph or run (at), or all of them. {_RESOLVES} {_ONE}"
    ),
    "revision.reject": (
        "Reject tracked changes: by ids (from revision.list), by revision author, in a "
        f"paragraph or run (at), or all of them. {_RESOLVES} {_ONE}"
    ),
}


def direct_schema(kind: str) -> dict[str, Any]:
    """The request schema of the direct tool for ``kind``: the kind's own fields plus the
    batch envelope fields it does not already have, ``engine`` only when it runs live."""
    own = ALL_SCHEMAS[kind]
    fields = {k: v for k, v in own["properties"].items() if k != "op"}
    envelope = {
        k: BATCH_SCHEMA["properties"][k]
        for k in _ENVELOPE
        if k not in fields
        and (k != "engine" or kind in LIVE_KINDS)
        and (k != "tracked" or kind in _PLAIN_KINDS)
        and not (kind.startswith("revision.") and k == "tag")
    }
    return {
        "type": "object",
        "properties": {**envelope, **fields},
        "required": ["doc", *(r for r in own["required"] if r != "op")],
        "additionalProperties": False,
    }


def _handler(kind: str) -> Handler:
    fields = set(ALL_SCHEMAS[kind]["properties"]) - {"op"}

    def run(request: dict[str, Any]) -> dict[str, Any] | Reply:
        edit = {"op": kind, **{k: v for k, v in request.items() if k in fields}}
        envelope = {k: v for k, v in request.items() if k not in fields}
        return batch({**envelope, "ops": [edit]})

    run.__name__ = kind.replace(".", "_")
    return run


for _kind, _summary in DIRECT.items():
    operation(
        _kind,
        "write",
        _summary,
        direct_schema(_kind),
        # A revision kind's `author` picks whose revisions to resolve: never a default.
        fills_author=not _kind.startswith("revision."),
        output=outputs.direct_output(_kind, live=_kind in LIVE_KINDS),
    )(_handler(_kind))
