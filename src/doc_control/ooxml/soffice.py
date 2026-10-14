"""Headless LibreOffice, used to recompute field results (``fields.update``).

LibreOffice does not rebuild a table of contents or refresh page references when it merely
converts a file, so a one-off Basic macro does it: it lives in a throw-away user profile
(created here, nothing is installed), loads ``in.docx``, updates every index and every text
field and saves ``out.docx``. The document passed in is never touched: callers hand over a
copy and read the saved result.

The discovery is the validator's (``tests/corpus/validator.py``): ``soffice`` or
``libreoffice`` on ``PATH``; ``DOC_CONTROL_NO_SOFFICE`` forces "absent".
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from doc_control.errors import DocControlError, ErrorCode

NO_SOFFICE_ENV = "DOC_CONTROL_NO_SOFFICE"
WORK_ENV = "DOC_CONTROL_LO_DIR"
TIMEOUT = 300

_MODULE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">
<script:module xmlns:script="http://openoffice.org/2000/script" script:name="Module1" \
script:language="StarBasic">
Sub Refresh()
  Dim dir As String
  dir = Environ("DOC_CONTROL_LO_DIR")
  Dim load(0) As New com.sun.star.beans.PropertyValue
  load(0).Name = "Hidden"
  load(0).Value = True
  Dim doc As Object
  doc = StarDesktop.loadComponentFromURL(ConvertToURL(dir &amp; "/in.docx"), "_blank", 0, load())
  If IsNull(doc) Then Exit Sub
  Dim idx As Object
  idx = doc.getDocumentIndexes()
  Dim i As Long
  For i = 0 To idx.getCount() - 1
    idx.getByIndex(i).update()
  Next i
  doc.getTextFields().refresh()
  Dim save(0) As New com.sun.star.beans.PropertyValue
  save(0).Name = "FilterName"
  save(0).Value = "MS Word 2007 XML"
  doc.storeToURL(ConvertToURL(dir &amp; "/out.docx"), save())
  doc.close(True)
End Sub
</script:module>
"""
_LIBRARY = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE library:library PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "library.dtd">
<library:library xmlns:library="http://openoffice.org/2000/library" library:name="Standard" \
library:readonly="false" library:passwordprotected="false">
 <library:element library:name="Module1"/>
</library:library>
"""
_DIALOGS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE library:library PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "library.dtd">
<library:library xmlns:library="http://openoffice.org/2000/library" library:name="Standard" \
library:readonly="false" library:passwordprotected="false"/>
"""
_LIBRARIES = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE library:libraries PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" \
"libraries.dtd">
<library:libraries xmlns:library="http://openoffice.org/2000/library" \
xmlns:xlink="http://www.w3.org/1999/xlink">
 <library:library library:name="Standard" xlink:href="$(USER)/basic/Standard/{kind}.xlb/" \
xlink:type="simple" library:link="false"/>
</library:libraries>
"""


def soffice_path() -> str | None:
    """The LibreOffice executable, or ``None`` when there is none (or it is switched off)."""
    if os.environ.get(NO_SOFFICE_ENV):
        return None
    return shutil.which("soffice") or shutil.which("libreoffice")


@contextlib.contextmanager
def _work_dir(soffice: str) -> Iterator[Path]:
    # A snap-confined LibreOffice cannot see /tmp: it gets a directory under the home.
    base = Path.home() if soffice.startswith("/snap/") else None
    path = Path(tempfile.mkdtemp(prefix="doc-control-lo-", dir=base))
    try:
        yield path
    finally:
        deadline = time.monotonic() + 30
        while path.exists() and time.monotonic() < deadline:
            # LibreOffice writes its profile for a moment after it is told to stop.
            shutil.rmtree(path, ignore_errors=True)
            time.sleep(0.2)


def _tail(err: str | None, limit: int = 500) -> str:
    text = (err or "").strip()
    return f"; LibreOffice said: {text[-limit:]}" if text else ""


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill ``proc`` and everything it started: on Windows ``soffice.exe`` is a launcher
    for ``soffice.bin``, elsewhere a wrapper around oosplash and soffice.bin."""
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        with contextlib.suppress(OSError):
            proc.kill()  # the tree is gone, or taskkill failed: at least the launcher
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


def _reap(proc: subprocess.Popen[str]) -> None:
    """Wait for a killed ``proc``; one that outlives a second kill is waited for without a
    bound, as a process left unreaped would stay a zombie."""
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait()


def _run(soffice: str, profile: Path, *args: str, env: dict[str, str]) -> str:
    """Run LibreOffice to completion and return its stderr."""
    command = [
        soffice,
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--headless",
        "--norestore",
        *args,
    ]
    try:
        # Its own session: `soffice` is a wrapper around oosplash and soffice.bin, and a
        # timeout must end the whole group, not just the wrapper.
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise DocControlError(
            ErrorCode.ENGINE_UNAVAILABLE, f"LibreOffice could not be started: {exc}"
        ) from exc
    try:
        _, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)
        try:  # a process outside the group may still hold stderr open
            _, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            err = ""
            _reap(proc)
        raise DocControlError(
            ErrorCode.ENGINE_UNAVAILABLE,
            f"LibreOffice did not finish within {TIMEOUT}s" + _tail(err),
        ) from exc
    except BaseException:  # interrupted (Ctrl-C, a signal): leave nothing running
        _kill_tree(proc)
        _reap(proc)
        raise
    return err or ""


def refresh_fields(source: Path, soffice: str | None = None) -> bytes:
    """A copy of ``source`` with every index and field result recomputed by LibreOffice,
    as the bytes of a ``.docx``. ``source`` is only read.

    Raises ``ENGINE_UNAVAILABLE`` when LibreOffice is absent or produced nothing.
    """
    soffice = soffice or soffice_path()
    if soffice is None:
        raise DocControlError(ErrorCode.ENGINE_UNAVAILABLE, "LibreOffice (soffice) was not found")
    with _work_dir(soffice) as work:
        shutil.copyfile(source, work / "in.docx")
        profile = work / "profile"
        env = {**os.environ, WORK_ENV: str(work)}
        # The first start writes the profile; the macro library goes in only after that,
        # or LibreOffice overwrites it.
        _run(soffice, profile, "--terminate_after_init", env=env)
        basic = profile / "user" / "basic"
        (basic / "Standard").mkdir(parents=True, exist_ok=True)
        (basic / "Standard" / "Module1.xba").write_text(_MODULE, encoding="utf-8")
        (basic / "Standard" / "script.xlb").write_text(_LIBRARY, encoding="utf-8")
        (basic / "Standard" / "dialog.xlb").write_text(_DIALOGS, encoding="utf-8")
        for kind in ("script", "dialog"):
            (basic / f"{kind}.xlc").write_text(_LIBRARIES.format(kind=kind), encoding="utf-8")
        err = _run(soffice, profile, "macro:///Standard.Module1.Refresh", env=env)
        out = work / "out.docx"
        if not out.is_file():
            raise DocControlError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "LibreOffice opened no result: it could not load the document or run its macro"
                + _tail(err),
            )
        return out.read_bytes()
