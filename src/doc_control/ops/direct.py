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

#: Each direct write tool: the ``doc.batch`` kind it applies, and its summary.
DIRECT: dict[str, str] = {
    "text.replace": (
        "Replace one range with new text, as a tracked change: part of a paragraph "
        "(p3#a1b2c3d4:10-15, code-point offsets), a whole paragraph (p3#a1b2c3d4) or a span "
        "across paragraphs (p3#a1b2c3d4:10..p5#e5f6a7b8:2); empty `text` deletes it. Get "
        "ranges from text.find or para.get. File engine: backed up first, logged for "
        "history.undo, dry_run previews, expect.sha256 refuses a changed file; tracked: false "
        "writes plainly. engine live edits the open app. STALE_ADDRESS if the paragraph "
        "changed since read. Use term.rename for a name everywhere, doc.batch for several "
        "edits."
    ),
    "para.insert": (
        "Insert one new paragraph with `text` after or before an existing one (give `after` or "
        "`before`, e.g. p3#a1b2c3d4), as a tracked insertion (style it with style.apply in "
        "doc.batch). File engine: backed up first, logged for history.undo, dry_run previews, "
        "expect.sha256 refuses a changed file; tracked: false inserts plainly. engine live "
        "inserts in the open Word/LibreOffice window (after live.open). Use doc.batch for "
        "several edits all or nothing. Returns the address_map of shifted paragraphs."
    ),
    "para.delete": (
        "Delete a paragraph (p3#a1b2c3d4) or a run of paragraphs (p3#a1b2c3d4..p5#e5f6a7b8), "
        "as a tracked deletion the writer can reject. File engine: backed up first, logged for "
        "history.undo, dry_run previews, expect.sha256 refuses a file changed since; tracked: "
        "false deletes plainly (a change the writer approved). STALE_ADDRESS if the paragraph "
        "changed since it was read. Use text.replace to delete part of a paragraph, doc.batch "
        "for several edits all or nothing. Returns the address_map of shifted paragraphs."
    ),
    "comment.add": (
        "Add one comment on a range: `at` is a paragraph (p3#a1b2c3d4), part of one "
        "(p3#a1b2c3d4:10-15) or a span across paragraphs (p3#a1b2c3d4:10..p5#e5f6a7b8:2); a "
        "newline in `text` starts a new comment paragraph. Author defaults to "
        "DOC_CONTROL_AUTHOR. File engine: backed up first, dry_run previews, expect.sha256 "
        "refuses a file changed since; history.undo cannot take a comment back. engine live "
        "adds it in the open Word/LibreOffice window (after live.open). Use doc.batch to add "
        "several, or with comment.reply/comment.resolve, all or nothing. Returns the new "
        "comment id."
    ),
    "revision.accept": (
        "Accept tracked changes, making them plain text: by `ids` (from revision.list), by "
        "revision `author`, in a paragraph or run (`at`, p3#a1b2c3d4..p9#e5f6a7b8), or `all`. "
        "Not itself a tracked change: on the file engine the file is backed up first and the "
        "write is logged for history.undo; dry_run previews counts; expect.sha256 refuses a "
        "changed file. Use revision.reject to drop changes instead, doc.batch to accept and "
        "reject in one write. Returns counts and the revisions resolved."
    ),
    "revision.reject": (
        "Reject tracked changes, undoing them in the text (insertions removed, deletions "
        "restored): by `ids` (from revision.list), by revision `author`, in a paragraph or run "
        "(`at`, p3#a1b2c3d4..p9#e5f6a7b8), or `all`. Not itself a tracked change: on the file "
        "engine the file is backed up first and the write is logged for history.undo; dry_run "
        "previews counts; expect.sha256 refuses a changed file. Use revision.accept to keep "
        "changes instead, doc.batch to accept and reject in one write. Returns counts and the "
        "revisions resolved."
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
