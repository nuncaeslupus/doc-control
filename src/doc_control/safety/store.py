"""The per-document store: id, backups with retention, config and log path.

Adjacent mode (default) keeps ``.doc-control/<file name>/`` next to the
document; the full name (``Novela.docx``, not ``Novela``) keeps same-stem files
such as ``Novela.docx`` and ``Novela.dotx`` from sharing a store. With
``DOC_CONTROL_STORE=appdata`` the store lives under the user store
(``DOC_CONTROL_HOME`` or the platform default) at
``stores/<file name>-<hash of the resolved path>/``, so same-named documents in
different folders never share one, while ``x/../Novela.docx``, a symlink to it
and (on Windows) a different drive-letter case all map to the same store.

``config.json`` is validated on load, and a bad one is ``SCHEMA_INVALID``
before anything is copied or pruned. A corrupt ``id`` is an error and is never
overwritten, since a fresh id would orphan the document's history.

Retention prunes full backups only; ``log.jsonl`` is never touched here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.schema import validate

STORE_DIRNAME = ".doc-control"
APP_NAME = "doc-control"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DEFAULT_CONFIG: dict[str, Any] = {"backups": {"keep": 20, "min_days": 30}}
CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "backups": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "keep": {"type": "integer", "minimum": 0, "maximum": 100_000},
                "min_days": {"type": "number", "minimum": 0, "maximum": 36_500},
            },
        },
    },
}
# ``-<n>`` disambiguates a same-second, same-sha8 backup whose bytes differ.
_BACKUP_RE = re.compile(r"^(?P<ts>\d{8}T\d{6}Z)-(?P<sha>[0-9a-f]{8})(?:-(?P<n>\d+))?\.docx$")


def _reject_constant(name: str) -> Any:
    """``json.loads`` hook: NaN and ±Infinity are not JSON and are never valid config."""
    raise ValueError(f"non-finite number {name} is not allowed")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def user_home(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """The user store: ``DOC_CONTROL_HOME`` or the platform default."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("DOC_CONTROL_HOME"):
        return Path(env["DOC_CONTROL_HOME"]).expanduser()
    if platform.startswith("win"):
        base = env.get("APPDATA")
        return (Path(base) if base else Path("~/AppData/Roaming").expanduser()) / APP_NAME
    if platform == "darwin":
        return Path("~/Library/Application Support").expanduser() / APP_NAME
    xdg = env.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path("~/.config").expanduser()) / APP_NAME


def store_dir(doc_path: Path, env: Mapping[str, str] | None = None) -> Path:
    """Where the store for ``doc_path`` lives under the configured mode."""
    env = os.environ if env is None else env
    doc_path = doc_path.resolve()
    if env.get("DOC_CONTROL_STORE", "adjacent").strip().lower() == "appdata":
        canonical = os.path.normcase(str(doc_path))
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        return user_home(env) / "stores" / f"{doc_path.name}-{key}"
    return doc_path.parent / STORE_DIRNAME / doc_path.name


