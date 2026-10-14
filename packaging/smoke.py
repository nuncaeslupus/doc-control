"""Smoke-test a doc-control build: `capabilities`, then a regex `find` (a spawned worker).

Usage: python smoke.py COMMAND... (e.g. `build-out/doc-control/doc-control.exe`).
Each call must print exactly one ok envelope. Exit 0 on success.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

FROZEN = {"frozen": True, "freezer": "pyinstaller"}
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
        'package/2006/content-types"><Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType='
        '"application/xml"/><Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://'
        'schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    ),
    "word/document.xml": (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W}><w:body><w:p><w:r>'
        "<w:t>smoke test 42</w:t></w:r></w:p></w:body></w:document>"
    ),
}


def _envelope(command: list[str], *args: str, stdin: str | None = None) -> dict[str, Any]:
    proc = subprocess.run(
        [*command, *args], input=stdin, capture_output=True, text=True, timeout=120, check=False
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0 or len(lines) != 1:
        raise SystemExit(f"{args}: exit {proc.returncode}, stdout {proc.stdout!r}, {proc.stderr}")
    envelope: dict[str, Any] = json.loads(lines[0])
    if envelope.get("ok") is not True:
        raise SystemExit(f"{args}: not ok: {envelope}")
    return envelope


def main(command: list[str]) -> int:
    caps = _envelope(command, "capabilities")
    build = caps["result"]["build"]
    print("capabilities ok:", len(caps["result"]["operations"]), "operations, build", build)
    with tempfile.TemporaryDirectory() as tmp:
        doc = Path(tmp) / "smoke.docx"
        with zipfile.ZipFile(doc, "w") as z:
            for name, xml in _PARTS.items():
                z.writestr(name, xml)
        request = json.dumps({"doc": str(doc), "query": "[0-9]+", "regex": True})
        found = _envelope(command, "find", "--json", "-", stdin=request)
    texts = [m["text"] for m in found["result"]["matches"]]
    if texts != ["42"]:
        raise SystemExit(f"regex find: expected ['42'], got {texts}")
    print("regex find ok")
    if "--frozen" in sys.argv and build != FROZEN:
        raise SystemExit(f"build is {build}, expected {FROZEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main([a for a in sys.argv[1:] if a != "--frozen"]))
