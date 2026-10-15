"""Shared fixtures: synthetic corpus documents written to tmp paths."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.corpus.generator import generate

CorpusWriter = Callable[[str, int], Path]


def write_corpus_doc(directory: Path, kind: str, seed: int = 0) -> Path:
    """Write the synthetic ``kind`` document for ``seed`` under ``directory``."""
    path = directory / f"{kind}-{seed}.docx"
    path.write_bytes(generate(kind, seed))
    return path


@pytest.fixture
def corpus_doc(tmp_path: Path) -> CorpusWriter:
    """Factory fixture: ``corpus_doc(kind, seed)`` returns a path to a fresh .docx."""

    def write(kind: str, seed: int = 0) -> Path:
        return write_corpus_doc(tmp_path, kind, seed)

    return write


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's opt-in must never make the suite reach the network."""
    monkeypatch.delenv("DOC_CONTROL_UPDATE_CHECK", raising=False)


@pytest.fixture(autouse=True)
def _keep_written_documents(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """CI hook: with ``DOC_CONTROL_KEEP_DOCS=<dir>``, copy every package the tool commits.

    The OOXML validation job points this at a directory and lints what was kept
    (``scripts/ci_validate_docs.py``). Without the variable this does nothing.
    """
    target = os.environ.get("DOC_CONTROL_KEEP_DOCS")
    if not target:
        return
    from doc_control.ooxml import zipio
    from doc_control.safety import verify

    keep = Path(target)
    keep.mkdir(parents=True, exist_ok=True)
    original = zipio.commit_bytes

    def recording(payload: bytes, dest: Path, stage: Callable[[str], None] | None) -> None:
        if request.node.get_closest_marker("writes_invalid_docs"):
            original(payload, dest, stage)
            return
        before = dest.read_bytes() if dest.is_file() else None
        original(payload, dest, stage)
        name = hashlib.sha256(payload).hexdigest()[:16]
        (keep / f"{name}.docx").write_bytes(payload)
        (keep / f"{name}.test").write_text(request.node.nodeid, encoding="utf-8")
        if before is not None:
            (keep / f"{name}.before").write_bytes(before)

    monkeypatch.setattr(zipio, "commit_bytes", recording)
    monkeypatch.setattr(verify, "commit_bytes", recording)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """A ``bench`` test named on the command line by its node id runs even under the
    default ``-m 'not bench'`` (a task gate names its benchmark that way), and so does one
    named exactly by ``-k <test name>``."""
    for item in items:
        if _named_on_command_line(config, item):
            item.own_markers = [m for m in item.own_markers if m.name != "bench"]


def _named_on_command_line(config: pytest.Config, item: pytest.Item) -> bool:
    named = {arg for arg in config.args if "::" in arg}
    keyword = str(config.getoption("keyword", "") or "").strip()
    return item.name == keyword or any(
        item.nodeid.endswith(arg.split("/", 1)[-1]) or item.nodeid == arg for arg in named
    )


@pytest.fixture
def gate_mode(request: pytest.FixtureRequest) -> bool:
    """True when this test was named on the command line, which is how a task gate runs
    it. A benchmark that cannot measure must then fail rather than skip: a skip exits 0,
    so the gate would pass with no number behind it."""
    return _named_on_command_line(request.config, request.node)


def strict(schema: Any) -> Any:
    """``schema`` with ``additionalProperties: false`` wherever it lists ``properties`` and
    says nothing about others, so a result key its output schema does not name fails."""
    if not isinstance(schema, dict):
        return schema
    out = {k: strict(v) if k in ("items",) else v for k, v in schema.items()}
    if "properties" in schema:
        out["properties"] = {k: strict(v) for k, v in schema["properties"].items()}
        out.setdefault("additionalProperties", False)
    return out


def check_result(name: str, out: Any) -> list[str]:
    """Every way a handler's return value (as JSON) strays from the output schema of
    operation ``name``, and its ``doc`` block from the envelope's."""
    from doc_control.envelope import Reply
    from doc_control.errors import DocControlError
    from doc_control.outputs import DOC_BLOCK
    from doc_control.registry import REGISTRY
    from doc_control.schema import validate

    output = REGISTRY[name].output
    if output is None:
        return [f"{name} declares no output schema"]
    reply = out if isinstance(out, Reply) else Reply(out)
    checks = [("result", reply.result, output)]
    if reply.doc is not None:
        checks.append(("doc", reply.doc, DOC_BLOCK))
    problems = []
    for label, value, schema in checks:
        try:
            validate(json.loads(json.dumps(value, default=str)), strict(schema))
        except DocControlError as err:
            problems.append(f"{name}: {label} {err.message}")
    return problems


@pytest.fixture(autouse=True)
def _results_match_output_schemas(request: pytest.FixtureRequest) -> Iterator[None]:
    """Every result any operation returns during a test conforms to its declared output
    schema (strictly: no undeclared keys). The whole suite thereby proves the schemas.
    Benchmarks are left out: validation would count against their time budgets."""
    from doc_control import registry

    if "bench" in request.node.path.parts:
        yield
        return

    problems: list[str] = []

    def check(name: str, out: Any) -> None:
        problems.extend(check_result(name, out))

    previous, registry.RESULT_CHECK = registry.RESULT_CHECK, check
    try:
        yield
    finally:
        registry.RESULT_CHECK = previous
    assert not problems, "results that break their output schema:\n" + "\n".join(
        sorted(set(problems))
    )
