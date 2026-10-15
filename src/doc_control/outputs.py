"""Building blocks for output schemas: what an operation's ``result`` holds.

Every operation declares one (``operation(..., output=...)``). It is listed as the MCP
tool's ``outputSchema`` (wrapped in the response envelope, see :func:`envelope_schema`) and
by ``server.help``, and the test suite checks every result the operations return against
it. The schemas use the subset :mod:`doc_control.schema` validates. An object that lists
``properties`` names every key it can hold; one whose keys vary (an address map, a part
footprint) gives no ``properties``.
"""

from __future__ import annotations

from typing import Any

from doc_control import API_VERSION

Schema = dict[str, Any]


def obj(description: str, required: tuple[str, ...] = (), /, **properties: Schema) -> Schema:
    """An object with these ``properties`` (``required`` among them)."""
    out: Schema = {"type": "object", "description": description}
    if properties:
        out["properties"] = properties
    if required:
        out["required"] = list(required)
    return out


def result(required: tuple[str, ...] = (), /, **properties: Schema) -> Schema:
    """The top-level ``result`` object of an operation."""
    out: Schema = {"type": "object", "properties": properties}
    if required:
        out["required"] = list(required)
    return out


def extensible(schema: Schema) -> Schema:
    """``schema`` (an object) declaring that keys beyond its ``properties`` may appear."""
    return {**schema, "additionalProperties": True}


def arr(description: str, items: Schema) -> Schema:
    return {"type": "array", "description": description, "items": items}


def string(description: str) -> Schema:
    return {"type": "string", "description": description}


def integer(description: str) -> Schema:
    return {"type": "integer", "description": description}


def number(description: str) -> Schema:
    return {"type": "number", "description": description}


def boolean(description: str) -> Schema:
    return {"type": "boolean", "description": description}


def nullable(kind: str, description: str) -> Schema:
    return {"type": [kind, "null"], "description": description}


def mapping(description: str) -> Schema:
    """An object whose keys vary (addresses, part names, counts by kind)."""
    return {"type": "object", "description": description}


def strings(description: str) -> Schema:
    return arr(description, {"type": "string"})


def anything(description: str) -> Schema:
    return {"description": description}


# -- shared fields -------------------------------------------------------------------------

ADDRESS = string("Paragraph address, p<n>#<fingerprint> (story-prefixed in notes/headers).")
RANGE = string("Range address, p<n>#<fp>:<start>-<end> (code points).")
TOTAL = integer("Items in the whole list, across pages.")
DRY_RUN = boolean("True when nothing was written (a preview).")
SHA256 = nullable("string", "sha256 of the file after the write (pass as expect.sha256 next).")
BACKUP = nullable("string", "Path of the backup taken before the write.")
OP_ID = nullable("string", "Undo-log id of this write (history.undo / history.restore).")
AUTHOR = string("Revision author stamped on the changes.")
DATE = string("Revision date (UTC, ISO 8601).")
REVISION_IDS = arr("w:id of every revision this write created.", {"type": ["integer", "string"]})
FOOTPRINT = mapping("Part name -> the paragraphs or items it touched in that part.")
ADDRESS_MAP = mapping("Old address -> new address of every paragraph whose address changed.")
TRACKED = boolean("False for a plain (untracked) write.")
WARNINGS = strings("Notes on what the write could not do as asked.")
COMMIT = {"sha256": SHA256, "backup": BACKUP, "op_id": OP_ID}
"""What a file write that happened adds (absent on a dry run)."""

PREVIEW = arr(
    "One entry per edit: what changes where.",
    obj(
        "A previewed edit.",
        op=integer("Index of the edit in ops."),
        at=string("Where it applies."),
        before=string("Text before."),
        after=string("Text after."),
        action=string("accept or reject (revision kinds)."),
        revisions=integer("Revisions it resolves (revision kinds)."),
    ),
)

#: The result of doc.batch and of the direct write tools that run through it.
BATCH_FIELDS: dict[str, Schema] = {
    "applied": {
        "type": ["integer", "array"],
        "description": "Edits applied (a count), or for style kinds one entry per style op.",
    },
    "dry_run": DRY_RUN,
    "preview": PREVIEW,
    "address_map": ADDRESS_MAP,
    "footprint": FOOTPRINT,
    "revision_ids": REVISION_IDS,
    "author": AUTHOR,
    "date": DATE,
    "comment_ids": strings("Ids of the comments added."),
    "tracked": TRACKED,
    "warnings": WARNINGS,
    **COMMIT,
}

#: What revision.accept / revision.reject (and a doc.batch of them) add.
RESOLUTION_FIELDS: dict[str, Schema] = {
    "accepted": integer("Revisions accepted."),
    "rejected": integer("Revisions rejected."),
    "revisions": arr(
        "The revisions resolved (capped; see revisions_omitted).",
        obj(
            "A resolved revision.",
            id=string("Revision id."),
            type=string("Revision type (insert, delete, mark_insert, format...)."),
            author=string("Its author."),
            at=string("Paragraph address."),
            action=string("accept or reject."),
        ),
    ),
    "revisions_omitted": integer("Resolved revisions left out of the list."),
    "merged_paragraphs": integer("Paragraphs joined by resolving paragraph marks."),
}

#: What a live-engine edit (engine: live) returns besides applied and author.
LIVE_FIELDS: dict[str, Schema] = {
    "engine": string("live: edited in the open app; nothing backed up or verified."),
    "op_ids": strings("Live: one log entry per edit, in request order."),
}

