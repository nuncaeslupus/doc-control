"""Read a .docx package and write a new one that changes only the given parts.

Untouched members are copied raw: their compressed bytes, method, flags,
timestamps, names, extra fields, attributes and comments go into the new file
verbatim, and so does the order. Replaced members keep their header fields and
are recompressed with their original method; new members are appended,
deflated. The new file is written next to the target with the target's
permission bits and moved into place with an atomic replace, so a failure never
leaves a half-written document.

The writer is our own (stdlib ``zipfile`` only parses and validates), because
``zipfile`` can only write a member by recompressing it. ZIP64 archives and
encrypted members are refused: Word never writes them for normal documents.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import struct
import tempfile
import time
import zipfile
import zlib
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from doc_control.errors import DocControlError, ErrorCode

REQUIRED_PARTS = ("[Content_Types].xml", "word/document.xml")

_LOCAL = struct.Struct("<4s5H3L2H")
_CENTRAL = struct.Struct("<4s4B4HL2L5H2L")
_END = struct.Struct("<4s4H2LH")
_LOCAL_SIG = b"PK\x03\x04"
_CENTRAL_SIG = b"PK\x01\x02"
_END_SIG = b"PK\x05\x06"
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_ZIP64_EXTRA_ID = 0x0001
_U16 = 0xFFFF
_U32 = 0xFFFFFFFF
_FLAG_ENCRYPTED = 0x0001
_FLAG_DATA_DESCRIPTOR = 0x0008
_FLAG_UTF8 = 0x0800
# Deflate option bits 1-2 of the general purpose flags -> zlib level.
_DEFLATE_LEVELS = {0: 6, 1: 9, 2: 1, 3: 1}
_SUPPORTED_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_READ_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    NotImplementedError,
    RuntimeError,
    EOFError,
    OSError,
    ValueError,
    struct.error,
)


class _Member:
    """A member's central-directory record plus where its raw data lives."""

    def __init__(self, info: zipfile.ZipInfo, raw: bytes) -> None:
        self.info = info
        off = info.header_offset
        if raw[off : off + 4] != _LOCAL_SIG:
            raise zipfile.BadZipFile(f"bad local header for {info.filename}")
        fields = _LOCAL.unpack_from(raw, off)
        nlen, xlen = fields[9], fields[10]
        start = off + _LOCAL.size
        self.local_extra = raw[start + nlen : start + nlen + xlen]
        data_at = start + nlen + xlen
        if data_at + info.compress_size > len(raw):
            raise zipfile.BadZipFile(f"truncated data for {info.filename}")
        self.data = raw[data_at : data_at + info.compress_size]

    @property
    def name_bytes(self) -> bytes:
        return _encode_name(self.info.orig_filename, self.info.flag_bits)


