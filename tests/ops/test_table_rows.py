"""Tracked `table.row.insert` and `table.row.delete` (STORIES T10)."""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from tests.corpus.generator import generate
from tests.corpus.validator import REQUIRE_ENV, soffice_path
from tests.helpers import CONTENT_TYPES, RELS, soffice_dir

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DRIVER = Path(__file__).with_name("lo_table_rows.py")


@functools.cache
def _uno_python() -> str | None:
    """A Python that can import ``uno`` (LibreOffice's bridge is not on PyPI)."""
    program = Path(soffice_path() or "").resolve().parent
    for candidate in (
        sys.executable,
        shutil.which("python3"),
        "/usr/bin/python3",
        str(program / "python"),
    ):
        if candidate is None or not Path(candidate).exists():
            continue
        found = subprocess.run(
            [candidate, "-c", "import uno"], capture_output=True, timeout=60, check=False
        )
        if found.returncode == 0:
            return candidate
    return None


def _require_libreoffice() -> str:
    """The Python that drives LibreOffice. A missing LibreOffice or uno skips the test,
    unless ``DOC_CONTROL_REQUIRE_SOFFICE`` is set: then it fails, never passes by skipping."""
    reason = None
    python = _uno_python() if soffice_path() is not None else None
    if soffice_path() is None:
        reason = "LibreOffice (soffice) is not installed"
    elif python is None:
        reason = "no Python can `import uno` (install python3-uno or LibreOffice's Python)"
    if reason is not None:
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but {reason}")
        pytest.skip(reason)
    assert python is not None
    return python


