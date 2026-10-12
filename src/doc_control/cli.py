"""Command-line front door: `doc-control <op> [--json '<request>' | --json - < request.json]`.

`doc-control help <op>` is shorthand for `doc-control help --json '{"op": "<op>"}'`, and
`--help` / `-h` are aliases of `help`.

Never prompts and never reads stdin unless asked with `--json -`, so an agent whose
stdin is an open pipe cannot hang it. Prints exactly one JSON envelope on stdout. Exit codes:
0 success, 1 operation error, 2 usage error (unknown op or invalid request).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from doc_control.envelope import failure, success
from doc_control.errors import DocControlError, ErrorCode
from doc_control.registry import load_operations

_USAGE_CODES = {ErrorCode.SCHEMA_INVALID}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    registry = load_operations()
    op = args[0] if args else "help"
    if op in ("--version", "version"):
        op = "capabilities"
    elif op in ("--help", "-h"):
        op = "help"
    try:
        rest = args[1:]
        if op == "help" and len(rest) == 1 and not rest[0].startswith("-"):
            request: dict[str, Any] = {"op": rest[0]}
        else:
            request = _read_request(rest)
        if op not in registry:
            raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown operation: {op}", op=op)
        envelope = success(op, registry[op].run(request))
        code = 0
    except DocControlError as err:
        envelope = failure(op, err)
        code = 2 if err.code in _USAGE_CODES else 1
    except Exception as err:  # never leak a traceback instead of an envelope
        envelope = failure(op, DocControlError(ErrorCode.INTERNAL, f"{type(err).__name__}: {err}"))
        code = 1
    print(json.dumps(envelope, ensure_ascii=False))
    return code


def _read_request(rest: list[str]) -> dict[str, Any]:
    raw: str | None = None
    if rest[:1] == ["--json"]:
        if len(rest) < 2:
            raise DocControlError(ErrorCode.SCHEMA_INVALID, "--json needs a value")
        raw = sys.stdin.read() if rest[1] == "-" else rest[1]
        if len(rest) > 2:
            raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unexpected arguments: {rest[2:]}")
    elif rest:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unexpected arguments: {rest}")
    if not raw or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as err:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"request is not JSON: {err}") from err
    if not isinstance(value, dict):
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "request must be a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
