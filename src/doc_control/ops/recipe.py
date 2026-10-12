"""`recipe.list` and `recipe.run`.

``recipe.run`` never writes: it returns ``result.batch``, a ready-to-send ``batch``
request (``doc``, ``ops`` and ``expect.sha256``), so applying a recipe is always a
separate, reviewable ``batch`` call. ``dry_run`` is accepted for symmetry with ``batch``
and changes nothing. A recipe is a stored one (``recipe``: name) or an inline
``definition``; both go through the same strict validation (``RECIPE_INVALID``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.layout import centred
from doc_control.ops.tracked import apply_edits, edit_from_dict
from doc_control.recipes.engine import propose
from doc_control.recipes.schema import validate_recipe
from doc_control.recipes.store import MAX_RECIPE_BYTES, catalogue
from doc_control.registry import operation

_LIST: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

_RUN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx file."},
        "recipe": {"type": "string", "minLength": 1, "description": "Name from recipe.list."},
        "definition": {
            "type": "object",
            "description": "An inline declarative recipe (same schema as a stored one).",
        },
        "dry_run": {
            "type": "boolean",
            "description": "Accepted for symmetry; recipe.run never writes.",
        },
    },
    "required": ["doc"],
    "additionalProperties": False,
}


@operation(
    "recipe.list",
    "read",
    "List the bundled and user recipes, and any recipe files that are invalid.",
    _LIST,
)
def recipe_list(_: dict[str, Any]) -> dict[str, Any]:
    cat = catalogue()
    return {
        "recipes": [
            {
                "name": name,
                "version": recipe["version"],
                "description": recipe.get("description", ""),
                "source": source,
            }
            for name, (source, recipe) in sorted(cat.recipes.items())
        ],
        "invalid": cat.invalid,
    }


def _prune(
    doc: Document, path: Path, ops: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Drop proposed operations that cannot or need not be applied; report each as skipped.

    A ``format.apply`` that only centres a paragraph that is already centred changes
    nothing. A ``para.delete`` the engine refuses for structural reasons (the last
    paragraph of a cell, section or body, ...) would fail the whole all-or-nothing
    batch, so it is tried on its own in a dry run and left out when refused.
    """
    skipped: list[dict[str, str]] = []
    kept: list[dict[str, Any]] = []
    for op in ops:
        keys = set(op) - {"op", "at"}
        if op["op"] == "format.apply" and keys == {"align"} and op["align"] == "center":
            try:
                already = centred(doc, doc.index.resolve_address(op["at"]))
            except DocControlError:
                already = False
            if already:
                continue  # nothing to do: not worth reporting
        kept.append(op)
    deletes = [op for op in kept if op["op"] == "para.delete"]
    if deletes and _refusal(path, deletes) is not None:
        # Each delete may pass alone yet fail with the others (emptying a cell), so
        # the set is grown one op at a time and every op it refuses is left out.
        accepted: list[dict[str, Any]] = []
        for op in deletes:
            reason = _refusal(path, [*accepted, op])
            if reason is None:
                accepted.append(op)
            else:
                kept.remove(op)
                skipped.append({"op": op["op"], "at": op["at"], "reason": reason})
    return kept, skipped


def _refusal(path: Path, ops: list[dict[str, Any]]) -> str | None:
    """The engine's structural refusal of ``ops`` in a dry run, or None when they apply."""
    try:
        apply_edits(path, [edit_from_dict(op) for op in ops], dry_run=True)
    except DocControlError as err:
        if err.code is not ErrorCode.UNSUPPORTED_CONTENT:
            raise
        return err.message
    return None


@operation(
    "recipe.run",
    "write",
    "Run a recipe and return a batch proposal (repeated until stable); apply it with batch.",
    _RUN,
)
def recipe_run(request: dict[str, Any]) -> Reply:
    if ("recipe" in request) == ("definition" in request):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, "give exactly one of `recipe` and `definition`"
        )
    if "definition" in request:
        size = len(json.dumps(request["definition"], ensure_ascii=False).encode("utf-8"))
        if size > MAX_RECIPE_BYTES:
            raise DocControlError(
                ErrorCode.RECIPE_INVALID, f"definition is larger than {MAX_RECIPE_BYTES} bytes"
            )
        recipe = validate_recipe(request["definition"])
    else:
        found = catalogue().recipes.get(request["recipe"])
        if found is None:
            raise DocControlError(
                ErrorCode.RECIPE_INVALID,
                f"no recipe named {request['recipe']!r}; list them with recipe.list",
            )
        recipe = found[1]
    doc = Document(Path(request["doc"]).as_posix())
    paragraphs = [(p.address, p.text) for p in doc.index.paragraphs if p.text]
    proposal = propose(recipe, paragraphs)
    ops, skipped = _prune(doc, Path(request["doc"]), proposal.ops)
    warnings = [*proposal.warnings, *(f"SKIPPED: {s['at']} {s['reason']}" for s in skipped)]
    result: dict[str, Any] = {
        "recipe": recipe["recipe"],
        "applied": False,
        "matches": proposal.matches,
        "passes": proposal.passes,
        "stable": proposal.stable,
        "skipped": skipped,
        "batch": {
            "doc": request["doc"],
            "ops": ops,
            "expect": {"sha256": doc.sha256},
        },
    }
    return Reply(result, doc=doc.envelope_doc(), warnings=warnings)
