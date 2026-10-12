"""Operation registry, schema validation, envelope and CLI contract."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.errors import HINTS, DocControlError, ErrorCode
from doc_control.registry import load_operations
from doc_control.schema import validate


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, Any]]:
    code = main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_registry_every_op_has_schema_and_appears_in_capabilities(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every registered op has an object schema and is listed by `capabilities`."""
    registry = load_operations()
    code, env = run_cli(capsys, "capabilities")
    listed = {op["name"] for op in env["result"]["operations"]}
    missing_schema = [n for n, op in registry.items() if op.schema.get("type") != "object"]
    assert code == 0
    assert missing_schema == []
    assert listed == set(registry)


def test_every_error_code_has_a_hint() -> None:
    """Each stable error code carries a one-line hint."""
    assert set(HINTS) == set(ErrorCode)
    assert all(hint and "\n" not in hint for hint in HINTS.values())


def test_unknown_operation_returns_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """An unknown op exits 2 with SCHEMA_INVALID and a hint."""
    code, env = run_cli(capsys, "no.such.op")
    assert code == 2
    assert env["ok"] is False
    assert env["error"]["code"] == "SCHEMA_INVALID"
    assert env["error"]["hint"]


def test_help_returns_schema_for_one_operation(capsys: pytest.CaptureFixture[str]) -> None:
    """`help` with an op name returns that op's JSON Schema."""
    code, env = run_cli(capsys, "help", "--json", '{"op": "capabilities"}')
    assert code == 0
    assert env["result"]["operations"]["capabilities"]["schema"]["type"] == "object"


def test_request_with_unknown_key_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """Strict schemas reject keys they do not declare, naming the JSON path."""
    code, env = run_cli(capsys, "capabilities", "--json", '{"bogus": 1}')
    assert code == 2
    assert env["error"]["details"]["path"] == "$.bogus"


def test_non_json_request_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """A request that is not JSON returns SCHEMA_INVALID instead of a traceback."""
    code, env = run_cli(capsys, "help", "--json", "{not json")
    assert code == 2
    assert env["error"]["code"] == "SCHEMA_INVALID"


def test_cli_reads_request_from_stdin_only_with_json_dash() -> None:
    """`--json -` reads the request from stdin; without it stdin is never read."""
    out = subprocess.run(
        [sys.executable, "-m", "doc_control", "help", "--json", "-"],
        input='{"op": "help"}',
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    assert json.loads(out)["result"]["operations"]["help"]["kind"] == "read"


def test_cli_does_not_block_on_open_stdin() -> None:
    """With stdin left open and no --json, the CLI answers without reading it."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "doc_control", "capabilities"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        out = proc.stdout.read() if proc.stdout else ""
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.stdin:
            proc.stdin.close()
    assert json.loads(out)["ok"] is True


@pytest.mark.parametrize(
    ("value", "schema"),
    [
        ("x", {"type": "integer"}),
        (True, {"type": "integer"}),
        ({}, {"type": "object", "required": ["doc"]}),
        ([1, "a"], {"type": "array", "items": {"type": "integer"}}),
        ("b", {"enum": ["a"]}),
        (0, {"type": "integer", "minimum": 1}),
    ],
)
def test_validate_rejects_invalid_values(value: Any, schema: dict[str, Any]) -> None:
    """The schema subset rejects wrong types, missing keys, bad items and enums."""
    with pytest.raises(DocControlError) as err:
        validate(value, schema)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
