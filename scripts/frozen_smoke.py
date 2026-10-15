"""Smoke-test a doc-control binary from outside: CLI, file engine and MCP (PACKAGE T3).

Usage::

    python scripts/frozen_smoke.py --exe build-out/doc-control/doc-control.exe \\
        --strip-python-path --frozen
    python scripts/frozen_smoke.py --exe python -- -m doc_control   # the pip install

Checks, one line each (``ok NAME`` or ``FAIL NAME: why``):

- ``server.capabilities`` answers one ok envelope (with ``--frozen``, its ``build`` must be
  ``{"frozen": true, "freezer": "pyinstaller"}``);
- a plain and a regex ``text.find`` on a corpus fixture (``tests/corpus/generator.py``); the
  regex runs in a spawned worker, so a frozen exe without ``freeze_support`` fails it;
- MCP over stdio: ``initialize``, ``tools/list``, then one ``tools/call`` per operation
  family (the ``doc_control.ops`` module the registry imports by name), each on a temp
  copy of the fixture and read-only or ``dry_run``.

``--strip-python-path`` runs the binary with every directory that holds a Python
interpreter or the ``py`` launcher removed from ``PATH``, and ``PYTHONHOME`` and
``PYTHONPATH`` unset. The last line is ``frozen_smoke_failures == N``; exit 0 iff N == 0.

Standard library only. The binary's output is decoded as UTF-8, never the locale codec.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tests" / "corpus" / "generator.py"
FROZEN = {"frozen": True, "freezer": "pyinstaller"}
TIMEOUT = 180.0
PYTHON_NAME = re.compile(r"^(python(\d+(\.\d+)*)?|pythonw|py|pyw)(\.exe)?$", re.IGNORECASE)
# Survives the UTF-8 round trip through the binary's stdin and stdout only if both are UTF-8.
UNICODE_TEXT = "café  ñandú — “smoke”"
UNICODE_NORMALIZED = "café ñandú — “smoke”"

# Family (the `doc_control.ops` module) -> one tool call. Strings are templates: {doc} is the
# fixture copy, {tmp} the scratch folder, {at} a body paragraph address read over MCP.
FAMILY_CALLS: dict[str, tuple[str, dict[str, Any]]] = {
    "anchor": ("anchor_create", {"doc": "{doc}", "at": "{at}"}),
    "batch": (
        "doc_batch",
        {
            "doc": "{doc}",
            "ops": [{"op": "para.insert", "after": "{at}", "text": "smoke"}],
            "dry_run": True,
        },
    ),
    "bulk": ("para_list", {"doc": "{doc}", "limit": 3}),
    "compare": ("doc_compare", {"old": "{doc}", "new": "{doc}", "output": "{tmp}/compare.docx"}),
    "crossref": (
        "caption_add",
        {"doc": "{doc}", "at": "{at}", "label": "Figure", "text": "smoke", "dry_run": True},
    ),
    "diff": ("doc_diff", {"old": "{doc}", "new": "{doc}"}),
    "dotx": ("styleset_export_dotx", {"doc": "{doc}", "out": "{tmp}/styles.dotx"}),
    "fields": ("field_list", {"doc": "{doc}"}),
    "fieldwrite": ("field_update", {"doc": "{doc}", "set_update_on_open": True, "dry_run": True}),
    "find": ("text_find", {"doc": "{doc}", "query": "[0-9]+", "regex": True, "limit": 3}),
    "history": ("history_list", {"doc": "{doc}"}),
    "layout": ("layout_get", {"doc": "{doc}"}),
    "live": ("live_status", {}),
    "locate": ("para_locate", {"doc": "{doc}", "limit": 2}),
    "meta": ("server_capabilities", {}),
    "notes": (
        "note_add",
        {"doc": "{doc}", "at": "{at}", "kind": "footnote", "text": "smoke", "dry_run": True},
    ),
    "page": ("page_estimate", {"doc": "{doc}", "limit": 2}),
    "read": ("doc_outline", {"doc": "{doc}"}),
    "recipe": ("recipe_list", {}),
    "rename": ("term_rename", {"doc": "{doc}", "from": "the", "to": "teh", "dry_run": True}),
    "sections": ("section_list", {"doc": "{doc}"}),
    "segment": ("doc_segment", {"doc": "{doc}", "limit": 2}),
    "styleset": ("styleset_export", {"doc": "{doc}"}),
    "stylewrite": ("style_create", {"doc": "{doc}", "name": "Smoke", "dry_run": True}),
    "tablerows": ("table_row_delete", {"doc": "{doc}", "table": 1, "row": 2, "dry_run": True}),
    "tables": ("table_get", {"doc": "{doc}", "table": 1}),
    "text": ("text_normalize", {"text": UNICODE_TEXT}),
}


class CheckFailed(Exception):
    """One check's failure; the message is printed after ``FAIL name:``."""


