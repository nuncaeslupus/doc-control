"""Document store: layout, id, backups, retention and the app-data option."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from doc_control.ooxml.zipio import sha256_file
from doc_control.safety import guard_write
from doc_control.safety.store import (
    DEFAULT_CONFIG,
    DocStore,
    backup,
    prune_backups,
    store_dir,
    user_home,
)
from tests.helpers import make_docx

NOW = datetime(2026, 10, 12, 11, 45, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_CONTROL_STORE", raising=False)
    monkeypatch.delenv("DOC_CONTROL_HOME", raising=False)


def test_adjacent_store_layout(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    root = tmp_path / ".doc-control" / "Novela"
    assert store.root == root
    assert store.id_path == root / "id"
    assert store.backups_dir == root / "backups"
    assert store.config_path == root / "config.json"
    assert store.log_path == root / "log.jsonl"
    assert store.backups_dir.is_dir()
    assert json.loads(store.config_path.read_text()) == DEFAULT_CONFIG
    assert DEFAULT_CONFIG == {"backups": {"keep": 20, "min_days": 30}}
    assert not store.log_path.exists()  # the log itself is written by T13


def test_document_id_is_a_uuid_and_persists(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    first = DocStore.open(doc).doc_id
    assert str(uuid.UUID(first)) == first
    assert DocStore.open(doc).doc_id == first
    assert (tmp_path / ".doc-control" / "Novela" / "id").read_text().strip() == first


def test_config_overrides_are_read_and_defaults_fill_gaps(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    store.config_path.write_text(json.dumps({"backups": {"keep": 3}, "other": 1}))
    assert DocStore.open(doc).config["backups"] == {"keep": 3, "min_days": 30}


def test_backup_copies_document_with_timestamp_and_sha8(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    sha = sha256_file(doc)
    path = backup(doc, now=NOW)
    assert path.name == f"20261012T114500Z-{sha[:8]}.docx"
    assert path.parent == tmp_path / ".doc-control" / "Novela" / "backups"
    assert path.read_bytes() == doc.read_bytes()


def _seed(store: DocStore, ages_days: list[float]) -> list[Path]:
    paths = []
    for i, age in enumerate(ages_days):
        stamp = (NOW - timedelta(days=age)).strftime("%Y%m%dT%H%M%SZ")
        p = store.backups_dir / f"{stamp}-{i:08x}.docx"
        p.write_bytes(b"b")
        paths.append(p)
    return paths


def _with_retention(doc: Path, keep: int, min_days: int) -> DocStore:
    DocStore.open(doc).config_path.write_text(
        json.dumps({"backups": {"keep": keep, "min_days": min_days}})
    )
    return DocStore.open(doc)


def test_retention_keeps_last_n_when_they_outnumber_recent(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = _with_retention(doc, keep=3, min_days=30)
    paths = _seed(store, [100, 90, 80, 70, 60])  # all older than 30 days
    store.log_path.write_text('{"log_version": 1}\n')
    removed = prune_backups(store, now=NOW)
    assert sorted(removed) == sorted(paths[:2])
    assert sorted(store.backups_dir.iterdir()) == sorted(paths[2:])
    assert store.log_path.read_text() == '{"log_version": 1}\n'


def test_retention_keeps_all_younger_than_min_days(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = _with_retention(doc, keep=2, min_days=30)
    paths = _seed(store, [45, 29, 20, 10, 5, 1])
    removed = prune_backups(store, now=NOW)
    assert removed == [paths[0]]
    assert len(list(store.backups_dir.iterdir())) == 5


def test_retention_ignores_foreign_files(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = _with_retention(doc, keep=0, min_days=0)
    foreign = store.backups_dir / "notes.txt"
    foreign.write_text("keep me")
    _seed(store, [100])
    prune_backups(store, now=NOW)
    assert list(store.backups_dir.iterdir()) == [foreign]


def test_backup_prunes_with_default_retention(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    _seed(store, [400 + i for i in range(25)])
    newest = guard_write(doc, None, now=NOW)
    remaining = list(store.backups_dir.iterdir())
    assert len(remaining) == 20
    assert newest in remaining


def test_appdata_mode_uses_doc_control_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("DOC_CONTROL_STORE", "appdata")
    monkeypatch.setenv("DOC_CONTROL_HOME", str(home))
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = make_docx(docs / "Novela.docx", "Hola")
    path = guard_write(doc, sha256_file(doc), now=NOW)
    assert not (docs / ".doc-control").exists()
    assert path.is_relative_to(home / "stores")
    root = store_dir(doc)
    assert root.parent == home / "stores"
    assert root.name.startswith("Novela-")
    # Same-named documents in different folders get different stores.
    other = tmp_path / "other"
    other.mkdir()
    assert store_dir(make_docx(other / "Novela.docx", "x")) != root


def test_adjacent_mode_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_CONTROL_STORE", "adjacent")
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    assert store_dir(doc) == tmp_path / ".doc-control" / "Novela"


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("win32", {"APPDATA": "C:/Roaming"}, "C:/Roaming/doc-control"),
        ("darwin", {}, "~/Library/Application Support/doc-control"),
        ("linux", {}, "~/.config/doc-control"),
        ("linux", {"XDG_CONFIG_HOME": "/xdg"}, "/xdg/doc-control"),
        ("linux", {"DOC_CONTROL_HOME": "/custom"}, "/custom"),
    ],
)
def test_user_home_platform_defaults(platform: str, env: dict[str, str], expected: str) -> None:
    assert user_home(env=env, platform=platform) == Path(expected).expanduser()
