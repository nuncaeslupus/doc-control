"""Compare a doc-control binary's results with in-process doc_control (PACKAGE T4).

Usage::

    python scripts/frozen_parity.py --exe build-out/doc-control/doc-control.exe
    python scripts/frozen_parity.py --exe python -- -m doc_control   # the pip install

Runs ``server.capabilities`` and, for every corpus kind (``tests/corpus/generator.py``, ``KINDS``
plus ``STORY_KINDS``), a few ``text.find`` queries (plain and regex), once through the binary
(``<exe> <op> --json -``) and once through ``doc_control.cli.execute`` in this process,
on the same fixture file. The two envelopes must be equal; ``capabilities.build`` is
ignored (the binary says ``frozen: true``, the pip install ``frozen: false``).

Each differing case prints one ``DIFF`` line naming the first differing path. The last line
is ``frozen_vs_python_result_diffs == N``; exit 0 iff N == 0.

Standard library only, plus ``doc_control`` imported in-process. The binary's output is
decoded as UTF-8, never the locale codec.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tests" / "corpus" / "generator.py"
TIMEOUT = 180.0
SEED = 1
# (query, regex): English words, a Spanish one (the novel) and a regex, which runs in a
# spawned worker.
QUERIES: tuple[tuple[str, bool], ...] = (("the", False), ("de", False), ("[0-9]+", True))
MISSING = "<missing>"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_parity_corpus_generator", GENERATOR)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the corpus generator at {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it executes
    spec.loader.exec_module(module)
    return module


def first_diff(want: Any, got: Any, path: str = "") -> str | None:
    """The first path where ``got`` differs from ``want``, with both values; else None."""
    if isinstance(want, dict) and isinstance(got, dict):
        for key in sorted(set(want) | set(got), key=str):
            found = first_diff(want.get(key, MISSING), got.get(key, MISSING), f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(want, list) and isinstance(got, list):
        for i, (w, g) in enumerate(zip(want, got, strict=False)):
            found = first_diff(w, g, f"{path}[{i}]")
            if found:
                return found
        if len(want) != len(got):
            return f"{path or '.'}: length {len(got)}, expected {len(want)}"
        return None
    if want != got or type(want) is not type(got):
        shown = [json.dumps(v, ensure_ascii=False)[:200] for v in (got, want)]
        return f"{path or '.'}: {shown[0]}, expected {shown[1]}"
    return None


def _drop_build(envelope: Any) -> Any:
    if isinstance(envelope, dict) and isinstance(envelope.get("result"), dict):
        envelope = {**envelope, "result": dict(envelope["result"])}
        envelope["result"].pop("build", None)
    return envelope


class Parity:
    def __init__(self, command: list[str], cwd: Path) -> None:
        from doc_control.cli import execute
        from doc_control.registry import load_operations

        self.command, self.cwd = command, cwd
        self.registry = load_operations()
        self.execute = execute
        self.diffs = 0
        self.cases = 0

    def exe(self, op: str, request: dict[str, Any]) -> Any:
        args = [op] if not request else [op, "--json", "-"]
        stdin = json.dumps(request, ensure_ascii=False).encode("utf-8") if request else None
        try:
            proc = subprocess.run(
                [*self.command, *args],
                input=stdin,
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
                cwd=self.cwd,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            return {"<could not run>": str(err)}
        out = proc.stdout.decode("utf-8", errors="replace")
        lines = [line for line in out.splitlines() if line.strip()]
        try:
            return json.loads(lines[0]) if len(lines) == 1 else {"<stdout>": out[-2000:]}
        except ValueError:
            return {"<stdout>": out[-2000:]}

    def compare(self, name: str, op: str, request: dict[str, Any]) -> None:
        self.cases += 1
        want, _ = self.execute(self.registry, op, dict(request))
        got = self.exe(op, request)
        if op == "server.capabilities":
            want, got = _drop_build(want), _drop_build(got)
        found = first_diff(json.loads(json.dumps(want)), got)
        if found:
            self.diffs += 1
            print(f"DIFF {name}: {found}", flush=True)


def run(command: list[str]) -> int:
    generator = _generator()
    kinds: tuple[str, ...] = (*generator.KINDS, *generator.STORY_KINDS)
    with tempfile.TemporaryDirectory(prefix="frozen-parity-") as scratch:
        tmp = Path(scratch)
        parity = Parity(command, tmp)
        parity.compare("capabilities", "server.capabilities", {})
        for kind in kinds:
            doc = tmp / f"{kind}.docx"
            doc.write_bytes(generator.generate(kind, SEED))
            for query, regex in QUERIES:
                request = {"doc": str(doc), "query": query, "regex": regex}
                parity.compare(f"find {kind} {query!r} regex={regex}", "text.find", request)
    print(f"{parity.cases} cases over {len(kinds)} corpus kinds", flush=True)
    print(f"frozen_vs_python_result_diffs == {parity.diffs}", flush=True)
    return 0 if parity.diffs == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--exe", required=True, help="the doc-control binary (or an interpreter)")
    parser.add_argument("exe_args", nargs="*", help="after `--`: arguments put before each call's")
    args = parser.parse_args(argv)
    # A DIFF line quotes the binary's (UTF-8) output; a cp1252 console must not crash on it.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    exe = str(Path(args.exe).absolute()) if Path(args.exe).exists() else args.exe
    return run([exe, *args.exe_args])


if __name__ == "__main__":
    # In-process regex find spawns a worker that re-imports this module as __mp_main__.
    sys.exit(main())
