"""PACKAGE T5: the live Word scenarios, every doc-control call made over MCP stdio.

    uv run --no-sync python scripts/live_word_frozen_check.py --exe PATH\\doc-control.exe
    uv run --no-sync python scripts/live_word_frozen_check.py --exe python -- -m doc_control

Runs the scenarios of ``tests/live/word_suite.py`` with a ``doc-control mcp`` process as
the doc-control side: the exe starts Word itself (``live.open``), and this script attaches
to that Word for the writer's actions, hides it, and quits it at the end. Word must not
be running beforehand: the exe would drive that one, and a document you have open with
it. Prints ``live_word_failures == N`` and exits non-zero unless N is 0.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from doc_control.live.word_com import Word  # noqa: E402
from tests.helpers import make_docx  # noqa: E402
from tests.live import word_suite  # noqa: E402


def _word_running() -> bool:
    client: Any = importlib.import_module("win32com.client")
    try:
        client.GetActiveObject("Word.Application")
    except Exception:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exe", required=True, help="the doc-control binary (or python)")
    parser.add_argument("rest", nargs="*", help="arguments before `mcp` (after --)")
    args = parser.parse_args(argv)
    reason = word_suite.unavailable()
    if reason:
        print(f"Word cannot be driven here: {reason}")
        return 2
    if _word_running():
        print("Word is running: close it first, so the exe starts a Word of its own")
        return 2
    runner = word_suite.McpExe([args.exe, *args.rest, "mcp"])
    word = Word()  # attaches to the running Word on first use: the exe's
    owned = False  # quit Word at the end only once it is known to be the exe's
    try:
        with tempfile.TemporaryDirectory(prefix="doc-control-word-") as folder:
            home = Path(folder)
            first = {"doc": str(make_docx(home / "start.docx", "Start."))}
            runner.run("live.open", first)  # the exe starts Word
            app = word._application()
            if app.Documents.Count != 1:  # a Word the owner opened meanwhile: hands off
                raise SystemExit("Word was opened meanwhile: not driving it")
            owned = True
            app.Visible = False  # the scenarios expect a Word with no window to front
            app.DisplayAlerts = 0  # wdAlertsNone
            print(f"Word {app.Version} build {app.Build}, started by {args.exe}")
            runner.run("live.close", {**first, "discard": True})
            with word_suite.through(runner):
                failures = word_suite.run_all(word, home)
            with contextlib.suppress(Exception):
                for doc in list(app.Documents):
                    doc.Close(0)
    finally:
        runner.close()
        if owned:
            with contextlib.suppress(Exception):
                app = word._application()
                app.NormalTemplate.Saved = True
                app.Quit(0)
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"live_word_failures == {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
