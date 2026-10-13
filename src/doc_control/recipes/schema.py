"""The strict schema of a declarative recipe, and its validation.

A recipe is pure data. Every object, at every level, rejects keys the schema does not
list (``RECIPE_INVALID``), so a recipe has no way to name code, files or the network. The
only things a recipe can ask for are the batch operations in :data:`PROPOSABLE`, aimed at
``$match`` (the matched range) or ``$para`` (its paragraph). Text may use ``$match``,
``$para`` and ``$1``..``$9`` (``$$`` is a literal dollar sign); nothing is evaluated.

``match.where`` adds context: conditions on the paragraph's own style, heading status and
emptiness, and on its previous and next paragraphs (``prev[0]`` is the one just before).
Empty paragraphs are scanned only by a rule that asks for them (``where.empty: true``). A
rule without ``find`` matches its whole paragraph.
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
MAX_NEIGHBOURS = 3
MAX_STYLES = 16

_STYLES: dict[str, Any] = {"type": "array", "items": {"type": "string", "minLength": 1}}
_COND_KEYS: dict[str, Any] = {
    "empty": {"type": "boolean"},
    "heading": {"type": "boolean"},
    "style": _STYLES,
    "not_style": _STYLES,
}
_NEIGHBOUR: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_COND_KEYS,
        "missing": {"type": "boolean"},
        "text": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}
_WHERE: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_COND_KEYS,
        "skip_empty": {"type": "boolean"},
        "prev": {"type": "array", "items": _NEIGHBOUR},
        "next": {"type": "array", "items": _NEIGHBOUR},
    },
    "additionalProperties": False,
}

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
            "properties": {"find": _FIND, "where": _WHERE},
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


def search_options(recipe: dict[str, Any]) -> SearchOptions | None:
    """The search of ``match.find``, or None for a rule that matches whole paragraphs."""
    find = recipe["match"].get("find")
    if find is None:
        return None
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
    match = raw["match"]
    if not match:
        raise _invalid("$.match: give `find`, `where` or both")
    repeat = raw.get("repeat_until_stable", False)
    where = match.get("where", {})
    if "where" in match and not where:
        raise _invalid("$.match.where: give at least one condition")
    _check_where(where)
    options = search_options(raw)
    groups = 0
    if options is not None:
        find = match["find"]
        if ("regex" in find) == ("literal" in find):
            raise _invalid("$.match.find: give exactly one of `regex` and `literal`")
        try:
            matcher = Matcher(options)
        except DocControlError as err:
            raise _invalid(f"$.match.find: {err.message}") from err
        groups = matcher.pattern.groups if options.regex else 0
        if where.get("empty"):
            raise _invalid("$.match: an empty paragraph has no text to `find` in")
    elif repeat:
        raise _invalid("$.repeat_until_stable: needs `find`")
    steps = raw["propose"]
    if not 1 <= len(steps) <= MAX_PROPOSE:
        raise _invalid(f"$.propose: give 1 to {MAX_PROPOSE} operations")
    if "max_passes" in raw and not repeat:
        raise _invalid("$.max_passes: only meaningful with `repeat_until_stable`")
    kinds = [
        _check_step(step, f"$.propose[{i}]", groups, bool(where.get("empty")), repeat)
        for i, step in enumerate(steps)
    ]
    if kinds.count("text.replace") > 1:
        raise _invalid("$.propose: at most one text.replace")
    if "para.delete" in kinds and len(kinds) > 1:
        raise _invalid("$.propose: para.delete cannot be combined with other operations")
    return dict(raw)


def _check_where(where: dict[str, Any]) -> None:
    if not where:
        return
    if set(where) == {"skip_empty"}:
        raise _invalid("$.match.where: give at least one condition")
    _check_cond(where, "$.match.where")
    for side in ("prev", "next"):
        conds = where.get(side, [])
        if len(conds) > MAX_NEIGHBOURS:
            raise _invalid(f"$.match.where.{side}: at most {MAX_NEIGHBOURS} paragraphs")
        for i, cond in enumerate(conds):
            path = f"$.match.where.{side}[{i}]"
            if not cond:
                raise _invalid(f"{path}: give at least one condition")
            if cond.get("missing") and len(cond) > 1:
                raise _invalid(f"{path}: `missing: true` takes no other condition")
            _check_cond(cond, path)
            if cond.get("empty") and where.get("skip_empty"):
                raise _invalid(f"{path}.empty: `skip_empty` never sees an empty neighbour")
            if "text" in cond:
                try:
                    Matcher(SearchOptions(cond["text"], regex=True))
                except DocControlError as err:
                    raise _invalid(f"{path}.text: {err.message}") from err


def _check_cond(cond: dict[str, Any], path: str) -> None:
    for key in ("style", "not_style"):
        if key in cond and not 1 <= len(cond[key]) <= MAX_STYLES:
            raise _invalid(f"{path}.{key}: give 1 to {MAX_STYLES} style ids or names")
    if cond.get("empty") and "text" in cond:
        raise _invalid(f"{path}: an empty paragraph has no `text`")


def _check_step(step: Any, path: str, groups: int, empty: bool, repeat: bool) -> str:
    if not isinstance(step, dict) or step.get("op") not in PROPOSABLE:
        raise _invalid(f"{path}.op: must be one of {list(PROPOSABLE)}")
    kind: str = step["op"]
    _checked(step, OPERATION_SCHEMAS.get(kind) or COMMENT_SCHEMAS[kind], path)
    at = step["at"]
    if at not in TARGETS or (kind == "para.delete" and at != "$para"):
        allowed = "$para" if kind == "para.delete" else "$match or $para"
        raise _invalid(f"{path}.at: must be {allowed}")
    if empty and at == "$match":
        raise _invalid(f"{path}.at: an empty paragraph has no text; use $para")
    if empty and kind == "format.apply":
        raise _invalid(f"{path}.op: an empty paragraph has no text to format")
    if repeat and at == "$match" and kind != "text.replace":
        raise _invalid(f"{path}.at: with `repeat_until_stable` only text.replace may target $match")
    for ref in SUBSTITUTION.findall(step.get("text", "")):
        if ref.isdigit() and int(ref) > groups:
            raise _invalid(f"{path}.text: ${ref} but the pattern has {groups} group(s)")
    probe = {**step, "at": PLACEHOLDER}
    try:
        if kind == "comment.add":
            comment_from_dict(probe)
        else:
            edit_from_dict(probe)
    except DocControlError as err:
        raise _invalid(f"{path}: {err.message}") from err
    return kind
