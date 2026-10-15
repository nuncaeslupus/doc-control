"""Writer safety (T44): any editor's lock, where renames were skipped, a chosen store.

Gate: ``writer_safety_failures == 0``, the sum of: writes that went through while an
editor's lock file was present (Word's ``~$`` forms, LibreOffice's ``.~lock.<name>#``),
skipped rename matches reported without a location, and files written outside the
caller's store and the document itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from tests.helpers import make_docx

FIELD = (
    'Hola,</w:t></w:r><w:fldSimple w:instr="REF x"><w:r><w:t>Ana</w:t></w:r>'
    "</w:fldSimple><w:r><w:t>, adiós"
)


def _write(doc: Path, **open_args: Any) -> None:
    with api.open(doc, **open_args) as d:
        first = d.paragraphs()[0]["at"]
        d.run("doc.batch", {"ops": [{"op": "text.replace", "at": f"{first}:0-3", "text": "Uno"}]})


def _locked_writes(tmp: Path) -> int:
    through = 0
    for name, lock in [
        ("Novela.docx", "~$Novela.docx"),
        ("Manuscrito.docx", "~$nuscrito.docx"),
        ("Novela.docx", ".~lock.Novela.docx#"),
        ("Manuscrito largo.docx", ".~lock.Manuscrito largo.docx#"),
    ]:
        folder = tmp / f"lock{through}{len(lock)}"
        folder.mkdir()
        doc = make_docx(folder / name, "Una frase.")
        (folder / lock).write_text("owner", "utf-8")
        try:
            _write(doc)
        except DocControlError as err:
            assert err.code is ErrorCode.DOC_LOCKED
        else:
            through += 1
    return through


def _skips_without_location(tmp: Path) -> int:
    doc = make_docx(tmp / "rename.docx", "Ana llegó.", FIELD, "Y Ana se fue.")
    with api.open(doc, store=tmp / "store-r") as d:
        result = d.run("term.rename", {"from": "Ana", "to": "Eva", "dry_run": True})
    skipped = result["skipped"]
    assert result["counts"]["skipped_unsupported"] == len(skipped) >= 1
    return sum(1 for s in skipped if not (s.get("at") and s.get("reason") and s.get("text")))


def _files_outside_store(tmp: Path) -> int:
    folder = tmp / "writer"
    folder.mkdir()
    doc = make_docx(folder / "Novela.docx", "Una frase.", "Otra.")
    store = tmp / "workspace-store"
    before = {p.name for p in folder.iterdir()}
    _write(doc, store=store)
    after = {p.name for p in folder.iterdir()}
    assert any(store.rglob("*.docx"))  # the backup went to the chosen store
    return len(after - before)


def test_locks_skips_and_store(tmp_path: Path) -> None:
    failures = _locked_writes(tmp_path) + _skips_without_location(tmp_path)
    failures += _files_outside_store(tmp_path)
    print(f"writer_safety_failures == {failures}")
    assert failures == 0


def test_store_dir_env_is_honoured(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DOC_CONTROL_STORE_DIR", str(tmp_path / "env-store"))
    folder = tmp_path / "w"
    folder.mkdir()
    doc = make_docx(folder / "N.docx", "Una frase.")
    _write(doc)
    assert [p.name for p in folder.iterdir()] == ["N.docx"]
    assert any((tmp_path / "env-store").rglob("*.docx"))
