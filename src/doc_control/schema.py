"""A small JSON Schema subset validator (stdlib only).

Supported keywords: type, properties, required, additionalProperties (bool),
enum, items, minimum, maximum, minLength, description, default.
"""

from __future__ import annotations

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


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Raise SCHEMA_INVALID with the JSON path of the first violation."""
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        ok = any(
            isinstance(value, _TYPES[n])
            and not (n in ("integer", "number") and value is True)
            and not (n in ("integer", "number") and value is False)
            for n in names
        )
        if not ok:
            _fail(path, f"expected {'/'.join(names)}")
    if "enum" in schema and value not in schema["enum"]:
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


def _fail(path: str, why: str) -> None:
    raise DocControlError(ErrorCode.SCHEMA_INVALID, f"{path} {why}", path=path)
