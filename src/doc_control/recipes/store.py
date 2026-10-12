"""Where recipes come from: bundled data files and the user store.

User recipes are ``<user home>/recipes/<name>.json`` (``DOC_CONTROL_HOME`` or the
platform default, see :func:`doc_control.safety.store.user_home`). Files are read as plain
JSON and validated against the strict schema; a bad file never stops the others and is
reported by :func:`catalogue`. A user recipe cannot take the name of a bundled one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.recipes.schema import validate_recipe
from doc_control.safety.store import user_home

MAX_RECIPE_BYTES = 64 * 1024
BUNDLED_DIR = Path(__file__).parent / "bundled"


@dataclass(frozen=True)
class Catalogue:
    recipes: dict[str, tuple[str, dict[str, Any]]]  # name -> (source, recipe)
    invalid: list[dict[str, str]]


def user_recipes_dir(env: Mapping[str, str] | None = None) -> Path:
    return user_home(env) / "recipes"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def parse_recipe_file(path: Path) -> dict[str, Any]:
    """Read and validate one recipe file; ``RECIPE_INVALID`` if it is not a valid recipe."""
    try:
        if path.stat().st_size > MAX_RECIPE_BYTES:
            raise DocControlError(ErrorCode.RECIPE_INVALID, f"larger than {MAX_RECIPE_BYTES} bytes")
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constant,
        )
    except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise DocControlError(ErrorCode.RECIPE_INVALID, f"cannot read recipe: {exc}") from exc
    recipe = validate_recipe(raw)
    if path.stem != recipe["recipe"]:
        raise DocControlError(
            ErrorCode.RECIPE_INVALID, f"file name must be {recipe['recipe']}.json"
        )
    return recipe


def _load_dir(
    directory: Path,
    source: str,
    found: dict[str, tuple[str, dict[str, Any]]],
    bad: list[dict[str, str]],
) -> None:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        try:
            if path.is_symlink():
                raise DocControlError(ErrorCode.RECIPE_INVALID, "symbolic links are not followed")
            recipe = parse_recipe_file(path)
            if recipe["recipe"] in found:
                raise DocControlError(
                    ErrorCode.RECIPE_INVALID, f"the name {recipe['recipe']} is already taken"
                )
        except DocControlError as err:
            bad.append({"file": path.name, "source": source, "error": err.message})
            continue
        found[recipe["recipe"]] = (source, recipe)


def catalogue(env: Mapping[str, str] | None = None, bundled_dir: Path | None = None) -> Catalogue:
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    bad: list[dict[str, str]] = []
    _load_dir(BUNDLED_DIR if bundled_dir is None else bundled_dir, "bundled", found, bad)
    _load_dir(user_recipes_dir(env), "user", found, bad)
    return Catalogue(found, bad)
