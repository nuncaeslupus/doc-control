"""LibreOffice Writer as a live app, driven over UNO.

LibreOffice takes connections only when started with ``--accept``, so ``open`` starts a
listening instance when none answers on the port; a window the writer opened without it
cannot be attached to. ``uno`` ships with LibreOffice, not PyPI: it is imported on first
use, from LibreOffice's program directory when this Python does not have it.

Environment: ``DOC_CONTROL_LO_PORT`` (default 2002), ``DOC_CONTROL_LO_PROGRAM`` (the
directory holding ``uno.py``), ``DOC_CONTROL_LO_ARGS`` (extra ``soffice`` arguments,
e.g. ``--headless``).
"""

from __future__ import annotations

import ctypes
import functools
import importlib
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from doc_control.live.protocol import LiveUnavailable

_PROGRAM_DIRS = (
    "/usr/lib/libreoffice/program",
    "/snap/libreoffice/current/lib/libreoffice/program",
    "/opt/libreoffice/program",
)
START_TIMEOUT = 90.0
_F = TypeVar("_F", bound=Callable[..., Any])


def _port() -> int:
    return int(os.environ.get("DOC_CONTROL_LO_PORT", "2002"))


def _soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


@functools.cache
def _uno() -> Any:
    """The ``uno`` module, or ``LiveUnavailable`` with why this Python cannot load it."""
    reason = "LibreOffice is not installed"
    try:
        return importlib.import_module("uno")
    except ImportError:
        pass
    # pyuno links libpython, which a uv or pyenv Python does not put on the loader path.
    lib = Path(sysconfig.get_config_var("LIBDIR") or "") / (
        sysconfig.get_config_var("INSTSONAME") or "-"
    )
    for program in (os.environ.get("DOC_CONTROL_LO_PROGRAM"), *_PROGRAM_DIRS):
        if not program or not Path(program, "uno.py").is_file():
            continue
        try:
            if lib.is_file():
                ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
            os.environ.setdefault("URE_BOOTSTRAP", f"vnd.sun.star.pathname:{program}/fundamentalrc")
            sys.path.append(program)
            return importlib.import_module("uno")
        except (ImportError, OSError) as err:
            sys.path.remove(program)
            reason = f"{err} (Python {sys.version_info.major}.{sys.version_info.minor})"
    raise LiveUnavailable(f"LibreOffice's Python bridge (uno) cannot be imported: {reason}")


def _from_uno(err: Exception) -> bool:
    return any("com.sun.star" in cls.__qualname__ for cls in type(err).__mro__)


def _guard(method: _F) -> _F:
    """A UNO failure (the app closed, the bridge dropped) is ``LiveUnavailable``."""

    @functools.wraps(method)
    def run(self: LibreOffice, *args: Any) -> Any:
        try:
            return method(self, *args)
        except LiveUnavailable:
            raise
        except Exception as err:
            if not _from_uno(err):
                raise
            self._desk = None
            raise LiveUnavailable(
                f"LibreOffice failed: {type(err).__name__} {getattr(err, 'Message', '')}".strip()
            ) from err

    return run  # type: ignore[return-value]


class LibreOffice:
    app = "libreoffice"

    def __init__(self) -> None:
        self._desk: Any = None
        self._ctx: Any = None

    def _desktop(self, start: bool = False) -> Any:
        if self._desk is not None:
            return self._desk
        uno = _uno()
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local
        )
        port = _port()
        accept = f"socket,host=localhost,port={port};urp;"
        deadline = 0.0
        while True:
            try:
                self._ctx = resolver.resolve(f"uno:{accept}StarOffice.ComponentContext")
                break
            except Exception as err:
                if not _from_uno(err):
                    raise
                exe = _soffice()
                if not start or exe is None:
                    raise LiveUnavailable(
                        f"LibreOffice is not listening on port {port}: call live.open, "
                        f'or start it with soffice --accept="{accept}"'
                    ) from err
                if not deadline:
                    # ponytail: a localhost socket, open to this machine's other users;
                    # a named pipe if that matters (the snap's private /tmp hides it today).
                    extra = shlex.split(os.environ.get("DOC_CONTROL_LO_ARGS", ""))
                    subprocess.Popen(
                        [exe, "--norestore", "--nologo", f"--accept={accept}", *extra],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    deadline = time.monotonic() + START_TIMEOUT
                elif time.monotonic() > deadline:
                    raise LiveUnavailable(
                        f"LibreOffice did not start listening on port {port}: close the "
                        "LibreOffice windows opened without --accept and call live.open again"
                    ) from err
                time.sleep(0.2)
        self._desk = self._ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", self._ctx
        )
        return self._desk

    def _config(self, node: str) -> Any:
        self._desktop()
        provider = self._ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider", self._ctx
        )
        arg = _uno().createUnoStruct("com.sun.star.beans.PropertyValue")
        arg.Name, arg.Value = "nodepath", node
        return provider.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationUpdateAccess", (arg,)
        )

    def probe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "app": self.app,
            "available": _soffice() is not None,
            "reachable": False,
            "listening": False,
            "version": None,
        }
        try:
            _uno()
            try:
                version = self._version()
            except LiveUnavailable:
                if not out["available"]:
                    raise
                # Not running, but ``open`` can start it.
                return {**out, "reachable": True}
            return {
                **out,
                "available": True,
                "reachable": True,
                "listening": True,
                "version": version,
            }
        except LiveUnavailable as err:
            return {**out, "reason": str(err)}

    @_guard
    def _version(self) -> str:
        return str(self._config("/org.openoffice.Setup/Product").ooSetupVersionAboutBox)

    def _find(self, path: Path) -> Any:
        """The open document at ``path``, or None (also when LibreOffice is not listening)."""
        try:
            desktop = self._desktop()
        except LiveUnavailable:
            return None
        want = path.resolve()
        components = desktop.Components.createEnumeration()
        while components.hasMoreElements():
            component = components.nextElement()
            url = getattr(component, "URL", "")
            if url.startswith("file:") and Path(_uno().fileUrlToSystemPath(url)) == want:
                return component
        return None

    def _doc(self, path: Path) -> Any:
        doc = self._find(path)
        if doc is None:
            raise LiveUnavailable(f"{path.name} is not open in LibreOffice")
        return doc

    @_guard
    def is_open(self, path: Path) -> bool:
        return self._find(path) is not None

    @_guard
    def open(self, path: Path) -> None:
        desktop = self._desktop(start=True)
        if self._find(path) is not None:
            return
        doc = desktop.loadComponentFromURL(path.resolve().as_uri(), "_blank", 0, ())
        if doc is None or not doc.supportsService("com.sun.star.text.TextDocument"):
            if doc is not None:
                doc.close(True)
            raise LiveUnavailable(f"LibreOffice could not open {path.name} as a text document")

    @_guard
    def dirty(self, path: Path) -> bool:
        return bool(self._doc(path).isModified())

    @_guard
    def save(self, path: Path) -> None:
        self._doc(path).store()

    @_guard
    def close(self, path: Path) -> None:
        self._doc(path).close(True)


_ADAPTER = LibreOffice()


def adapter() -> LibreOffice:
    """One adapter per process, so its connection is reused."""
    return _ADAPTER