class Package:
    """A read-only view of a .docx zip, with members in their original order."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if not path.is_file():
            raise DocControlError(ErrorCode.DOC_NOT_FOUND, f"no such file: {path}", path=str(path))
        raw = path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        try:
            self._load(raw)
        except DocControlError:
            raise
        except _READ_ERRORS as exc:
            raise DocControlError(
                ErrorCode.NOT_A_DOCX, f"corrupt zip package {path}: {exc}", path=str(path)
            ) from exc
        missing = [name for name in REQUIRED_PARTS if name not in self._data]
        if missing:
            raise DocControlError(
                ErrorCode.NOT_A_DOCX,
                f"missing parts: {', '.join(missing)}",
                missing=missing,
                path=str(path),
            )

    def _load(self, raw: bytes) -> None:
        path = str(self.path)
        end = _find_end(raw)
        if end < 0:
            raise DocControlError(ErrorCode.NOT_A_DOCX, f"not a zip package: {path}", path=path)
        if _has_zip64_end(raw, end):
            raise _unsupported(path, "ZIP64 archives are not supported")
        # zipfile finds the end record with rfind, so an archive comment holding an end
        # signature misleads it. Hand it the archive without the comment (member offsets
        # do not move) and keep the comment ourselves.
        self.comment = raw[end + _END.size :]
        view = raw[:end] + _END.pack(*_END.unpack_from(raw, end)[:7], 0)
        with zipfile.ZipFile(io.BytesIO(view)) as zf:
            self.infos: list[zipfile.ZipInfo] = zf.infolist()
            dupes = sorted(n for n, c in Counter(i.filename for i in self.infos).items() if c > 1)
            if dupes:
                raise DocControlError(
                    ErrorCode.NOT_A_DOCX,
                    f"duplicate member names: {', '.join(dupes)}",
                    duplicates=dupes,
                    path=path,
                )
            self._members: dict[str, _Member] = {}
            self._data: dict[str, bytes] = {}
            for info in self.infos:
                name = info.filename
                if info.flag_bits & _FLAG_ENCRYPTED:
                    raise _unsupported(path, f"encrypted member: {name}")
                member = _Member(info, raw)
                if _is_zip64(info, member.local_extra):
                    raise _unsupported(path, f"ZIP64 member: {name}")
                if info.compress_type not in _SUPPORTED_METHODS:
                    raise zipfile.BadZipFile(
                        f"unsupported compression method {info.compress_type} in {name}"
                    )
                try:
                    self._data[name] = zf.read(info)
                except _READ_ERRORS as exc:
                    raise zipfile.BadZipFile(f"{name}: {exc}") from exc
                self._members[name] = member

    @property
    def names(self) -> list[str]:
        return [info.filename for info in self.infos]

    def read(self, name: str) -> bytes:
        return self._data[name]

    def __contains__(self, name: object) -> bool:
        return name in self._data


def write_package(pkg: Package, replacements: Mapping[str, bytes], dest: Path) -> str:
    """Write ``pkg`` with ``replacements`` applied to ``dest``; return its sha256.

    Names not in the package are appended as new deflated members.
    """
    for name, data in replacements.items():
        if not isinstance(data, bytes):
            raise TypeError(f"replacement for {name} must be bytes")
    payload = _build(pkg, replacements)
    mode = stat.S_IMODE(dest.stat().st_mode) if dest.exists() else 0o666 & ~_umask()
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(mode)
        _replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return hashlib.sha256(payload).hexdigest()


def _build(pkg: Package, replacements: Mapping[str, bytes]) -> bytes:
    out = bytearray()
    central = bytearray()
    count = 0

    def emit(
        info: zipfile.ZipInfo,
        name: bytes,
        flags: int,
        method: int,
        crc: int,
        size: int,
        data: bytes,
        local_extra: bytes,
    ) -> None:
        nonlocal count
        offset = len(out)
        if offset > _U32 or len(data) > _U32 or size > _U32:
            raise _unsupported(str(pkg.path), "result would need ZIP64")
        dos_time, dos_date = _dos_datetime(info.date_time)
        out.extend(
            _LOCAL.pack(
                _LOCAL_SIG,
                info.extract_version,
                flags,
                method,
                dos_time,
                dos_date,
                crc,
                len(data),
                size,
                len(name),
                len(local_extra),
            )
        )
        out.extend(name + local_extra + data)
        central.extend(
            _CENTRAL.pack(
                _CENTRAL_SIG,
                info.create_version,
                info.create_system,
                info.extract_version,
                info.reserved,
                flags,
                method,
                dos_time,
                dos_date,
                crc,
                len(data),
                size,
                len(name),
                len(info.extra),
                len(info.comment),
                0,
                info.internal_attr,
                info.external_attr,
                offset,
            )
        )
        central.extend(name + info.extra + info.comment)
        count += 1

    for info in pkg.infos:
        member = pkg._members[info.filename]
        flags = info.flag_bits & ~_FLAG_DATA_DESCRIPTOR
        if info.filename in replacements:
            content = replacements[info.filename]
            data = _compress(content, info.compress_type, flags)
            crc, size = zlib.crc32(content), len(content)
        else:
            data, crc, size = member.data, info.CRC, info.file_size
        emit(
            info, member.name_bytes, flags, info.compress_type, crc, size, data, member.local_extra
        )

    for name, content in replacements.items():
        if name in pkg:
            continue
        info = zipfile.ZipInfo(name, time.localtime()[:6])
        info.create_system = 0
        info.create_version = info.extract_version = 20
        info.external_attr = 0
        flags = 0 if name.isascii() else _FLAG_UTF8
        data = _compress(content, zipfile.ZIP_DEFLATED, flags)
        emit(
            info,
            _encode_name(name, flags),
            flags,
            zipfile.ZIP_DEFLATED,
            zlib.crc32(content),
            len(content),
            data,
            b"",
        )

    if count > _U16 or len(out) > _U32 or len(central) > _U32:
        raise _unsupported(str(pkg.path), "result would need ZIP64")
    cd_offset = len(out)
    out.extend(central)
    out.extend(_END.pack(_END_SIG, 0, 0, count, count, len(central), cd_offset, len(pkg.comment)))
    out.extend(pkg.comment)
    return bytes(out)


def _compress(content: bytes, method: int, flags: int) -> bytes:
    if method == zipfile.ZIP_STORED:
        return content
    level = _DEFLATE_LEVELS[(flags >> 1) & 0x3]
    comp = zlib.compressobj(level, zlib.DEFLATED, -15)
    return comp.compress(content) + comp.flush()


def _dos_datetime(dt: tuple[int, int, int, int, int, int]) -> tuple[int, int]:
    year, month, day, hour, minute, second = dt
    return (hour << 11) | (minute << 5) | (second // 2), ((year - 1980) << 9) | (month << 5) | day


def _encode_name(name: str, flags: int) -> bytes:
    return name.encode("utf-8" if flags & _FLAG_UTF8 else "cp437")


def _extra_ids(extra: bytes) -> set[int]:
    ids: set[int] = set()
    pos = 0
    while pos + 4 <= len(extra):
        tag, length = struct.unpack_from("<2H", extra, pos)
        ids.add(tag)
        pos += 4 + length
    return ids


def _is_zip64(info: zipfile.ZipInfo, local_extra: bytes) -> bool:
    if max(info.file_size, info.compress_size, info.header_offset) >= _U32:
        return True
    return _ZIP64_EXTRA_ID in _extra_ids(info.extra) | _extra_ids(local_extra)


def _umask() -> int:
    """The process umask (read by setting and restoring it; there is no getter)."""
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def _find_end(raw: bytes) -> int:
    """Offset of the real end record: the last one whose comment runs exactly to EOF."""
    pos = len(raw) - _END.size
    floor = max(0, pos - _U16)
    while pos >= floor:
        pos = raw.rfind(_END_SIG, floor, pos + 4)
        if pos < 0:
            break
        if _END.unpack_from(raw, pos)[7] == len(raw) - pos - _END.size:
            return pos
        pos -= 1
    return -1


def _has_zip64_end(raw: bytes, end: int) -> bool:
    """Whether the end record at ``end`` is a ZIP64 one (locator just before it, or sentinels)."""
    if end >= 20 and raw[end - 20 : end - 16] == _ZIP64_LOCATOR_SIG:
        return True
    fields = _END.unpack_from(raw, end)
    return _U16 in fields[1:5] or _U32 in fields[5:7]


def _unsupported(path: str, message: str) -> DocControlError:
    return DocControlError(ErrorCode.UNSUPPORTED_CONTENT, message, path=path)


def _replace(src: Path, dest: Path) -> None:
    """``Path.replace`` with one retry, for Windows targets briefly held open."""
    try:
        src.replace(dest)
    except PermissionError:
        time.sleep(0.5)
        src.replace(dest)
