"""Plain writes in the live engines (D-21): ``batch {engine: live, tracked: false}``.

The gate ``live_plain_write_failures == 0``: a plain write leaves no revision of its own,
the app's change tracking is put back as the writer had it, and a paragraph holding
another unresolved tracked change is refused before anything is written. Word runs in a
hidden instance of its own (``DOC_CONTROL_REQUIRE_LIVE_WORD=1`` turns its skip into a
failure); LibreOffice is the headless one of ``conftest.py``. The dispatch and refusal
paths without an app: ``tests/ops/test_plain_live_dispatch.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.lo_uno import LibreOffice
from doc_control.model.address import fingerprint
from tests.helpers import make_docx
from tests.live import word_suite

AUTHOR = "Rev Iewer"
TEXTS = ["Alpha line one.", "Beta line two.", "Gamma line three.", "Delta line four."]


def _run(op: str, request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = api.run(op, request).result
    return result


def at(n: int, text: str, span: str = "") -> str:
    return f"p{n}#{fingerprint(text)}{span}"


def test_word_plain_replace_leaves_no_revision(tmp_path: Path) -> None:
    reason = word_suite.unavailable()
    if reason:
        if os.environ.get("DOC_CONTROL_REQUIRE_LIVE_WORD"):
            pytest.fail(reason)
        pytest.skip(reason)
    with word_suite.hidden_word() as word:
        assert word_suite.plain(word, tmp_path) == []  # live_plain_write_failures


def _redlines(doc: Any) -> int:
    found, items = 0, doc.Redlines.createEnumeration()
    while items.hasMoreElements():
        items.nextElement()
        found += 1
    return found


def test_lo_plain_replace_leaves_no_redline(live: LibreOffice, lo_home: Path) -> None:
    doc = make_docx(lo_home / "plain.docx", *TEXTS)
    base = {"doc": str(doc)}
    batch = {**base, "engine": "live", "author": AUTHOR}
    _run("live.open", base)
    failures: list[object] = []  # live_plain_write_failures
    try:
        lo = live._doc(doc)
        for prior, ops in (
            (True, [{"op": "text.replace", "at": at(1, TEXTS[0], ":0-5"), "text": "ALPHA"}]),
            (
                False,
                [
                    {"op": "text.replace", "at": at(2, TEXTS[1], ":0-4"), "text": "BETA"},
                    {"op": "para.insert", "after": at(2, TEXTS[1]), "text": "Inserted plain."},
                ],
            ),
        ):
            lo.RecordChanges = prior
            done = _run("doc.batch", {**batch, "ops": ops, "tracked": False})
            if done.get("tracked") is not False:
                failures.append(("reply", prior, done))
            if _redlines(lo) != 0:
                failures.append(("redlines", prior, _redlines(lo)))
            if bool(lo.RecordChanges) is not prior:
                failures.append(("RecordChanges not put back", prior))
        lo.RecordChanges = False

        gamma = {"op": "text.replace", "at": at(4, TEXTS[2], ":0-5"), "text": "GAMMA"}
        _run("doc.batch", {**batch, "ops": [gamma]})
        marked = _redlines(lo)
        assert marked > 0
        revised = at(4, "GAMMA line three.")
        for ops in (
            [{"op": "text.replace", "at": f"{revised}:6-10", "text": "LINE"}],
            [{"op": "para.insert", "after": revised, "text": "Not here."}],
        ):
            with pytest.raises(DocControlError) as err:
                _run("doc.batch", {**batch, "ops": ops, "tracked": False})
            if err.value.code is not ErrorCode.UNSUPPORTED_CONTENT:
                failures.append(("refusal", ops[0]["op"], err.value.code))
            if _redlines(lo) != marked:
                failures.append(("redlines after a refusal", _redlines(lo)))
        delta = {"op": "text.replace", "at": at(5, TEXTS[3], ":0-5"), "text": "DELTA"}
        _run("doc.batch", {**batch, "ops": [delta], "tracked": False})
        if _redlines(lo) != marked:
            failures.append(("a plain write after a tracked one", _redlines(lo)))
        _run("live.save", base)
    finally:
        _run("live.close", {**base, "discard": True})

    texts = [p["text"] for p in _run("para.list", base)["paragraphs"]]
    want = ["ALPHA line one.", "BETA line two.", "Inserted plain.", "GAMMA line three."]
    if texts != [*want, "DELTA line four."]:
        failures.append(("text", texts))
    entries = _run("history.list", base)["entries"]
    if len([e for e in entries if e.get("tracked") is False]) != 4:
        failures.append(("history", entries))
    assert failures == []
