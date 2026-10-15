"""Operation registry, schema validation, envelope and CLI contract."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.errors import HINTS, DocControlError, ErrorCode
from doc_control.registry import (
    ALIASES,
    REGISTRY,
    canonical,
    get_operation,
    load_operations,
    operation,
)
from doc_control.schema import check_schema, validate


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, Any]]:
    code = main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_registry_every_op_has_schema_and_appears_in_capabilities(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every registered schema is well formed and `server.capabilities` lists exactly the ops.

    `check_schema` inspects the schema itself (keywords, nesting, required names), so this
    fails if an op ships a schema the validator would silently misread.
    """
    registry = load_operations()
    code, env = run_cli(capsys, "server.capabilities")
    listed = [op["name"] for op in env["result"]["operations"]]
    failing = {n: check_schema(op.schema) for n, op in registry.items() if check_schema(op.schema)}
    not_object = [n for n, op in registry.items() if op.schema.get("type") != "object"]
    assert code == 0
    assert failing == {}
    assert not_object == []
    assert sorted(listed) == sorted(registry)
    assert len(listed) == len(set(listed))


_MALFORMED: list[dict[str, Any]] = [
    {"type": "object", "propertes": {}},
    {"type": "object", "properties": {"a": "string"}},
    {"type": "object", "properties": {"a": {"type": "strng"}}},
    {"type": "object", "properties": {}, "required": ["doc"]},
    {"type": "object", "required": "doc", "properties": {"doc": {"type": "string"}}},
    {"type": "object", "additionalProperties": {"type": "string"}},
    {"type": "object", "properties": {"a": {"enum": []}}},
    {"type": "object", "properties": {"a": {"enum": "ab"}}},
    {"type": "object", "properties": {"a": {"type": "array", "items": "x"}}},
    {"type": "object", "properties": {"a": {"type": "number", "minimum": math.nan}}},
    {"type": "object", "properties": {"a": {"type": "number", "maximum": math.inf}}},
    {"type": "object", "properties": {"a": {"type": "string", "minLength": -1}}},
    {"type": []},
]


@pytest.mark.parametrize("schema", _MALFORMED)
def test_check_schema_reports_malformed_schemas(schema: dict[str, Any]) -> None:
    """The well-formedness checker flags unknown keywords, bad nesting and bad values."""
    assert check_schema(schema) != []


def test_registering_an_op_with_a_malformed_schema_fails() -> None:
    """`operation()` runs `check_schema`, so a malformed schema never reaches the registry."""
    bad = {"type": "object", "properties": {"doc": {"type": "strng"}}, "required": ["dco"]}
    with pytest.raises(ValueError, match=r"test\.malformed is malformed"):
        operation("test.malformed", "read", "never registered", bad)(lambda r: r)
    assert "test.malformed" not in REGISTRY


def test_check_schema_accepts_the_supported_subset() -> None:
    """A schema using every supported keyword passes."""
    schema = {
        "type": "object",
        "description": "d",
        "properties": {
            "n": {"type": ["integer", "null"], "minimum": 0, "maximum": 9, "default": 1},
            "s": {"type": "string", "minLength": 1, "enum": ["a", "b"]},
            "l": {"type": "array", "items": {"type": "boolean"}},
        },
        "required": ["n"],
        "additionalProperties": False,
    }
    assert check_schema(schema) == []


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
    """`server.help` with an op name returns that op's JSON Schema."""
    code, env = run_cli(capsys, "server.help", "--json", '{"op": "server.capabilities"}')
    assert code == 0
    assert env["result"]["operations"]["server.capabilities"]["schema"]["type"] == "object"


def test_help_accepts_positional_op_as_the_hint_says(capsys: pytest.CaptureFixture[str]) -> None:
    """`doc-control server.help <op>`, which the SCHEMA_INVALID hint names, works as written."""
    assert "`doc-control server.help <op>`" in HINTS[ErrorCode.SCHEMA_INVALID]
    code, env = run_cli(capsys, "server.help", "server.capabilities")
    assert code == 0
    assert list(env["result"]["operations"]) == ["server.capabilities"]
    code, env = run_cli(capsys, "server.help", "no.such.op")
    assert code == 2
    assert env["error"]["code"] == "SCHEMA_INVALID"


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_flags_are_aliases_of_help(capsys: pytest.CaptureFixture[str], flag: str) -> None:
    """`--help` and `-h` behave as `server.help`, with or without a positional op."""
    code, env = run_cli(capsys, flag)
    assert code == 0
    assert env["op"] == "server.help"
    assert "server.capabilities" in env["result"]["operations"]
    code, env = run_cli(capsys, flag, "server.capabilities")
    assert code == 0
    assert list(env["result"]["operations"]) == ["server.capabilities"]


