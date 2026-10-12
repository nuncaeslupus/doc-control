"""`recipe.list` and `recipe.run`.

``recipe.run`` never writes: it returns ``result.batch``, a ready-to-send ``batch``
request (``doc``, ``ops`` and ``expect.sha256``), so applying a recipe is always a
separate, reviewable ``batch`` call. ``dry_run`` is accepted for symmetry with ``batch``
and changes nothing. A recipe is a stored one (``recipe``: name) or an inline
``definition``; both go through the same strict validation (``RECIPE_INVALID``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.recipes.engine import propose
from doc_control.recipes.schema import validate_recipe
from doc_control.recipes.store import catalogue
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
    result: dict[str, Any] = {
        "recipe": recipe["recipe"],
        "applied": False,
        "matches": proposal.matches,
        "passes": proposal.passes,
        "stable": proposal.stable,
        "batch": {
            "doc": request["doc"],
            "ops": proposal.ops,
            "expect": {"sha256": doc.sha256},
        },
    }
    return Reply(result, doc=doc.envelope_doc(), warnings=proposal.warnings)
