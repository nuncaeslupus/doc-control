"""A small JSON Schema subset validator (stdlib only).

Supported keywords: type, properties, required, additionalProperties (bool),
enum, items, minimum, maximum, minLength, description, default.
`check_schema` verifies a schema uses only that subset, correctly; `validate`
checks a value against a schema that passed it.
"""

from __future__ import annotations

import math
from typing import Any

from doc_control.errors import DocControlError, ErrorCode

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}

_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "items",
        "minimum",
        "maximum",
        "minLength",
        "description",
        "default",
    }
)


def check_schema(schema: Any, path: str = "$") -> list[str]:
    """Return every way `schema` strays from the supported subset; [] means well formed."""
    if not isinstance(schema, dict):
        return [f"{path} must be a schema object"]
    problems = [f"{path}.{k} is not a supported keyword" for k in schema if k not in _KEYWORDS]
    if "type" in schema:
        t = schema["type"]
        names = t if isinstance(t, list) else [t]
        if not names or not all(isinstance(n, str) and n in _TYPES for n in names):
            problems.append(f"{path}.type must name known types: {sorted(_TYPES)}")
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        problems.append(f"{path}.properties must be an object")
        props = {}
    for key, sub in props.items():
        problems += check_schema(sub, f"{path}.properties.{key}")
    if "required" in schema:
        req = schema["required"]
        if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
            problems.append(f"{path}.required must be a list of property names")
        else:
            problems += [f"{path}.required names undeclared {r!r}" for r in req if r not in props]
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        problems.append(f"{path}.additionalProperties must be true or false")
    if "enum" in schema and not (isinstance(schema["enum"], list) and schema["enum"]):
        problems.append(f"{path}.enum must be a non-empty list")
    if "items" in schema:
        problems += check_schema(schema["items"], f"{path}.items")
    for bound in ("minimum", "maximum"):
        if bound in schema and not _is_finite_number(schema[bound]):
            problems.append(f"{path}.{bound} must be a finite number")
    if "minLength" in schema:
        n = schema["minLength"]
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            problems.append(f"{path}.minLength must be a non-negative integer")
    if "description" in schema and not isinstance(schema["description"], str):
        problems.append(f"{path}.description must be a string")
    return problems


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Raise SCHEMA_INVALID with the JSON path of the first violation."""
    expected = schema.get("type")
    names: list[str] = []
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        ok = any(
            isinstance(value, _TYPES[n])
            and not (n in ("integer", "number") and isinstance(value, bool))
            for n in names
        )
        if not ok:
            _fail(path, f"expected {'/'.join(names)}")
    numeric = bool({"integer", "number"} & set(names)) or "minimum" in schema or "maximum" in schema
    if numeric and isinstance(value, float) and not math.isfinite(value):
        _fail(path, "must be a finite number")
    if "enum" in schema and not any(_json_equal(value, v) for v in schema["enum"]):
        _fail(path, f"must be one of {schema['enum']}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        _fail(path, f"shorter than {schema['minLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            _fail(path, f"below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            _fail(path, f"above maximum {schema['maximum']}")
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                _fail(f"{path}.{key}", "is required")
        if schema.get("additionalProperties", True) is False:
            for key in value:
                if key not in props:
                    _fail(f"{path}.{key}", "is not allowed")
        for key, sub in props.items():
            if key in value:
                validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]")


def _json_equal(a: Any, b: Any) -> bool:
    """JSON equality: booleans never equal numbers (Python's True == 1 does not apply)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return False
    return bool(a == b)


def _is_finite_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _fail(path: str, why: str) -> None:
    raise DocControlError(ErrorCode.SCHEMA_INVALID, f"{path} {why}", path=path)