@pytest.mark.parametrize(
    "args",
    [
        ["server.capabilities", "server.help"],
        ["server.help", "server.capabilities", "extra"],
        ["server.help", "a", "--json", "{}"],
        ["help", "capabilities", "extra"],
    ],
)
def test_positional_args_are_rejected_except_one_op_for_help(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    """Only `server.help` (or its old name `help`) takes a positional op, and only one."""
    code, env = run_cli(capsys, *args)
    assert code == 2
    assert env["error"]["code"] == "SCHEMA_INVALID"


def test_request_with_unknown_key_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """Strict schemas reject keys they do not declare, naming the JSON path."""
    code, env = run_cli(capsys, "server.capabilities", "--json", '{"bogus": 1}')
    assert code == 2
    assert env["error"]["details"]["path"] == "$.bogus"


def test_non_json_request_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """A request that is not JSON returns SCHEMA_INVALID instead of a traceback."""
    code, env = run_cli(capsys, "server.help", "--json", "{not json")
    assert code == 2
    assert env["error"]["code"] == "SCHEMA_INVALID"


def test_cli_reads_request_from_stdin_only_with_json_dash() -> None:
    """`--json -` reads the request from stdin; without it stdin is never read."""
    out = subprocess.run(
        [sys.executable, "-m", "doc_control", "server.help", "--json", "-"],
        input='{"op": "server.help"}',
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    assert json.loads(out)["result"]["operations"]["server.help"]["kind"] == "read"


def test_cli_does_not_block_on_open_stdin() -> None:
    """With stdin left open and no --json, the CLI answers without reading it.

    The write end of the stdin pipe stays open for the whole wait, so a regression that
    reads stdin blocks the child; the timeout then kills it and the test fails, not hangs.
    """
    read_end, write_end = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "doc_control", "server.capabilities"],
            stdin=read_end,
            stdout=subprocess.PIPE,
            text=True,
        )
        os.close(read_end)
        read_end = -1
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("CLI blocked on an open stdin it was not asked to read")
    finally:
        if read_end != -1:
            os.close(read_end)
        os.close(write_end)
    assert proc.returncode == 0
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
        (1, {"enum": [True]}),
        (True, {"enum": [1]}),
        (0, {"enum": [False]}),
        (False, {"enum": [0, None]}),
        ([1], {"enum": [[True]]}),
        ({"a": True}, {"enum": [{"a": 1}]}),
        (math.nan, {"type": "number"}),
        (math.inf, {"type": "number"}),
        (-math.inf, {"type": "number"}),
        (math.nan, {"type": "number", "minimum": 0}),
        (math.nan, {"minimum": 0}),
        (math.inf, {"maximum": 10}),
        (math.nan, {"type": "integer"}),
    ],
)
def test_validate_rejects_invalid_values(value: Any, schema: dict[str, Any]) -> None:
    """The schema subset rejects wrong types, missing keys, bad items and enums."""
    with pytest.raises(DocControlError) as err:
        validate(value, schema)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


@pytest.mark.parametrize(
    ("value", "schema"),
    [
        (True, {"enum": [True]}),
        (1, {"enum": [1, True]}),
        (1.0, {"enum": [1]}),
        ([True], {"enum": [[True]]}),
        (1.5, {"type": "number", "minimum": 1, "maximum": 2}),
    ],
)
def test_validate_accepts_valid_values(value: Any, schema: dict[str, Any]) -> None:
    """Enum matches respect bool vs number; finite numbers inside bounds pass."""
    validate(value, schema)


def test_operation_modules_name_the_module_of_every_operation() -> None:
    from doc_control.registry import OPERATION_MODULES, load_operations

    registry = load_operations()
    assert {name: op.handler.__module__.rsplit(".", 1)[1] for name, op in registry.items()} == (
        OPERATION_MODULES
    )


def test_every_old_name_resolves_to_a_current_operation() -> None:
    """Each name an operation had before 0.7.0 runs the operation it was renamed to."""
    registry = load_operations()
    assert not set(ALIASES) & set(registry)  # an old name never shadows a current one
    for old, new in ALIASES.items():
        assert new in registry, old
        assert canonical(old) == new
        assert get_operation(old) is registry[new]


@pytest.mark.parametrize("old", sorted(ALIASES))
def test_old_names_still_run_on_every_front_door(
    capsys: pytest.CaptureFixture[str], old: str
) -> None:
    """The CLI, help's `op` and the Python API all accept an old name."""
    from doc_control import api

    new = ALIASES[old]
    code, env = run_cli(capsys, "help", old)
    assert code == 0 and list(env["result"]["operations"]) == [new]
    if old == "capabilities":
        code, env = run_cli(capsys, old)
        assert code == 0 and env["op"] == new
        assert api.run(old, {}).result["version"]


def test_old_names_are_not_listed(capsys: pytest.CaptureFixture[str]) -> None:
    code, env = run_cli(capsys, "server.capabilities")
    listed = {op["name"] for op in env["result"]["operations"]}
    assert code == 0 and not listed & set(ALIASES)
    code, env = run_cli(capsys, "server.help")
    assert code == 0 and not set(env["result"]["operations"]) & set(ALIASES)
