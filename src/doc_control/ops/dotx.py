"""``.dotx`` templates: ``styleset.export-dotx`` and ``styleset.import-dotx``.

Export writes a minimal, valid Word template package (template main content type, an
empty body, ``styles.xml`` holding the exported definitions, the package and document
relationships and ``docProps``) to a new file; the source document is never touched.
Import reads the styles of a ``.dotx`` back into a style set and installs it with the
``styleset.install`` semantics (``on_conflict``), as one verified write.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.model.styles import load_styles
from doc_control.ooxml.styledef import STYLES_CONTENT_TYPE, invalid
from doc_control.ooxml.zipio import Package
from doc_control.ops.styleset import (
    _DOC,
    _install,
    _validated,
    select_styleset,
    style_entry,
    write_new_file,
)
from doc_control.ops.stylewrite import StyleSession, commit_styles
from doc_control.registry import operation

TEMPLATE_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
)
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CORE_REL = _PKG_REL + "/metadata/core-properties"
_EPOCH = (1980, 1, 1, 0, 0, 0)


def _rels(rows: list[tuple[str, str, str]]) -> str:
    body = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{g}"/>' for i, t, g in rows)
    return f'{_XML}<Relationships xmlns="{_PKG_REL}">{body}</Relationships>'


def _skeleton(title: str) -> dict[str, bytes]:
    """The template package without its styles definitions."""
    types = (
        f'{_XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{TEMPLATE_CONTENT_TYPE}"/>'
        f'<Override PartName="/word/styles.xml" ContentType="{STYLES_CONTENT_TYPE}"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    )
    document = (
        f'{_XML}<w:document xmlns:w="{_W}"><w:body><w:p/>'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>'
    )
    core = (
        f'{_XML}<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{escape(title)}</dc:title><dc:creator>doc-control</dc:creator>"
        "</cp:coreProperties>"
    )
    app = (
        f'{_XML}<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        'extended-properties"><Application>doc-control</Application></Properties>'
    )
    parts = {
        "[Content_Types].xml": types,
        "_rels/.rels": _rels(
            [
                ("rId1", _OD + "officeDocument", "word/document.xml"),
                ("rId2", _CORE_REL, "docProps/core.xml"),
                ("rId3", _OD + "extended-properties", "docProps/app.xml"),
            ]
        ),
        "word/document.xml": document,
        "word/_rels/document.xml.rels": _rels([("rId1", _OD + "styles", "styles.xml")]),
        "docProps/core.xml": core,
        "docProps/app.xml": app,
    }
    return {name: text.encode("utf-8") for name, text in parts.items()}


def _zip(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in parts.items():
            info = zipfile.ZipInfo(name, _EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def build_dotx(styleset: dict[str, Any]) -> bytes:
    """A minimal Word template package holding the style set's definitions."""
    entries = _validated(styleset)
    ids = {e["id"] for e in entries}
    # A subset can leave a reference to a style that is not in the template: drop it.
    entries = [
        {k: v for k, v in e.items() if k not in ("based_on", "next", "link") or v in ids}
        for e in entries
    ]
    parts = _skeleton(str(styleset.get("styleset") or "styles"))
    parts["word/styles.xml"] = f'{_XML}<w:styles xmlns:w="{_W}"></w:styles>'.encode()
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "styles.dotx"
        scratch.write_bytes(_zip(parts))
        session = StyleSession(Package(scratch))
        _install(session, entries, "replace")
        session.check()
        parts["word/styles.xml"] = session.styles.data()
    return _zip(parts)


@operation(
    "styleset.export-dotx",
    "write",
    "Write the document's styles as a new .dotx template; never touches the document.",
    {
        "type": "object",
        "properties": {
            "doc": _DOC,
            "out": {"type": "string", "minLength": 1, "description": "New .dotx to create."},
            "name": {"type": "string", "minLength": 1, "description": "Set name."},
            "styles": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "description": "Only these styles (ids or names).",
            },
            "custom_only": {"type": "boolean", "description": "Only user-defined styles."},
        },
        "required": ["doc", "out"],
        "additionalProperties": False,
    },
)
def styleset_export_dotx(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    styleset = select_styleset(doc, request)
    data = build_dotx(styleset)
    write_new_file(Path(request["out"]), Path(request["doc"]), data)
    result = {"path": request["out"], "count": len(styleset["styles"]), "bytes": len(data)}
    return Reply(result, doc=doc.envelope_doc())


def read_dotx(path: Path) -> list[dict[str, Any]]:
    """The style entries of a .dotx (or any Word package) as style set entries."""
    return [style_entry(s) for s in load_styles(Package(path)).styles]


@operation(
    "styleset.import-dotx",
    "write",
    "Install the styles of a .dotx template into the document: new styles are added, "
    "existing ones replaced (or skipped); dry_run previews.",
    {
        "type": "object",
        "properties": {
            "doc": _DOC,
            "path": {"type": "string", "minLength": 1, "description": "The .dotx to read."},
            "on_conflict": {
                "enum": ["replace", "skip", "error"],
                "description": "A style id already in the document (default replace).",
            },
            "expect": {
                "type": "object",
                "properties": {"sha256": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
        },
        "required": ["doc", "path"],
        "additionalProperties": False,
    },
)
def styleset_import_dotx(request: dict[str, Any]) -> Reply:
    source, target = Path(request["path"]), Path(request["doc"])
    if source.exists() and target.exists() and source.samefile(target):
        raise invalid("path is the document itself")
    entries = _validated({"version": 1, "styles": read_dotx(source)})
    mode = request.get("on_conflict", "replace")
    result = commit_styles(
        target,
        lambda session: _install(session, entries, mode),
        expect_sha256=request.get("expect", {}).get("sha256"),
        dry_run=request.get("dry_run", False),
    )
    return Reply(result, doc=Document(request["doc"]).envelope_doc())
