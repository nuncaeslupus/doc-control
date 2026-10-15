"""Command-line front door: `doc-control <op> [--json '<request>' | --json - < request.json]`.

`doc-control server.help <op>` is shorthand for
`doc-control server.help --json '{"op": "<op>"}'`, and `help`, `--help` and `-h` are aliases
of `server.help`. Names operations had before 0.7.0 (`registry.ALIASES`) still run.
`doc-control mcp` serves the same operations as an MCP server on stdio (see `doc_control.mcp`).

Never prompts and never reads stdin unless asked with `--json -`, so an agent whose
stdin is an open pipe cannot hang it. Prints exactly one JSON envelope on stdout. Exit codes:
0 success, 1 operation error, 2 usage error (unknown op or invalid request).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from doc_control.envelope import Reply, failure, success
from doc_control.errors import DocControlError, ErrorCode
from doc_control.registry import Operation, canonical, load_operations

_USAGE_CODES = {ErrorCode.SCHEMA_INVALID}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    registry = load_operations()
    op = args[0] if args else "server.help"
    if op == "mcp":
        from doc_control.mcp import serve

        return serve()
    if op in ("--version", "version"):
        op = "server.capabilities"
    elif op in ("--help", "-h"):
        op = "server.help"
    op = canonical(op)
    rest = args[1:]
    try:
        if op == "server.help" and len(rest) == 1 and not rest[0].startswith("-"):
            request: dict[str, Any] = {"op": rest[0]}
        else:
            request = _read_request(rest)
    except DocControlError as err:
        envelope, code = failure(op, err), 2
    else:
        envelope, code = execute(registry, op, request)
    _write_utf8(json.dumps(envelope, ensure_ascii=False) + "\n")
    return code


def _utf8(stream: Any) -> None:
    """UTF-8 on ``stream`` whatever the locale (Windows pipes default to cp1252)."""
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


def _write_utf8(text: str) -> None:
    _utf8(sys.stdout)
    sys.stdout.write(text)
    sys.stdout.flush()


def execute(
    registry: dict[str, Operation], op: str, request: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run one operation; return its envelope and the CLI exit code. Never raises."""
    op = canonical(op)
    try:
        if op not in registry:
            raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown operation: {op}", op=op)
        out = registry[op].run(request)
        if isinstance(out, Reply):
            return success(
                op, out.result, doc=out.doc, warnings=out.warnings, next_cursor=out.next_cursor
            ), 0
        return success(op, out), 0
    except DocControlError as err:
        return failure(op, err), 2 if err.code in _USAGE_CODES else 1
    except Exception as err:  # never leak a traceback instead of an envelope
        internal = DocControlError(ErrorCode.INTERNAL, f"{type(err).__name__}: {err}")
        return failure(op, internal), 1


def _read_request(rest: list[str]) -> dict[str, Any]:
    raw: str | None = None
    if rest[:1] == ["--json"]:
        if len(rest) < 2:
            raise DocControlError(ErrorCode.SCHEMA_INVALID, "--json needs a value")
        if rest[1] == "-":
            _utf8(sys.stdin)
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
