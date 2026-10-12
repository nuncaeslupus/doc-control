"""Read a .docx package and write a new one that changes only the given parts.

Untouched members keep their bytes, order, compression method, timestamps and
attributes; replaced members keep their original ZipInfo. The new file is
written next to the target and moved into place with an atomic replace, so a
failure never leaves a half-written document.
"""

from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path

from doc_control.errors import DocControlError, ErrorCode

REQUIRED_PARTS = ("[Content_Types].xml", "word/document.xml")


class Package:
    """A read-only view of a .docx zip, with members in their original order."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if not path.is_file():
            raise DocControlError(ErrorCode.DOC_NOT_FOUND, f"no such file: {path}", path=str(path))
        if not zipfile.is_zipfile(path):
            raise DocControlError(ErrorCode.NOT_A_DOCX, f"not a zip package: {path}")
        with zipfile.ZipFile(path) as zf:
            self.infos: list[zipfile.ZipInfo] = zf.infolist()
            self._data = {info.filename: zf.read(info) for info in self.infos}
        missing = [name for name in REQUIRED_PARTS if name not in self._data]
        if missing:
            raise DocControlError(
                ErrorCode.NOT_A_DOCX, f"missing parts: {', '.join(missing)}", missing=missing
            )
        self.sha256 = sha256_file(path)

    @property
    def names(self) -> list[str]:
        return [info.filename for info in self.infos]

    def read(self, name: str) -> bytes:
        return self._data[name]

    def __contains__(self, name: object) -> bool:
        return name in self._data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_package(pkg: Package, replacements: Mapping[str, bytes], dest: Path) -> str:
    """Write ``pkg`` with ``replacements`` applied to ``dest``; return its sha256.

    Names not in the package are appended as new deflated members.
    """
    for name, data in replacements.items():
        if not isinstance(data, bytes):
            raise TypeError(f"replacement for {name} must be bytes")
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w") as out:
            for info in pkg.infos:
                data = replacements.get(info.filename, pkg.read(info.filename))
                out.writestr(copy.copy(info), data)
            for name, data in replacements.items():
                if name not in pkg:
                    out.writestr(
                        zipfile.ZipInfo(name, time.localtime()[:6]),
                        data,
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
        _replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return sha256_file(dest)


def _replace(src: Path, dest: Path) -> None:
    """``Path.replace`` with one retry, for Windows targets briefly held open."""
    try:
        src.replace(dest)
    except PermissionError:
        time.sleep(0.5)
        src.replace(dest)