def _merge(defaults: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(defaults)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry where the platform allows it (not on Windows)."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_text_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class DocStore:
    root: Path
    doc_id: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def id_path(self) -> Path:
        return self.root / "id"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def log_path(self) -> Path:
        return self.root / "log.jsonl"

    @classmethod
    def open(cls, doc_path: Path, env: Mapping[str, str] | None = None) -> DocStore:
        """Open (creating if needed) the store for ``doc_path``.

        Raises ``SCHEMA_INVALID`` for a bad ``config.json`` and ``INTERNAL``
        for a corrupt ``id``, before any backup is taken.
        """
        store = cls(store_dir(doc_path, env))
        # Read and validate everything first: an error path creates nothing.
        config = store._load_config()
        doc_id = store._load_id()
        store.backups_dir.mkdir(parents=True, exist_ok=True)
        if config is None:
            _write_text_atomic(store.config_path, json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
            config = copy.deepcopy(DEFAULT_CONFIG)
        if doc_id is None:
            doc_id = str(uuid.uuid4())
            _write_text_atomic(store.id_path, doc_id + "\n")
        store.config = config
        store.doc_id = doc_id
        return store

    def _load_id(self) -> str | None:
        """The stored id, or None when there is none yet; never rewrites it."""
        if not self.id_path.exists():
            return None
        try:
            return str(uuid.UUID(self.id_path.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError) as exc:
            # Never mint a replacement: a new id would orphan the history
            # logged under the old one, so a human has to look at it.
            raise DocControlError(
                ErrorCode.INTERNAL,
                f"document store id is corrupt or unreadable: {self.id_path} ({exc})",
                path=str(self.id_path),
            ) from exc

    def _load_config(self) -> dict[str, Any] | None:
        """The validated config merged over defaults, or None when the file is absent."""
        if not self.config_path.exists():
            return None
        try:
            loaded = json.loads(
                self.config_path.read_text(encoding="utf-8"), parse_constant=_reject_constant
            )
        except (OSError, ValueError) as exc:  # unreadable, bad JSON, bad UTF-8, NaN/Infinity
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                f"{self.config_path} is not readable, valid JSON: {exc}",
                path=str(self.config_path),
            ) from exc
        try:
            validate(loaded, CONFIG_SCHEMA)
        except DocControlError as exc:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                f"{self.config_path}: {exc.message}",
                path=str(self.config_path),
                field=exc.details.get("path"),
            ) from exc
        return _merge(DEFAULT_CONFIG, loaded)

    def backups(self) -> list[tuple[datetime, Path]]:
        """Backups in this store, oldest first; files not named like a backup are ignored."""
        found: list[tuple[datetime, Path]] = []
        for path in self.backups_dir.iterdir():
            match = _BACKUP_RE.match(path.name)
            if match and path.is_file():
                when = datetime.strptime(match["ts"], TIMESTAMP_FORMAT).replace(tzinfo=UTC)
                found.append((when, path))
        found.sort()
        return found


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=UTC)


def create_backup(
    store: DocStore,
    doc_path: Path,
    now: datetime | None = None,
    expect_sha256: str | None = None,
) -> Path:
    """Copy ``doc_path`` into ``store`` as ``<UTC timestamp>-<sha8>.docx``, flushed to disk.

    The bytes are hashed as they are copied, so the name always matches the
    content. If ``expect_sha256`` is given and the copied bytes differ, the
    partial backup is removed and ``DOC_CHANGED_ON_DISK`` is raised.
    """
    stamp = _now(now).astimezone(UTC).strftime(TIMESTAMP_FORMAT)
    digest = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(dir=store.backups_dir, prefix=".backup.", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as out, doc_path.open("rb") as src:
            while chunk := src.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        actual = digest.hexdigest()
        if expect_sha256 is not None and actual != expect_sha256.strip().lower():
            raise DocControlError(
                ErrorCode.DOC_CHANGED_ON_DISK,
                f"{doc_path.name} changed on disk since it was read",
                path=str(doc_path),
                expected_sha256=expect_sha256,
                actual_sha256=actual,
            )
        base = f"{stamp}-{actual[:8]}"
        dest = store.backups_dir / f"{base}.docx"
        n = 0
        while dest.exists():
            if dest.is_file() and _sha256_of(dest) == actual:
                tmp_path.unlink()
                return dest  # same content in the same second: already backed up
            n += 1  # same name, different bytes: never return or replace it
            dest = store.backups_dir / f"{base}-{n}.docx"
        tmp_path.replace(dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_dir(store.backups_dir)
    return dest


def prune_backups(
    store: DocStore, now: datetime | None = None, protect: Path | None = None
) -> list[Path]:
    """Delete backups beyond retention; return what was removed, oldest first.

    A backup is kept if it is among the newest ``backups.keep`` (at least 1) or
    younger than ``backups.min_days``, whichever keeps more. ``protect`` (the
    backup just taken) is never removed: timestamps are truncated to the
    second, so it need not sort last. The log is never deleted.
    """
    settings = store.config.get("backups", {})
    keep = max(1, int(settings.get("keep", DEFAULT_CONFIG["backups"]["keep"])))
    min_days = max(0, float(settings.get("min_days", DEFAULT_CONFIG["backups"]["min_days"])))
    cutoff = _now(now) - timedelta(days=min_days)
    entries = store.backups()
    newest = {path for _, path in entries[-keep:]}
    removed: list[Path] = []
    for when, path in entries:
        if path in newest or path == protect or when > cutoff:
            continue
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def backup(
    doc_path: Path,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    expect_sha256: str | None = None,
) -> Path:
    """Back up ``doc_path`` into its store, apply retention, return the backup path.

    The store's config and id are validated before anything is copied or pruned.
    """
    store = DocStore.open(doc_path, env)
    path = create_backup(store, doc_path, now, expect_sha256)
    prune_backups(store, now, protect=path)
    return path
