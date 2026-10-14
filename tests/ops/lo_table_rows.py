"""Drive LibreOffice (through UNO) over a document with tracked table rows.

Run by ``test_table_rows.py`` with a Python that can ``import uno`` (LibreOffice's own, or
the system's ``python3-uno``): ``python lo_table_rows.py DOCX PROFILE_DIR``. Prints one JSON
object on stdout: a snapshot of the document (its tables as rows of cell text, and its
tracked changes) as loaded, after accept-all, after undoing it, after reject-all and after
undoing that. The LibreOffice it starts is stopped before the script ends.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import uno  # type: ignore[import-not-found]
from com.sun.star.beans import PropertyValue  # type: ignore[import-not-found]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("localhost", 0))
        return int(probe.getsockname()[1])


def _props(**values: Any) -> tuple[Any, ...]:
    out = []
    for name, value in values.items():
        prop = PropertyValue()
        prop.Name, prop.Value = name, value
        out.append(prop)
    return tuple(out)


def _connect(port: int, proc: subprocess.Popen[bytes]) -> Any:
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local
    )
    deadline = time.monotonic() + 120
    while True:
        try:
            return resolver.resolve(
                f"uno:socket,host=localhost,port={port};urp;StarOffice.ComponentContext"
            )
        except Exception:
            if proc.poll() is not None or time.monotonic() > deadline:
                raise
            time.sleep(0.3)


def _snapshot(doc: Any) -> dict[str, Any]:
    tables = []
    names = doc.getTextTables().getElementNames()
    for name in names:
        table = doc.getTextTables().getByName(name)
        rows = []
        for r in range(table.getRows().getCount()):
            cells = []
            for c in range(table.getColumns().getCount()):
                try:
                    cells.append(table.getCellByPosition(c, r).getString())
                except Exception:
                    cells.append(None)
            rows.append(cells)
        tables.append(rows)
    redlines = []
    enum = doc.getRedlines().createEnumeration()
    while enum.hasMoreElements():
        red = enum.nextElement()
        redlines.append(
            {
                "type": str(red.RedlineType),
                "author": str(red.RedlineAuthor),
                "text": str(red.getPropertyValue("RedlineText")) if False else "",
            }
        )
    return {"tables": tables, "redlines": redlines}


def main(docx: str, profile: str) -> dict[str, Any]:
    port = _free_port()
    soffice = os.environ.get("SOFFICE", "soffice")
    proc = subprocess.Popen(
        [
            soffice,
            f"-env:UserInstallation={Path(profile).resolve().as_uri()}",
            "--headless",
            "--norestore",
            "--nologo",
            f"--accept=socket,host=localhost,port={port};urp;",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ctx = _connect(port, proc)
        smgr = ctx.ServiceManager
        desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        doc = desktop.loadComponentFromURL(
            Path(docx).resolve().as_uri(), "_blank", 0, _props(Hidden=True)
        )
        frame = doc.getCurrentController().getFrame()

        def dispatch(command: str) -> None:
            dispatcher.executeDispatch(frame, command, "", 0, ())

        out: dict[str, Any] = {"loaded": _snapshot(doc)}
        dispatch(".uno:AcceptAllTrackedChanges")
        out["accepted"] = _snapshot(doc)
        dispatch(".uno:Undo")
        out["accept_undone"] = _snapshot(doc)
        dispatch(".uno:RejectAllTrackedChanges")
        out["rejected"] = _snapshot(doc)
        dispatch(".uno:Undo")
        out["reject_undone"] = _snapshot(doc)
        doc.close(True)
        return out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    json.dump(main(sys.argv[1], sys.argv[2]), sys.stdout)
