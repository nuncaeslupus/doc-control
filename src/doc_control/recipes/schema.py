"""The strict schema of a declarative recipe, and its validation.

A recipe is pure data. Every object, at every level, rejects keys the schema does not
list (``RECIPE_INVALID``), so a recipe has no way to name code, files or the network. The
only things a recipe can ask for are the batch operations in :data:`PROPOSABLE`, aimed at
``$match`` (the matched range) or ``$para`` (its paragraph). Text may use ``$match``,
``$para`` and ``$1``..``$9`` (``$$`` is a literal dollar sign); nothing is evaluated.
"""

from __future__ import annotations

import re
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.search import Matcher, SearchOptions
from doc_control.ops.comments import COMMENT_SCHEMAS, comment_from_dict
from doc_control.ops.tracked import OPERATION_SCHEMAS, edit_from_dict
from doc_control.schema import validate

RECIPE_VERSION = 1
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SUBSTITUTION = re.compile(r"\$(\$|match|para|[1-9])")
PLACEHOLDER = "p1#00000000"  # stands in for `at` when checking an operation's other fields
TARGETS = ("$match", "$para")
PROPOSABLE = ("text.replace", "format.apply", "style.apply", "para.delete", "comment.add")
MAX_PROPOSE = 8
MAX_PASSES = 10
DEFAULT_PASSES = 4

_FIND: dict[str, Any] = {
    "type": "object",
    "properties": {
        "regex": {"type": "string", "minLength": 1},
        "literal": {"type": "string", "minLength": 1},
        "ignore_case": {"type": "boolean"},
        "ignore_accents": {"type": "boolean"},
        "whole_word": {"type": "boolean"},
        "scope": {"enum": ["paragraph"]},
    },
    "additionalProperties": False,
}

RECIPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recipe": {"type": "string", "minLength": 1},
        "version": {"enum": [RECIPE_VERSION]},
        "description": {"type": "string"},
        "match": {
            "type": "object",
            "properties": {"find": _FIND},
            "required": ["find"],
            "additionalProperties": False,
        },
        "propose": {"type": "array", "items": {"type": "object"}},
        "repeat_until_stable": {"type": "boolean"},
        "max_passes": {"type": "integer", "minimum": 1, "maximum": MAX_PASSES},
    },
    "required": ["recipe", "version", "match", "propose"],
    "additionalProperties": False,
}


def _invalid(message: str, **details: Any) -> DocControlError:
    return DocControlError(ErrorCode.RECIPE_INVALID, message, **details)


def _checked(value: Any, schema: dict[str, Any], path: str) -> None:
    try:
        validate(value, schema, path)
    except DocControlError as err:
        raise _invalid(err.message, **err.details) from err


def search_options(recipe: dict[str, Any]) -> SearchOptions:
    find = recipe["match"]["find"]
    return SearchOptions(
        find.get("regex", find.get("literal")),
        regex="regex" in find,
        ignore_case=find.get("ignore_case", False),
        ignore_accents=find.get("ignore_accents", False),
        whole_word=find.get("whole_word", False),
    )


def validate_recipe(raw: Any) -> dict[str, Any]:
    """Return ``raw`` if it is a well-formed recipe, else raise ``RECIPE_INVALID``."""
    _checked(raw, RECIPE_SCHEMA, "$")
    if NAME.fullmatch(raw["recipe"]) is None:
        raise _invalid("$.recipe: use lower-case letters, digits and hyphens (at most 64)")
    find = raw["match"]["find"]
    if ("regex" in find) == ("literal" in find):
        raise _invalid("$.match.find: give exactly one of `regex` and `literal`")
    options = search_options(raw)
    try:
        matcher = Matcher(options)
    except DocControlError as err:
        raise _invalid(f"$.match.find: {err.message}") from err
    groups = matcher.pattern.groups if options.regex else 0
    steps = raw["propose"]
    if not 1 <= len(steps) <= MAX_PROPOSE:
        raise _invalid(f"$.propose: give 1 to {MAX_PROPOSE} operations")
    repeat = raw.get("repeat_until_stable", False)
    if "max_passes" in raw and not repeat:
        raise _invalid("$.max_passes: only meaningful with `repeat_until_stable`")
    kinds = [
        _check_step(step, f"$.propose[{i}]", groups, options, repeat)
        for i, step in enumerate(steps)
    ]
    if kinds.count("text.replace") > 1:
        raise _invalid("$.propose: at most one text.replace")
    if "para.delete" in kinds and len(kinds) > 1:
        raise _invalid("$.propose: para.delete cannot be combined with other operations")
    return dict(raw)


def _check_step(step: Any, path: str, groups: int, options: SearchOptions, repeat: bool) -> str:
    if not isinstance(step, dict) or step.get("op") not in PROPOSABLE:
        raise _invalid(f"{path}.op: must be one of {list(PROPOSABLE)}")
    kind: str = step["op"]
    _checked(step, OPERATION_SCHEMAS.get(kind) or COMMENT_SCHEMAS[kind], path)
    at = step["at"]
    if at not in TARGETS or (kind == "para.delete" and at != "$para"):
        allowed = "$para" if kind == "para.delete" else "$match or $para"
        raise _invalid(f"{path}.at: must be {allowed}")
    if repeat and at == "$match" and kind != "text.replace":
        raise _invalid(f"{path}.at: with `repeat_until_stable` only text.replace may target $match")
    for ref in SUBSTITUTION.findall(step.get("text", "")):
        if ref.isdigit() and int(ref) > groups:
            raise _invalid(f"{path}.text: ${ref} but the pattern has {groups} group(s)")
        if ref.isdigit() and options.ignore_accents:
            raise _invalid(f"{path}.text: groups cannot be used with `ignore_accents`")
    probe = {**step, "at": PLACEHOLDER}
    try:
        if kind == "comment.add":
            comment_from_dict(probe)
        else:
            edit_from_dict(probe)
    except DocControlError as err:
        raise _invalid(f"{path}: {err.message}") from err
    return kind
