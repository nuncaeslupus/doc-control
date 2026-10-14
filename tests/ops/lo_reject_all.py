"""Reject every tracked change of a document in headless LibreOffice and save it as .odt.

Run by a Python that can ``import uno`` (the system one, not the project's):
``python lo_reject_all.py SRC DEST PROFILE_DIR PORT``. It shares no code with ``doc_control``.
"""

# mypy: ignore-errors
from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

import uno
from com.sun.star.beans import PropertyValue


def _prop(name: str, value: object) -> PropertyValue:
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def main() -> None:
    src, dest, profile = (Path(a).resolve() for a in sys.argv[1:4])
    port = int(sys.argv[4])
    proc = subprocess.Popen(
        [
            "soffice",
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--norestore",
            f"--accept=socket,host=localhost,port={port};urp;",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local
        )
        url = f"uno:socket,host=localhost,port={port};urp;StarOffice.ComponentContext"
        for _ in range(60):
            try:
                ctx = resolver.resolve(url)
                break
            except Exception:
                time.sleep(1)
        else:
            raise SystemExit("LibreOffice did not start")
        smgr = ctx.ServiceManager
        desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        doc = desktop.loadComponentFromURL(src.as_uri(), "_blank", 0, (_prop("Hidden", True),))
        dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        frame = doc.CurrentController.Frame
        dispatcher.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())
        doc.storeToURL(dest.as_uri(), (_prop("FilterName", "writer8"),))
        doc.close(True)
        with contextlib.suppress(Exception):
            desktop.terminate()
    finally:
        proc.kill()


main()
