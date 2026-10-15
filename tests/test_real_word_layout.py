"""The LAYOUT T9 script (``scripts/real_word_layout.py``): build, then verify a simulated
Word run.

doc-control stands in for Word here (accept all, reject all), so the script's expectations
are proved consistent before the owner runs it in real Word. LibreOffice is switched off,
so the TOC document (which needs it) is left out.
"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.ooxml.soffice import NO_SOFFICE_ENV

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "real_word_layout.py"


def test_build_then_verify_a_simulated_word_run(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(NO_SOFFICE_ENV, "1")
    module = runpy.run_path(str(SCRIPT))
    out = tmp_path / "layout"
    module["build"](out)
    built = sorted(p.stem for p in out.glob("*.docx"))
    assert built == sorted(module["names"](False))
    assert module["verify"](out) == 1  # nothing saved from "Word" yet
    store = tmp_path / "store"
    for name in built:
        target = out / f"{name}-accepted.docx"
        target.write_bytes((out / f"{name}.docx").read_bytes())
        with api.open(target, store=store) as doc:
            doc.run("doc.batch", {"ops": [{"op": "revision.accept", "all": True}]})
    # Word's Reject All drops the inserted section break with its paragraph mark;
    # doc-control's own reject keeps a break it has no history for, so the original stands
    # in for Word's result.
    rejected = out / f"{module['REJECTED']}-rejected.docx"
    module["_base"](rejected, store)
    assert module["verify"](out) == 0
    assert "real_word_layout_mismatches == 0" in capsys.readouterr().out