#: The result of a style write (style.* and styleset.install/import_dotx).
STYLE_WRITE_FIELDS: dict[str, Schema] = {
    "dry_run": DRY_RUN,
    "tracked": boolean("Always false: style definitions are not tracked changes."),
    "changed_parts": strings("Package parts rewritten."),
    "paragraphs_changed": integer("Paragraphs whose style reference was rewritten."),
    "references_rewritten": integer("Style references rewritten (renames, merges)."),
    "sha256": SHA256,
    "backup": BACKUP,
}

STYLE_APPLIED = arr(
    "One entry per style operation.",
    obj(
        "What one style operation did.",
        op=string("style.create, style.update, style.rename or style.merge."),
        style=string("Style id."),
        name=string("Display name."),
        type=string("Style type."),
        was=obj("Id and name before a rename.", id=string("Old id."), name=string("Old name.")),
        merged=strings("Ids merged into this style."),
    ),
)

#: A field write (toc/index/caption/xref/index.mark): tracked insertions, maybe a commit.
FIELD_WRITE_FIELDS: dict[str, Schema] = {
    "dry_run": DRY_RUN,
    "revision_ids": REVISION_IDS,
    "author": AUTHOR,
    "date": DATE,
    "footprint": FOOTPRINT,
    **COMMIT,
}

#: What a paragraph outside the body carries (stories: notes, headers).
STORY_FIELDS: dict[str, Schema] = {
    "story": string("Story kind outside the body: footnote, endnote, header or footer."),
    "note": {
        "type": ["object", "null"],
        "description": "A note's id, displayed number and the body paragraph holding its mark.",
        "properties": {
            "id": integer("Note id."),
            "number": string("Displayed number."),
            "of": string("Address of the paragraph holding the mark."),
        },
    },
    "section": integer("Section (1-based) whose header or footer this is."),
    "kind": string("Header/footer kind: default, first or even."),
    "used_by_sections": arr("Sections that show this header or footer.", {"type": "integer"}),
}

PAGE_LABEL = {"type": ["integer", "string"], "description": "Page number as printed."}

#: What doc_control.update_check.attach adds when the update notice is on.
UPDATE = obj(
    "Opt-in update notice (DOC_CONTROL_UPDATE_CHECK): the newest release.",
    current=string("This version."),
    latest=nullable("string", "Newest release."),
    mcpb_url=nullable("string", "Download of the newest .mcpb bundle."),
    checked_at=string("When it was checked (UTC)."),
    error=string("Why the check failed."),
)

#: One live app as doc_control.live.dispatch.apps() describes it.
LIVE_APP = obj(
    "A live app.",
    app=string("word, libreoffice (or a test adapter)."),
    available=boolean("Installed on this machine."),
    reachable=boolean("Can be driven now."),
    listening=boolean("LibreOffice: its UNO listener is up."),
    version=nullable("string", "App version, when known."),
    reason=string("Why it is not available or reachable."),
)

#: doc.batch: every family's result keys (text/comment, row, revision, style, live).
BATCH_OUTPUT = result(
    ("applied",),
    **BATCH_FIELDS,
    **RESOLUTION_FIELDS,
    **{k: v for k, v in STYLE_WRITE_FIELDS.items() if k not in BATCH_FIELDS},
    **LIVE_FIELDS,
)


def direct_output(kind: str, live: bool) -> Schema:
    """The result of a direct write tool (text.replace, revision.accept...): doc.batch's for
    that one kind."""
    if kind.startswith("revision."):
        keep = ("applied", "dry_run", "preview", "address_map", "footprint", "warnings")
        fields = {k: BATCH_FIELDS[k] for k in keep}
        return result(
            ("applied", "dry_run"),
            **fields,
            **RESOLUTION_FIELDS,
            **COMMIT,
        )
    fields = dict(BATCH_FIELDS)
    fields["applied"] = integer("Edits applied (1).")
    if kind != "comment.add":
        del fields["comment_ids"]
    return result(("applied",), **fields, **(LIVE_FIELDS if live else {}))


DOC_BLOCK = obj(
    "The document read or written: path, sha256 and paragraph count.",
    path=string("Path of the document."),
    sha256=string("sha256 of the file (pass as expect.sha256 to a write)."),
    paragraphs=integer("Body paragraphs."),
    source={
        "type": ["object", "string"],
        "description": "A .txt/.md/.odt source's format, encoding and split mode; "
        '"text" for a text string (text.normalize).',
    },
    characters=integer("text.normalize of a text string: its length."),
)


def envelope_schema(output: Schema) -> Schema:
    """The schema of a whole success envelope whose ``result`` is ``output`` (MCP
    ``outputSchema``; ``structuredContent`` is that envelope)."""
    return {
        "type": "object",
        "properties": {
            "api": {"type": "string", "description": f"API version ({API_VERSION})."},
            "ok": {"type": "boolean", "description": "Always true on success."},
            "op": {"type": "string", "description": "The operation that ran."},
            "doc": DOC_BLOCK,
            "result": {**output, "description": "What the operation returns."},
            "warnings": strings("Things to know about the result."),
            "next": nullable("string", "Cursor for the next page (pass as cursor), or null."),
        },
        "required": ["api", "ok", "op", "result", "warnings", "next"],
    }
