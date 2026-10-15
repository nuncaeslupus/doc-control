"""``history.undo`` of writes that change whole parts: comments, styles, style sets.

Each kind: the undo returns every part byte for byte (a part the write created is
removed), history.list marks the entry undone, a later write to the same part makes the
undo refuse with BATCH_CONFLICT, and a dry run writes nothing.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.ops import history as history_ops
from doc_control.ops.batch import batch
from doc_control.ops.styleset import styleset_export, styleset_install
from doc_control.ops.stylewrite import apply_style_ops
from doc_control.safety.history import read_log
from tests.helpers import make_docx

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {W}>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:customStyle="1" w:styleId="Old"><w:name w:val="Old Style"/>'
    '<w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/></w:pPr></w:style></w:styles>'
)


def _reply(fn: Any) -> Callable[[dict[str, Any]], Reply]:
    return cast("Callable[[dict[str, Any]], Reply]", fn)


undo = _reply(history_ops.undo)
history_list = _reply(history_ops.history_list)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_CONTROL_STORE", raising=False)
    monkeypatch.delenv("DOC_CONTROL_HOME", raising=False)


def members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def styled_doc(tmp_path: Path) -> Path:
    path = make_docx(tmp_path / "d.docx", "uno", extra={"word/styles.xml": STYLES.encode()})
    parts = members(path)
    para = '<w:p><w:pPr><w:pStyle w:val="Old"/></w:pPr><w:r><w:t>{}</w:t></w:r></w:p>'
    body = para.format("primero") + "<w:p><w:r><w:t>segundo</w:t></w:r></w:p>"
    body += para.format("tercero")
    parts["word/document.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}</w:body>'
        "</w:document>"
    ).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return path


def run(path: Path, ops: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(_reply(batch)({"doc": str(path), "ops": ops}).result)


def address(path: Path, n: int) -> str:
    return str(Document(str(path)).index.paragraphs[n].address)


def comment(path: Path, n: int = 1, text: str = "Revisar esto") -> dict[str, Any]:
    return run(path, [{"op": "comment.add", "at": address(path, n), "text": text}])


def styleset(path: Path, tmp_path: Path) -> dict[str, Any]:
    source = styled_doc(tmp_path / "src")
    apply_style_ops(source, [{"op": "style.update", "style": "Old", "align": "right"}])
    apply_style_ops(source, [{"op": "style.create", "name": "Nuevo", "id": "Nuevo"}])
    exported = styleset_export({"doc": str(source)}).result["styleset"]  # type: ignore[union-attr]
    return dict(styleset_install({"doc": str(path), "styleset": exported}).result)  # type: ignore[union-attr]


WRITES: dict[str, Callable[[Path, Path], dict[str, Any]]] = {
    "comment.add": lambda p, t: comment(p),
    "comment.resolve": lambda p, t: run(p, [{"op": "comment.resolve", "comment": "0"}]),
    "style.create": lambda p, t: apply_style_ops(p, [{"op": "style.create", "name": "Nuevo"}]),
    "style.update": lambda p, t: apply_style_ops(
        p, [{"op": "style.update", "style": "Old", "bold": True}]
    ),
    "style.rename": lambda p, t: apply_style_ops(
        p, [{"op": "style.rename", "style": "Old", "id": "Renamed", "name": "Renamed"}]
    ),
    "styleset.install": styleset,
}
# A later write that touches the same part as the write under test.
LATER: dict[str, Callable[[Path], object]] = {
    "comment": lambda p: comment(p, 0, "Otro"),
    "style": lambda p: apply_style_ops(p, [{"op": "style.create", "name": "Later"}]),
}


def prepared(name: str, tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    path = styled_doc(tmp_path)
    if name == "comment.resolve":
        comment(path)
    return path


def log_entries(path: Path) -> list[dict[str, Any]]:
    return read_log(path.parent / ".doc-control" / path.name / "log.jsonl").entries


@pytest.mark.parametrize("name", list(WRITES))
def test_undo_puts_every_part_back(name: str, tmp_path: Path) -> None:
    path = prepared(name, tmp_path)
    before = members(path)
    result = WRITES[name](path, tmp_path)
    op_id = result["op_id"]
    assert op_id and members(path) != before
    entry = next(e for e in log_entries(path) if e["op_id"] == op_id)
    assert entry["saved_parts"]
    assert entry["kind"] == ("batch" if name.startswith("comment") else "style")

    written = path.read_bytes()
    dry = undo({"doc": str(path), "op_id": op_id, "dry_run": True}).result
    assert dry["dry_run"] and dry["parts"] and path.read_bytes() == written

    done = undo({"doc": str(path), "op_id": op_id}).result
    assert done["undid"] == op_id and done["parts"]
    assert members(path) == before  # every member, byte for byte, none left over
    rows = history_list({"doc": str(path)}).result["entries"]
    assert next(r for r in rows if r["op_id"] == op_id)["undone"] is True


def test_comment_add_on_a_document_without_comments_removes_the_created_parts(
    tmp_path: Path,
) -> None:
    path = styled_doc(tmp_path)
    before = members(path)
    comment(path)
    added = set(members(path)) - set(before)
    assert "word/comments.xml" in added
    undo({"doc": str(path)})
    assert set(members(path)) == set(before)


@pytest.mark.parametrize("name", list(WRITES))
def test_a_later_write_to_the_part_makes_undo_refuse(name: str, tmp_path: Path) -> None:
    path = prepared(name, tmp_path)
    op_id = WRITES[name](path, tmp_path)["op_id"]
    LATER["comment" if name.startswith("comment") else "style"](path)
    written = path.read_bytes()
    with pytest.raises(DocControlError) as err:
        undo({"doc": str(path), "op_id": op_id})
    assert err.value.code is ErrorCode.BATCH_CONFLICT
    assert path.read_bytes() == written
    # the later write undoes first, then this one
    undo({"doc": str(path)})
    assert undo({"doc": str(path), "op_id": op_id}).result["undid"] == op_id


def test_an_entry_logged_without_saved_parts_is_still_refused(tmp_path: Path) -> None:
    path = styled_doc(tmp_path)
    comment(path)
    log = path.parent / ".doc-control" / path.name / "log.jsonl"
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    for row in lines:
        row.pop("saved_parts", None)
    log.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    written = path.read_bytes()
    with pytest.raises(DocControlError) as err:
        undo({"doc": str(path)})
    assert err.value.code is ErrorCode.UNSUPPORTED_CONTENT
    assert path.read_bytes() == written
