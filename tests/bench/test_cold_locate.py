"""Cold single-paragraph path (T56): import, open, ``para.get``, ``locate`` and
``page.estimate`` on one paragraph, in a fresh process, as literary-critic runs them.

Gate: ``cold_locate_path_regressions == 0``: the path on the 100k-word novel, with
``structure: false``, meets its budget; operations load one module at a time (layout facts
included); and ``structure: false`` changes nothing but the structure fields.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from tests.bench.test_perf_gate import REFERENCE_LOOP_SECONDS, _reference_loop
from tests.corpus.generator import generate

COLD_PATH_SECONDS = 0.5

_CHILD = """
import json, sys, time
start = time.perf_counter()
from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
with api.open(sys.argv[1]) as doc:
    para = doc.run("para.get", {"at": sys.argv[2]})
    row = doc.run("locate", {"at": sys.argv[2], "structure": False})["paragraphs"][0]
    page = doc.run("page.estimate", {"at": sys.argv[2]})
took = time.perf_counter() - start
loaded = sorted(m for m in sys.modules if m.startswith("doc_control.ops."))
print(json.dumps({"took": took, "para": para, "row": row, "loaded": loaded}))
"""


def _child(path: Path, at: str) -> dict[str, object]:
    out = subprocess.run(
        [sys.executable, "-c", _CHILD, str(path), at], capture_output=True, text=True, check=True
    )
    result: dict[str, object] = json.loads(out.stdout)
    return result


def _address(path: Path, n: int) -> str:
    with api.open(path) as doc:
        return str(doc.paragraphs()[n]["at"])


def _regressions(path: Path, at: str) -> tuple[int, float]:
    child = _child(path, at)
    bad = 0
    para = child["para"]
    assert isinstance(para, dict)
    bad += "heading_level" not in para  # layout facts load with para.get
    loaded = child["loaded"]
    assert isinstance(loaded, list)
    bad += "doc_control.ops.compare" in loaded or "doc_control.ops.recipe" in loaded
    with api.open(path) as doc:
        whole = doc.run("locate", {"at": at})["paragraphs"][0]
    row = child["row"]
    assert isinstance(row, dict)
    structure = ("chapter", "part", "scene")
    bad += any(row[k] is not None for k in structure)
    bad += {k: v for k, v in row.items() if k not in structure} != {
        k: v for k, v in whole.items() if k not in structure
    }
    took = child["took"]
    assert isinstance(took, float)
    return bad, took


def test_single_paragraph_path_loads_little_and_matches(tmp_path: Path) -> None:
    path = tmp_path / "kitchen.docx"
    path.write_bytes(generate("kitchen_sink", 1))
    bad, _ = _regressions(path, _address(path, 3))
    assert bad == 0


@pytest.mark.bench
def test_cold_locate_path_within_budget(tmp_path: Path) -> None:
    factor = max(1.0, min(_reference_loop() for _ in range(3)) / REFERENCE_LOOP_SECONDS)
    path = tmp_path / "novel.docx"
    path.write_bytes(generate("novel", 1))
    at = _address(path, 700)
    _child(path, at)  # warm the OS file cache and bytecode, not the process
    regressions, took = _regressions(path, at)
    regressions += took > COLD_PATH_SECONDS * factor
    print(f"cold locate path {took:.3f}s (budget {COLD_PATH_SECONDS * factor:.3f}s)")
    print(f"cold_locate_path_regressions == {regressions}")
    assert regressions == 0


def test_get_operation_always_waits_for_its_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operation registers before its module finishes (read.py's layout hooks come last),
    so a thread that finds it registered must still go through import_module, which waits
    while another thread is importing that module."""
    from doc_control import registry

    calls: list[str] = []
    real = registry.importlib.import_module

    def recording(name: str) -> object:
        calls.append(name)
        return real(name)

    monkeypatch.setattr(registry.importlib, "import_module", recording)
    registry.load_operations()
    assert registry.get_operation("para.get") is not None
    assert calls[-1] == "doc_control.ops.read"


def test_structure_false_still_checks_the_profile(tmp_path: Path) -> None:
    path = tmp_path / "kitchen.docx"
    path.write_bytes(generate("kitchen_sink", 1))
    at = _address(path, 3)
    with api.open(path) as doc, pytest.raises(DocControlError) as err:
        doc.run("locate", {"at": at, "structure": False, "profile": {"chapter_level": 42}})
    assert err.value.code is ErrorCode.SCHEMA_INVALID
