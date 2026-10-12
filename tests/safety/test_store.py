"""Document store: layout, id, backups, retention and the app-data option."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import doc_control.safety as safety_mod
import doc_control.safety.store as store_mod
from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.zipio import sha256_file
from doc_control.safety import guard_write
from doc_control.safety.lock import check_changed as real_check_changed
from doc_control.safety.store import (
    DEFAULT_CONFIG,
    DocStore,
    backup,
    create_backup,
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
    root = tmp_path / ".doc-control" / "Novela.docx"
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
    assert (tmp_path / ".doc-control" / "Novela.docx" / "id").read_text().strip() == first


def test_config_overrides_are_read_and_defaults_fill_gaps(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    store.config_path.write_text(json.dumps({"backups": {"keep": 3}}))
    assert DocStore.open(doc).config["backups"] == {"keep": 3, "min_days": 30}


def test_backup_copies_document_with_timestamp_and_sha8(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    sha = sha256_file(doc)
    path = backup(doc, now=NOW)
    assert path.name == f"20261012T114500Z-{sha[:8]}.docx"
    assert path.parent == tmp_path / ".doc-control" / "Novela.docx" / "backups"
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
    store = _with_retention(doc, keep=1, min_days=0)
    foreign = store.backups_dir / "notes.txt"
    foreign.write_text("keep me")
    paths = _seed(store, [100, 90])
    prune_backups(store, now=NOW)
    assert sorted(store.backups_dir.iterdir()) == sorted([foreign, paths[1]])


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
    assert root.name.startswith("Novela.docx-")
    # Same-named documents in different folders get different stores.
    other = tmp_path / "other"
    other.mkdir()
    assert store_dir(make_docx(other / "Novela.docx", "x")) != root


def test_adjacent_mode_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_CONTROL_STORE", "adjacent")
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    assert store_dir(doc) == tmp_path / ".doc-control" / "Novela.docx"


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


# --- review fixes ------------------------------------------------------------


def test_just_created_backup_is_never_pruned(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    _with_retention(doc, keep=0, min_days=0)
    path = guard_write(doc, None, now=NOW)
    assert path.is_file()
    assert path.read_bytes() == doc.read_bytes()


def test_just_created_backup_survives_same_second_sibling(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = _with_retention(doc, keep=1, min_days=0)
    sibling = store.backups_dir / f"{NOW.strftime('%Y%m%dT%H%M%SZ')}-ffffffff.docx"
    sibling.write_bytes(b"b")
    path = guard_write(doc, None, now=NOW)
    assert path.is_file()


def test_keep_is_clamped_to_at_least_one(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = _with_retention(doc, keep=0, min_days=0)
    paths = _seed(store, [100, 90])
    removed = prune_backups(store, now=NOW)
    assert removed == [paths[0]]
    assert list(store.backups_dir.iterdir()) == [paths[1]]


@pytest.mark.parametrize(
    "text",
    [
        '{"backups": 5}',
        '{"backups": {"keep": "abc"}}',
        '{"backups": {"keep": true}}',
        '{"backups": {"keep": -1}}',
        '{"backups": {"min_days": "x"}}',
        '{"backups": {"kep": 5}}',
        '{"backup": {"keep": 50}}',
        "null",
        "[]",
        '{"backups": {"keep": 50,}',
        "",
        '{"backups": {"min_days": 1000000}}',
        '{"backups": {"min_days": 1e10}}',
        '{"backups": {"min_days": Infinity}}',
        '{"backups": {"min_days": -Infinity}}',
        '{"backups": {"min_days": NaN}}',
        '{"backups": {"keep": NaN}}',
        '{"backups": {"keep": 1000000}}',
    ],
)
def test_bad_config_fails_before_any_backup(tmp_path: Path, text: str) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    existing = _seed(store, [400 + i for i in range(25)])
    store.config_path.write_text(text)
    with pytest.raises(DocControlError) as exc:
        guard_write(doc, None, now=NOW)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    assert exc.value.details["path"] == str(store.config_path)
    assert sorted(store.backups_dir.iterdir()) == sorted(existing)
    assert store.config_path.read_text() == text


def test_missing_config_gets_defaults(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    store.config_path.unlink()
    assert DocStore.open(doc).config == DEFAULT_CONFIG
    assert json.loads(store.config_path.read_text()) == DEFAULT_CONFIG


@pytest.mark.parametrize("text", ["not-a-uuid\n", "", "\n"])
def test_corrupt_id_is_an_error_and_never_overwritten(tmp_path: Path, text: str) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    store.id_path.write_text(text)
    with pytest.raises(DocControlError) as exc:
        guard_write(doc, None, now=NOW)
    assert exc.value.code is ErrorCode.INTERNAL
    assert "corrupt" in exc.value.message
    assert exc.value.details["path"] == str(store.id_path)
    assert store.id_path.read_text() == text
    assert list(store.backups_dir.iterdir()) == []


def _appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("DOC_CONTROL_STORE", "appdata")
    monkeypatch.setenv("DOC_CONTROL_HOME", str(home))
    return home


def test_appdata_key_resolves_dotdot_symlinks_and_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _appdata(monkeypatch, tmp_path)
    docs = tmp_path / "docs"
    (docs / "x").mkdir(parents=True)
    doc = make_docx(docs / "Novela.docx", "Hola")
    root = store_dir(doc)
    assert store_dir(docs / "x" / ".." / "Novela.docx") == root
    link = tmp_path / "link.docx"
    link.symlink_to(doc)
    assert store_dir(link) == root
    monkeypatch.chdir(docs)
    assert store_dir(Path("Novela.docx")) == root


def test_appdata_key_is_case_normalised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _appdata(monkeypatch, tmp_path)
    monkeypatch.setattr(store_mod.os.path, "normcase", str.lower)
    upper = store_dir(tmp_path / "DOCS" / "NOVELA.docx")
    lower = store_dir(tmp_path / "docs" / "novela.docx")
    # Same hash key; the folder names differ only in case, which the
    # case-insensitive file systems where normcase lowers treat as one.
    assert upper.parent == lower.parent
    assert upper.name.lower() == lower.name.lower()


def test_same_stem_documents_get_separate_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docx = make_docx(tmp_path / "Novela.docx", "Hola")
    dotx = make_docx(tmp_path / "Novela.dotx", "Plantilla")
    assert store_dir(dotx) == tmp_path / ".doc-control" / "Novela.dotx"
    assert store_dir(docx) != store_dir(dotx)
    _appdata(monkeypatch, tmp_path)
    assert store_dir(docx) != store_dir(dotx)
    assert store_dir(dotx).name.startswith("Novela.dotx-")


def test_backup_name_matches_its_content(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    path = backup(doc, now=NOW)
    assert path.name.split("-")[1][:8] == sha256_file(path)[:8]


def test_change_between_check_and_backup_fails_without_a_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    sha = sha256_file(doc)
    store = DocStore.open(doc)

    def racing_check(path: Path, expect: str | None) -> str:
        actual = real_check_changed(path, expect)
        make_docx(path, "Changed by Word")  # lands after the check, before the copy
        return actual

    monkeypatch.setattr(safety_mod, "check_changed", racing_check)
    with pytest.raises(DocControlError) as exc:
        guard_write(doc, sha, now=NOW)
    assert exc.value.code is ErrorCode.DOC_CHANGED_ON_DISK
    assert list(store.backups_dir.iterdir()) == []


def test_create_backup_rejects_mismatch_and_leaves_nothing(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    with pytest.raises(DocControlError) as exc:
        create_backup(store, doc, NOW, expect_sha256="0" * 64)
    assert exc.value.code is ErrorCode.DOC_CHANGED_ON_DISK
    assert list(store.backups_dir.iterdir()) == []
    ok = create_backup(store, doc, NOW, expect_sha256=sha256_file(doc).upper())
    assert ok.read_bytes() == doc.read_bytes()


def test_config_that_is_a_directory_is_schema_invalid(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    store.config_path.unlink()
    store.config_path.mkdir()
    with pytest.raises(DocControlError) as exc:
        guard_write(doc, None, now=NOW)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    assert exc.value.details["path"] == str(store.config_path)
    assert list(store.backups_dir.iterdir()) == []


def test_unreadable_config_is_schema_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    real_read_text = Path.read_text

    def deny(self: Path, *args: object, **kwargs: object) -> str:
        if self == store.config_path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(DocControlError) as exc:
        DocStore.open(doc)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    assert exc.value.details["path"] == str(store.config_path)


def test_id_that_is_a_directory_is_internal(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    store.id_path.unlink()
    store.id_path.mkdir()
    with pytest.raises(DocControlError) as exc:
        guard_write(doc, None, now=NOW)
    assert exc.value.code is ErrorCode.INTERNAL
    assert exc.value.details["path"] == str(store.id_path)
    assert store.id_path.is_dir()
    assert list(store.backups_dir.iterdir()) == []


def test_bad_config_creates_nothing(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    root = store_dir(doc)
    root.mkdir(parents=True)
    (root / "config.json").write_text('{"backups": 5}')
    with pytest.raises(DocControlError):
        DocStore.open(doc)
    assert sorted(p.name for p in root.iterdir()) == ["config.json"]


def test_bad_id_creates_nothing(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    root = store_dir(doc)
    root.mkdir(parents=True)
    (root / "id").write_text("garbage")
    with pytest.raises(DocControlError):
        DocStore.open(doc)
    assert sorted(p.name for p in root.iterdir()) == ["id"]


def test_name_collision_with_different_content_gets_a_new_name(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    stamp = NOW.strftime("%Y%m%dT%H%M%SZ")
    squatter = store.backups_dir / f"{stamp}-{sha256_file(doc)[:8]}.docx"
    squatter.write_bytes(b"different bytes, same sha8 by accident")
    path = backup(doc, now=NOW)
    assert path != squatter
    assert path.read_bytes() == doc.read_bytes()
    assert squatter.read_bytes() == b"different bytes, same sha8 by accident"
    assert path in [p for _, p in store.backups()]  # still seen by retention
    assert backup(doc, now=NOW) == path  # identical content reuses it


def test_same_content_same_second_reuses_backup(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    first = backup(doc, now=NOW)
    assert backup(doc, now=NOW) == first
    assert len(list(DocStore.open(doc).backups_dir.iterdir())) == 1


# --- round 3: links, unreadable squatters, raw OSError, no-clobber rename -----


def _squatter_name(store: DocStore, doc: Path) -> Path:
    return store.backups_dir / f"{NOW.strftime('%Y%m%dT%H%M%SZ')}-{sha256_file(doc)[:8]}.docx"


def _assert_real_copy(path: Path, doc: Path) -> None:
    assert not path.is_symlink()
    assert path.stat().st_nlink == 1
    assert not path.samefile(doc)
    assert path.read_bytes() == doc.read_bytes()


def test_symlink_to_document_is_not_accepted_as_backup(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    squatter = _squatter_name(store, doc)
    squatter.symlink_to(doc)
    path = backup(doc, now=NOW)
    assert path != squatter
    _assert_real_copy(path, doc)


def test_hard_link_to_document_is_not_accepted_as_backup(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    squatter = _squatter_name(store, doc)
    squatter.hardlink_to(doc)
    path = backup(doc, now=NOW)
    assert path != squatter
    _assert_real_copy(path, doc)


def test_unreadable_same_name_file_is_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    squatter = _squatter_name(store, doc)
    squatter.write_bytes(doc.read_bytes())
    real_open = Path.open

    def deny(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == squatter:
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    path = backup(doc, now=NOW)
    assert path != squatter
    assert path.name.endswith("-1.docx")


def test_store_root_that_is_a_file_is_internal(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    root = store_dir(doc)
    root.parent.mkdir(parents=True)
    root.write_text("not a folder")
    with pytest.raises(DocControlError) as exc:
        guard_write(doc, None, now=NOW)
    assert exc.value.code is ErrorCode.INTERNAL
    assert "path" in exc.value.details
    assert root.read_text() == "not a folder"


def test_backups_that_is_a_file_is_internal_and_writes_nothing(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    root = store_dir(doc)
    root.mkdir(parents=True)
    (root / "backups").write_text("not a folder")
    with pytest.raises(DocControlError) as exc:
        guard_write(doc, None, now=NOW)
    assert exc.value.code is ErrorCode.INTERNAL
    assert exc.value.details["path"] == str(root / "backups")
    assert sorted(p.name for p in root.iterdir()) == ["backups"]


def test_copy_failure_is_internal_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    real_open = Path.open

    def failing(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == doc:
            raise OSError(5, "Input/output error", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing)
    with pytest.raises(DocControlError) as exc:
        backup(doc, now=NOW)
    assert exc.value.code is ErrorCode.INTERNAL
    assert exc.value.details["path"] == str(doc)
    assert list(store.backups_dir.iterdir()) == []


def test_dangling_id_symlink_is_internal_and_kept(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    store.id_path.unlink()
    store.id_path.symlink_to(tmp_path / "gone")
    with pytest.raises(DocControlError) as exc:
        DocStore.open(doc)
    assert exc.value.code is ErrorCode.INTERNAL
    assert exc.value.details["path"] == str(store.id_path)
    assert store.id_path.is_symlink()
    assert not (tmp_path / "gone").exists()


def test_dangling_config_symlink_is_schema_invalid_and_kept(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    store.config_path.unlink()
    store.config_path.symlink_to(tmp_path / "gone")
    with pytest.raises(DocControlError) as exc:
        DocStore.open(doc)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    assert exc.value.details["path"] == str(store.config_path)
    assert store.config_path.is_symlink()
    assert not (tmp_path / "gone").exists()


def test_concurrent_creator_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    squatter = _squatter_name(store, doc)
    real_link = os.link
    real_replace = Path.replace

    def racer() -> None:
        if not squatter.exists():
            squatter.write_bytes(b"written by another process")

    def racing_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        racer()
        real_link(src, dst, *args, **kwargs)

    def racing_replace(self: Path, target: Any) -> Path:
        racer()
        return real_replace(self, target)

    monkeypatch.setattr(os, "link", racing_link)
    monkeypatch.setattr(Path, "replace", racing_replace)
    path = backup(doc, now=NOW)
    assert squatter.read_bytes() == b"written by another process"
    assert path.name.endswith("-1.docx")
    _assert_real_copy(path, doc)
    assert not [p for p in store.backups_dir.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback path")
def test_filesystem_without_hard_links_still_backs_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_docx(tmp_path / "Novela.docx", "Hola")
    store = DocStore.open(doc)
    squatter = _squatter_name(store, doc)
    squatter.write_bytes(b"written by another process")

    def no_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    path = backup(doc, now=NOW)
    assert squatter.read_bytes() == b"written by another process"
    assert path.name.endswith("-1.docx")
    _assert_real_copy(path, doc)
    assert not [p for p in store.backups_dir.iterdir() if p.name.endswith(".tmp")]