def holds_python(directory: str) -> bool:
    """Whether a directory holds a Python interpreter or the ``py`` launcher."""
    try:
        names = [entry.name for entry in Path(directory).iterdir()]
    except OSError:
        return False
    return any(PYTHON_NAME.match(name) for name in names)


def child_env(environ: dict[str, str], strip_python_path: bool) -> dict[str, str]:
    """The binary's environment; with ``strip_python_path``, no Python to fall back on."""
    env = dict(environ)
    if strip_python_path:
        for key in [k for k in env if k.upper() in ("PYTHONHOME", "PYTHONPATH")]:
            del env[key]
        path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
        entries = env.get(path_key, "").split(os.pathsep)
        env[path_key] = os.pathsep.join(e for e in entries if e and not holds_python(e))
    return env


def _fill(value: Any, names: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**names)
    if isinstance(value, list):
        return [_fill(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, names) for k, v in value.items()}
    return value


def _fixture(folder: Path) -> Path:
    """A temp copy of the kitchen-sink corpus document (headings, tables, fields, notes)."""
    spec = importlib.util.spec_from_file_location("_smoke_corpus_generator", GENERATOR)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the corpus generator at {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it executes
    spec.loader.exec_module(module)
    doc = folder / "kitchen_sink.docx"
    doc.write_bytes(module.generate("kitchen_sink", 1))
    return doc


class Smoke:
    def __init__(self, command: list[str], env: dict[str, str], cwd: Path) -> None:
        self.command, self.env, self.cwd = command, env, cwd
        self.failures = 0

    def check(self, name: str, run: Callable[[], str | None]) -> Any:
        try:
            note = run()
        except CheckFailed as err:
            self.failures += 1
            print(f"FAIL {name}: {err}", flush=True)
            return None
        print(f"ok {name}" + (f": {note}" if note else ""), flush=True)
        return note

    def cli(self, *args: str, stdin: str | None = None) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                [*self.command, *args],
                input=None if stdin is None else stdin.encode("utf-8"),
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
                env=self.env,
                cwd=self.cwd,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise CheckFailed(f"could not run: {err}") from err
        out = proc.stdout.decode("utf-8", errors="replace")
        lines = [line for line in out.splitlines() if line.strip()]
        if proc.returncode != 0 or len(lines) != 1:
            err_text = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            raise CheckFailed(f"exit {proc.returncode}, stdout {out[-2000:]!r}, stderr {err_text}")
        return _ok(json.loads(lines[0]))


def _ok(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise CheckFailed(f"not an ok envelope: {json.dumps(envelope, ensure_ascii=False)[:2000]}")
    return envelope


class McpSession:
    """``<exe> mcp`` on stdio: one JSON-RPC request at a time, a reader thread, a timeout."""

    def __init__(self, command: list[str], env: dict[str, str], cwd: Path, log: Path) -> None:
        self._log = log.open("wb")
        self.proc = subprocess.Popen(
            [*command, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            env=env,
            cwd=cwd,
        )
        self._lines: queue.Queue[bytes] = queue.Queue()
        assert self.proc.stdout is not None
        threading.Thread(target=self._read, args=(self.proc.stdout,), daemon=True).start()
        self._next_id = 0

    def _read(self, stream: IO[bytes]) -> None:
        for line in stream:
            self._lines.put(line)
        self._lines.put(b"")

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except OSError as err:
            raise CheckFailed(f"the server is gone: {err}") from err
        while True:
            try:
                line = self._lines.get(timeout=TIMEOUT)
            except queue.Empty as err:
                raise CheckFailed(f"no reply in {TIMEOUT:g} s") from err
            if not line:
                self._lines.put(b"")
                raise CheckFailed(f"the server closed stdout (exit {self.proc.poll()})")
            if not line.strip():
                continue
            try:
                reply = json.loads(line.decode("utf-8"))
            except ValueError as err:
                raise CheckFailed(f"not JSON on stdout: {line[:500]!r}") from err
            if isinstance(reply, dict) and reply.get("id") == self._next_id:
                break
        if "error" in reply:
            raise CheckFailed(f"JSON-RPC error {reply['error']}")
        return reply.get("result")

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        try:
            envelope = json.loads(result["content"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError) as err:
            raise CheckFailed(f"malformed tools/call result: {result!r:.2000}") from err
        if result.get("isError"):
            raise CheckFailed(f"isError: {json.dumps(envelope, ensure_ascii=False)[:2000]}")
        return _ok(envelope)

    def close(self) -> None:
        if self.proc.stdin is not None:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._log.close()


def run(command: list[str], strip_python_path: bool, frozen: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="frozen-smoke-") as scratch:
        tmp = Path(scratch)
        doc = _fixture(tmp)
        smoke = Smoke(command, child_env(dict(os.environ), strip_python_path), tmp)
        if strip_python_path:
            smoke.check("PATH without Python", lambda: _no_python(smoke.env))
        smoke.check("capabilities", lambda: _capabilities(smoke, frozen))
        smoke.check("find", lambda: _find(smoke, doc, "Synthetic", regex=False))
        smoke.check("find regex", lambda: _find(smoke, doc, "[0-9]+", regex=True))
        _mcp(smoke, tmp, doc)
    print(f"frozen_smoke_failures == {smoke.failures}", flush=True)
    return 0 if smoke.failures == 0 else 1


def _no_python(env: dict[str, str]) -> str:
    path = next((v for k, v in env.items() if k.upper() == "PATH"), "")
    left = [e for e in path.split(os.pathsep) if e and holds_python(e)]
    if left or any(k.upper() in ("PYTHONHOME", "PYTHONPATH") for k in env):
        raise CheckFailed(f"Python is still reachable: {left}")
    return f"{len([e for e in path.split(os.pathsep) if e])} PATH entries"


def _capabilities(smoke: Smoke, frozen: bool) -> str:
    result = smoke.cli("server.capabilities")["result"]
    build = result.get("build")
    if frozen and build != FROZEN:
        raise CheckFailed(f"build is {build}, expected {FROZEN}")
    return f"{len(result.get('operations', []))} operations, build {build}"


def _find(smoke: Smoke, doc: Path, query: str, regex: bool) -> str:
    request = json.dumps({"doc": str(doc), "query": query, "regex": regex})
    matches = smoke.cli("text.find", "--json", "-", stdin=request)["result"]["matches"]
    want = (lambda t: t.isdigit()) if regex else (lambda t: t == query)
    if not matches or not all(want(m["text"]) for m in matches):
        raise CheckFailed(f"unexpected matches {[m['text'] for m in matches][:10]}")
    return f"{len(matches)} matches"


def _mcp(smoke: Smoke, tmp: Path, doc: Path) -> None:
    try:
        session = McpSession(smoke.command, smoke.env, tmp, tmp / "mcp-stderr.log")
    except OSError as err:
        session = None
        reason = str(err)
    names = {"doc": str(doc), "tmp": str(tmp), "at": ""}
    try:

        def need_session() -> McpSession:
            if session is None:
                raise CheckFailed(f"could not start `mcp`: {reason}")
            return session

        def initialize() -> str:
            result = need_session().request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "frozen-smoke", "version": "1"},
                },
            )
            info = (result or {}).get("serverInfo", {})
            if info.get("name") != "doc-control":
                raise CheckFailed(f"unexpected initialize result {result!r:.2000}")
            return f"{result.get('protocolVersion')}, version {info.get('version')}"

        def tools_list() -> str:
            result = need_session().request("tools/list", {})
            listed = {t.get("name") for t in (result or {}).get("tools", [])}
            missing = sorted({tool for tool, _ in FAMILY_CALLS.values()} - listed)
            if missing:
                raise CheckFailed(f"tools missing from tools/list: {missing}")
            return f"{len(listed)} tools"

        smoke.check("mcp initialize", initialize)
        smoke.check("mcp tools/list", tools_list)
        for family, (tool, template) in sorted(FAMILY_CALLS.items()):
            smoke.check(
                f"mcp tools/call {tool} ({family})",
                lambda f=family, t=tool, a=template: _family(need_session(), f, t, a, names),
            )
    finally:
        if session is not None:
            session.close()


def _family(
    session: McpSession, family: str, tool: str, template: dict[str, Any], names: dict[str, str]
) -> str | None:
    if "{at}" in json.dumps(template) and not names["at"]:
        paragraphs = session.call("para_list", {"doc": names["doc"], "limit": 20})
        body = [p for p in paragraphs["result"]["paragraphs"] if not p.get("heading_level")]
        if not body:
            raise CheckFailed("the fixture has no body paragraph to address")
        names["at"] = body[0]["at"]
    result = session.call(tool, _fill(template, names))["result"]
    if family == "text":
        texts = [p["text"] for p in result["paragraphs"]]
        if texts != [UNICODE_NORMALIZED]:
            raise CheckFailed(f"UTF-8 round trip: expected {[UNICODE_NORMALIZED]}, got {texts}")
    if family == "find" and not result["matches"]:
        raise CheckFailed("regex find over MCP found nothing")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--exe", required=True, help="the doc-control binary (or an interpreter)")
    parser.add_argument(
        "--strip-python-path",
        action="store_true",
        help="drop every Python directory from PATH; unset PYTHONHOME and PYTHONPATH",
    )
    parser.add_argument(
        "--frozen", action="store_true", help=f"require capabilities.build == {FROZEN}"
    )
    parser.add_argument("exe_args", nargs="*", help="after `--`: arguments put before each call's")
    args = parser.parse_args(argv)
    # A FAIL line quotes the binary's (UTF-8) output; a cp1252 console must not crash on it.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    exe = str(Path(args.exe).absolute()) if Path(args.exe).exists() else args.exe
    return run([exe, *args.exe_args], args.strip_python_path, args.frozen)


if __name__ == "__main__":
    sys.exit(main())