def _run(name: str, request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = api.run(name, request).result
    return result


def _batch(doc: Path, ops: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return _run("doc.batch", {"doc": str(doc), "ops": ops, **extra})


def _texts(doc: Path, table: int) -> list[list[str]]:
    rows = _run("table.get", {"doc": str(doc), "table": table})["rows"]
    by_address = {r["at"]: r["text"] for r in _run("para.list", {"doc": str(doc)})["paragraphs"]}
    return [[" ".join(by_address[a] for a in cell["paragraphs"]) for cell in row] for row in rows]


def _libreoffice(python: str, doc: Path, work: Path) -> dict[str, Any]:
    done = subprocess.run(
        [python, str(DRIVER), str(doc), str(work / "profile")],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env={**os.environ, "SOFFICE": soffice_path() or "soffice"},
    )
    assert done.returncode == 0, done.stderr[-2000:]
    out: dict[str, Any] = json.loads(done.stdout)
    return out


def test_table_rows_tracked_round_trip(tmp_path: Path) -> None:
    python = _require_libreoffice()
    failures: list[str] = []
    with soffice_dir(tmp_path) as work:
        doc = work / "rows.docx"
        doc.write_bytes(generate("tables", 0))
        original = doc.read_bytes()
        before = _texts(doc, 1)
        assert len(before) == 3
        new_row = [f"New {c}" for c in range(len(before[0]))]

        result = _batch(
            doc,
            [
                {"op": "table.row.insert", "table": 1, "after_row": 1, "cells": new_row},
                {"op": "table.row.delete", "table": 1, "row": 2},
            ],
        )
        if result["applied"] != 2:
            failures.append(f"applied {result['applied']} operations")
        types = {r["type"] for r in _run("revision.list", {"doc": str(doc)})["revisions"]}
        if not {"row_insert", "row_delete", "insert", "delete"} <= types:
            failures.append(f"revision.list shows {sorted(types)}")
        if _texts(doc, 1) != [before[0], new_row, [""] * len(before[0]), before[2]]:
            failures.append(f"table.get after the edit: {_texts(doc, 1)}")

        seen = _libreoffice(python, doc, work)
        rows = lambda step: seen[step]["tables"][0]  # noqa: E731
        kinds = {r["type"] for r in seen["loaded"]["redlines"]}
        if not {"Insert", "Delete"} <= kinds:
            failures.append(f"LibreOffice lists tracked changes of kinds {sorted(kinds)}")
        if rows("loaded") != [before[0], new_row, before[1], before[2]]:
            failures.append(f"LibreOffice loads {rows('loaded')}")
        # accept: the inserted row stays, the deleted row is gone
        if rows("accepted") != [before[0], new_row, before[2]] or seen["accepted"]["redlines"]:
            failures.append(f"accept all gave {seen['accepted']}")
        # reject: the inserted row is gone, the deleted row is back
        if rows("rejected") != before or seen["rejected"]["redlines"]:
            failures.append(f"reject all gave {seen['rejected']}")
        # undo of either brings the tracked rows back
        for step in ("accept_undone", "reject_undone"):
            if seen[step] != seen["loaded"]:
                failures.append(f"LibreOffice undo ({step}) gave {seen[step]}")
        # the other tables are untouched
        if seen["loaded"]["tables"][1:] != seen["rejected"]["tables"][1:]:
            failures.append("another table changed")

        # our own undo gives the file back byte for byte, and cannot be done twice
        _run("history.undo", {"doc": str(doc)})
        if doc.read_bytes() != original:
            failures.append("undo did not restore the file")
        with pytest.raises(DocControlError) as again:
            _run("history.undo", {"doc": str(doc)})
        if again.value.code is not ErrorCode.HISTORY_NOT_FOUND:
            failures.append(f"a second undo gave {again.value.code}")
    print(f"table_row_failures == {len(failures)}")
    assert failures == [], "\n".join(failures)


# -- no LibreOffice needed ----------------------------------------------------------------


def _tc(text: str, span: int = 1, merge: str | None = None) -> str:
    props = '<w:tcW w:w="1000" w:type="dxa"/>'
    props += f'<w:gridSpan w:val="{span}"/>' if span > 1 else ""
    props += "<w:vMerge/>" if merge == "continue" else ""
    props += '<w:vMerge w:val="restart"/>' if merge == "restart" else ""
    run = f"<w:r><w:rPr><w:b/></w:rPr><w:t>{text}</w:t></w:r>" if text else ""
    ppr = "<w:pPr><w:jc w:val='center'/></w:pPr>"
    return f"<w:tc><w:tcPr>{props}</w:tcPr><w:p>{ppr}{run}</w:p></w:tc>"


def _docx(path: Path, rows: list[str], before: str = "") -> Path:
    grid = '<w:gridCol w:w="1000"/>' * 3
    body = (
        f"{before}<w:tbl><w:tblPr/><w:tblGrid>{grid}</w:tblGrid>{''.join(rows)}</w:tbl>"
        "<w:p><w:r><w:t>after</w:t></w:r></w:p>"
    )
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", xml)
    return path


def _plain(*cells: str) -> str:
    return "<w:tr>" + "".join(_tc(c) for c in cells) + "</w:tr>"


def _merged(path: Path) -> Path:
    rows = [
        "<w:tr>" + _tc("a") + _tc("B", merge="restart") + _tc("c") + "</w:tr>",
        "<w:tr>" + _tc("d") + _tc("", merge="continue") + _tc("f") + "</w:tr>",
        _plain("g", "h", "i"),
    ]
    return _docx(path, rows)


def test_rows_over_a_vertical_merge_are_refused_and_change_nothing(tmp_path: Path) -> None:
    doc = _merged(tmp_path / "m.docx")
    before = doc.read_bytes()
    refused = [
        {"op": "table.row.delete", "table": 1, "row": 1},  # starts a merge
        {"op": "table.row.delete", "table": 1, "row": 2},  # continues it
        {"op": "table.row.insert", "table": 1, "after_row": 1, "cells": ["x", "y", "z"]},
    ]
    for op in refused:
        with pytest.raises(DocControlError) as err:
            _batch(doc, [op])
        assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT, op
        assert "vertical merge" in err.value.message
        assert doc.read_bytes() == before
    # a row the merge does not touch is fine, and so is a place it does not span
    _batch(doc, [{"op": "table.row.delete", "table": 1, "row": 3}])
    _run("history.undo", {"doc": str(doc)})
    _batch(doc, [{"op": "table.row.insert", "table": 1, "after_row": 2, "cells": ["x", "y", "z"]}])
    assert _texts(doc, 1)[2] == ["x", "y", "z"]


def test_standalone_ops_dry_run_and_row_properties(tmp_path: Path) -> None:
    ex = "<w:tblPrEx><w:tblW w:w='0' w:type='auto'/></w:tblPrEx>"
    header = (
        f"<w:tr>{ex}<w:trPr><w:cantSplit/><w:tblHeader/><w:trHeight w:val='400'/></w:trPr>"
        f"{_tc('h1')}{_tc('h2')}{_tc('h3')}</w:tr>"
    )
    doc = _docx(tmp_path / "p.docx", [header, _plain("a", "b", "c")])
    before = doc.read_bytes()
    dry = _run(
        "table.row.insert",
        {"doc": str(doc), "table": 1, "after_row": 0, "cells": ["x", "", "z"], "dry_run": True},
    )
    assert dry["dry_run"] is True and doc.read_bytes() == before
    _run(
        "table.row.insert",
        {"doc": str(doc), "table": 1, "after_row": 0, "cells": ["x", "", "z"], "author": "Ana"},
    )
    xml = zipfile.ZipFile(doc).read("word/document.xml").decode()
    first = xml[xml.index("<w:tr>") : xml.index("</w:tr>")]
    assert "<w:ins " in first.split("</w:trPr>")[0] and 'w:author="Ana"' in first
    assert "<w:cantSplit/>" in first and "<w:trHeight" in first  # row properties are kept
    assert "tblHeader" not in first  # a new row is not another header row
    assert "<w:tblPrEx>" in first
    assert '<w:jc w:val="center"/>' in first or "<w:jc w:val='center'/>" in first
    assert _texts(doc, 1)[0] == ["x", "", "z"]
    _run("table.row.delete", {"doc": str(doc), "table": 1, "row": 3})
    assert _texts(doc, 1)[2] == ["", "", ""]  # deleted text is not visible text
    rows = {r["type"] for r in _run("revision.list", {"doc": str(doc)})["revisions"]}
    assert {"row_insert", "row_delete"} <= rows


def test_row_op_errors_leave_the_file_alone(tmp_path: Path) -> None:
    doc = _docx(tmp_path / "e.docx", [_plain("a", "b", "c"), _plain("d", "e", "f")])
    before = doc.read_bytes()
    cases: list[tuple[list[dict[str, Any]], ErrorCode]] = [
        ([{"op": "table.row.delete", "table": 2, "row": 1}], ErrorCode.ANCHOR_NOT_FOUND),
        ([{"op": "table.row.delete", "table": 1, "row": 3}], ErrorCode.ANCHOR_NOT_FOUND),
        (
            [{"op": "table.row.insert", "table": 1, "after_row": 3, "cells": ["x", "y", "z"]}],
            ErrorCode.ANCHOR_NOT_FOUND,
        ),
        (
            [{"op": "table.row.insert", "table": 1, "after_row": 1, "cells": ["x"]}],
            ErrorCode.SCHEMA_INVALID,
        ),
        (
            [{"op": "table.row.delete", "table": 1, "row": 1}] * 2,
            ErrorCode.BATCH_CONFLICT,
        ),
        (
            [
                {"op": "table.row.delete", "table": 1, "row": 1},
                {"op": "para.delete", "at": "p1#00000000"},
            ],
            ErrorCode.BATCH_CONFLICT,
        ),
    ]
    for ops, code in cases:
        with pytest.raises(DocControlError) as err:
            _batch(doc, ops)
        assert err.value.code is code, (ops, err.value.message)
        assert doc.read_bytes() == before


def test_two_inserts_at_one_place_keep_their_order_and_undo_together(tmp_path: Path) -> None:
    doc = _docx(tmp_path / "o.docx", [_plain("a", "b", "c")])
    before = doc.read_bytes()
    _batch(
        doc,
        [
            {"op": "table.row.insert", "table": 1, "after_row": 1, "cells": ["1", "1", "1"]},
            {"op": "table.row.insert", "table": 1, "after_row": 1, "cells": ["2", "2", "2"]},
            {"op": "table.row.insert", "table": 1, "after_row": 0, "cells": ["0", "0", "0"]},
        ],
    )
    assert [r[0] for r in _texts(doc, 1)] == ["0", "a", "1", "2"]
    _run("history.undo", {"doc": str(doc)})
    assert doc.read_bytes() == before


def test_undo_is_refused_once_a_tracked_row_was_edited_since(tmp_path: Path) -> None:
    doc = _docx(tmp_path / "u.docx", [_plain("a", "b", "c"), _plain("d", "e", "f")])
    _batch(doc, [{"op": "table.row.insert", "table": 1, "after_row": 1, "cells": ["x", "y", "z"]}])
    first = _run("history.list", {"doc": str(doc)})["entries"][0]["op_id"]
    _batch(doc, [{"op": "table.row.delete", "table": 1, "row": 2}])  # deletes the new row
    after = doc.read_bytes()
    with pytest.raises(DocControlError) as err:
        _run("history.undo", {"doc": str(doc), "op_id": first})
    assert err.value.code is ErrorCode.BATCH_CONFLICT
    assert doc.read_bytes() == after
    _run("history.undo", {"doc": str(doc)})  # the latest one can be
    _run("history.undo", {"doc": str(doc)})
    assert _texts(doc, 1) == [["a", "b", "c"], ["d", "e", "f"]]


def test_a_missing_uno_fails_when_libreoffice_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate never passes by skipping where the environment promises LibreOffice."""
    monkeypatch.setattr(sys.modules[__name__], "_uno_python", lambda: None)
    monkeypatch.setattr(sys.modules[__name__], "soffice_path", lambda: "/usr/bin/soffice")
    monkeypatch.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match=REQUIRE_ENV):
        _require_libreoffice()
    monkeypatch.delenv(REQUIRE_ENV)
    with pytest.raises(pytest.skip.Exception):
        _require_libreoffice()
