"""The T30 script (``scripts/real_word_check.py``): build, then verify a simulated Word run.

doc-control itself stands in for Word here (accept all, reject all, reply, resolve), so the
script's expectations are proved consistent before a person runs it in real Word.
"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

from doc_control import api

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "real_word_check.py"


def test_build_then_verify_a_simulated_word_run(tmp_path: Path, capsys: Any) -> None:
    module = runpy.run_path(str(SCRIPT))
    out = tmp_path / "t30"
    module["build"](out)
    assert len(list(out.glob("*.docx"))) == len(module["KINDS"]) + 2  # none overwritten
    store = tmp_path / "store"
    assert module["verify"](out) == 1  # nothing saved from "Word" yet
    for name, op in (("edits-accepted", "revision.accept"), ("edits-rejected", "revision.reject")):
        target = out / f"{name}.docx"
        target.write_bytes((out / "edits.docx").read_bytes())
        with api.open(target, store=store) as doc:
            doc.run("batch", {"ops": [{"op": op, "all": True}]})
    replied = out / "commented-word.docx"
    replied.write_bytes((out / "commented.docx").read_bytes())
    with api.open(replied, store=store) as doc:
        ops = [
            {"op": "comment.reply", "comment": "0", "text": "Done."},
            {"op": "comment.resolve", "comment": "1"},
        ]
        doc.run("batch", {"ops": ops})
    assert module["verify"](out) == 0
    assert "real_word_roundtrip_mismatches == 0" in capsys.readouterr().out
