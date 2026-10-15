"""``note.add``: a new footnote or endnote at a body offset, as tracked insertions.

``at`` is a body address: ``p40#c0ffee00:17-17`` puts the mark after character 17, a range
puts it after the range, a bare paragraph at its end. The reference mark in the body and the
note's text (with its own mark) are each in a ``w:ins``, so rejecting the insertion takes
both out; the note keeps the next free ``w:id`` of its part. The notes part is created, with
its separator notes, content type and relationship, when the document has none.

The write adds a note to the part as raw bytes and edits the one body paragraph through
:func:`doc_control.ops.tracked.prepare_edits`, all in one verified write. The log entry
carries ``undo_by_backup``: undo puts the backup back while the file is still exactly what
this write produced (see :func:`doc_control.safety.history.undo_entry`), since a created part
cannot be taken out paragraph by paragraph.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from doc_control import outputs
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.index import DocumentIndex, Paragraph, build_index
from doc_control.model.parts import DOCUMENT_RELS, OFFICE_REL, W_NS, related_part
from doc_control.model.storyorder import _note_numbers
from doc_control.model.styles import load_styles
from doc_control.ooxml.commentparts import (
    _REL_ID,
    CONTENT_TYPES,
    _decode,
    _insert_before_close,
    _Syntax,
)
from doc_control.ooxml.revisions import (
    DEFAULT_AUTHOR,
    ParagraphEditor,
    RevisionContext,
    format_author,
    format_date,
    used_ids,
)
from doc_control.ooxml.splice import Container
from doc_control.ooxml.zipio import Package
from doc_control.ops.tracked import boundary_ok, check_name, check_text, prepare_edits, story_of
from doc_control.registry import operation
from doc_control.safety.history import record
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, write_verified

_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml."
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_SEPARATORS = (("separator", -1), ("continuationSeparator", 0))


def _fresh_part(kind: str) -> str:
    """A notes part with the two separator notes Word expects."""
    notes = "".join(
        f'<w:{kind} w:type="{name}" w:id="{ident}"><w:p><w:pPr><w:spacing w:after="0" '
        f'w:line="240" w:lineRule="auto"/></w:pPr><w:r><w:{name}/></w:r></w:p></w:{kind}>'
        for name, ident in _SEPARATORS
    )
    return f'{_XML_DECL}<w:{kind}s xmlns:w="{W_NS}" xmlns:r="{_R_NS}">{notes}</w:{kind}s>'


def _next_note_id(part: str, kind: str) -> int:
    try:
        root = ET.fromstring(part.encode("utf-8"))
    except ET.ParseError as exc:
        raise DocControlError(ErrorCode.NOT_A_DOCX, f"the {kind}s part is not well-formed") from exc
    ids = [
        int(i)
        for note in root.iter(f"{{{W_NS}}}{kind}")
        if (i := note.get(f"{{{W_NS}}}id", "")).lstrip("-").isdigit()
    ]
    return max([0, *ids]) + 1


def _note_xml(
    w: str, kind: str, ident: int, text: str, styles: frozenset[str], revision: str
) -> str:
    """One note: its mark run and text run in a single tracked insertion. ``w`` is the
    prefix of the WordprocessingML namespace in the part, ``revision`` the attributes of
    the ``w:ins``."""
    cap = kind.capitalize()
    if f"{cap}Reference" in styles:
        mark_props = f'<{w}:rPr><{w}:rStyle {w}:val="{cap}Reference"/></{w}:rPr>'
    else:
        mark_props = f'<{w}:rPr><{w}:vertAlign {w}:val="superscript"/></{w}:rPr>'
    ppr = f'<{w}:pPr><{w}:pStyle {w}:val="{cap}Text"/></{w}:pPr>' if f"{cap}Text" in styles else ""
    return (
        f'<{w}:{kind} {w}:id="{ident}"><{w}:p>{ppr}<{w}:ins {revision}>'
        f"<{w}:r>{mark_props}<{w}:{kind}Ref/></{w}:r>"
        f'<{w}:r><{w}:t xml:space="preserve"> {escape(text)}</{w}:t></{w}:r>'
        f"</{w}:ins></{w}:p></{w}:{kind}>"
    )


def _marks_follow(body: bytes, kind: str, ident: int) -> bool:
    """True if another ``kind`` mark comes after note ``ident``'s in the body."""
    marks = [m.get(f"{{{W_NS}}}id") for m in ET.fromstring(body).iter(f"{{{W_NS}}}{kind}Reference")]
    return marks.index(str(ident)) + 1 < len(marks)


def _reference_run(
    kind: str, ident: int, styles: frozenset[str], ctx: RevisionContext
) -> ET.Element:
    """The body's mark, in a new tracked insertion."""
    w = f"{{{W_NS}}}"
    ins = ctx.revision("ins")
    run = ET.SubElement(ins, w + "r")
    rpr = ET.SubElement(run, w + "rPr")
    style = f"{kind.capitalize()}Reference"
    if style in styles:
        ET.SubElement(rpr, w + "rStyle", {w + "val": style})
    else:
        ET.SubElement(rpr, w + "vertAlign", {w + "val": "superscript"})
    ET.SubElement(run, w + f"{kind}Reference", {w + "id": str(ident)})
    return ins


def _package_parts(pkg: Package, part: str, kind: str) -> dict[str, bytes]:
    """The content-type override and relationship for a notes part being created."""
    types = _decode(CONTENT_TYPES, pkg.read(CONTENT_TYPES))
    out: dict[str, bytes] = {}
    if f"/{part}" not in types:
        override = f'<Override PartName="/{part}" ContentType="{_CT}{kind}s+xml"/>'
        out[CONTENT_TYPES] = _insert_before_close(CONTENT_TYPES, types, override).encode("utf-8")
    rels = _decode(DOCUMENT_RELS, pkg.read(DOCUMENT_RELS))
    rid = max((int(n) for n in _REL_ID.findall(rels)), default=0) + 1
    rel = f'<Relationship Id="rId{rid}" Type="{OFFICE_REL}{kind}s" Target="{kind}s.xml"/>'
    out[DOCUMENT_RELS] = _insert_before_close(DOCUMENT_RELS, rels, rel).encode("utf-8")
    return out


def _point(index: DocumentIndex, at: str) -> tuple[Paragraph, int]:
    """The body paragraph and offset a mark goes at."""
    if story_of(at):
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            "note.add works at a body address: notes cannot hold notes",
            address=at,
        )
    rng = index.resolve_range(at)
    if rng.first is not rng.last or rng.sentence is not None:
        raise DocControlError(
            ErrorCode.ADDRESS_INVALID,
            f"{at!r} must be a point or a range inside one paragraph",
            address=at,
        )
    assert rng.end_offset is not None
    if not boundary_ok(rng.first.text, rng.end_offset):
        raise DocControlError(
            ErrorCode.ADDRESS_INVALID,
            f"{at!r} splits a character from its combining marks",
            address=at,
        )
    return rng.first, rng.end_offset


def add_note(
    doc_path: Path,
    at: str,
    kind: str,
    text: str,
    *,
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Add a tracked ``kind`` (``footnote`` or ``endnote``) holding ``text`` at ``at``."""
    check_name(author, "author")
    if tag is not None:
        check_name(tag, "tag")
    check_text(text, "text")
    if not text.strip() or "\n" in text:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "note text must be one non-empty line")
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    now = now or datetime.now(UTC)
    index = DocumentIndex.from_package(pkg)
    para, offset = _point(index, at)

    styles = frozenset(s.id for s in load_styles(pkg).styles)
    part = related_part(pkg, OFFICE_REL + f"{kind}s", f"word/{kind}s.xml")
    created = part is None
    part = part or f"word/{kind}s.xml"
    text_before = _decode(part, pkg.read(part)) if not created else _fresh_part(kind)
    ident = _next_note_id(text_before, kind)

    full_author, date = format_author(author, tag), format_date(now)
    taken = used_ids(pkg)
    note_rid = max(taken | {ident}, default=0) + 1  # the note's own revision id
    syntax = _Syntax(text_before)
    w, declaration = syntax.prefix("w")
    revision = (
        f'{w}:id="{note_rid}" {w}:author="{escape(full_author, {chr(34): "&quot;"})}" '
        f'{w}:date="{date}"{declaration}'
    )
    note = _note_xml(w, kind, ident, text, styles, revision)

    def annotate(elem: ET.Element, p: Paragraph, ctx: RevisionContext) -> None:
        ins = _reference_run(kind, ident, styles, ctx)
        ParagraphEditor(elem, p, ctx).place(offset, [ins])

    prepared = prepare_edits(
        pkg,
        [],
        author=author,
        tag=tag,
        now=now,
        dry_run=dry_run,
        index=index,
        annotations={para.number: annotate},
        reserved_ids=[note_rid],
    )
    new_part = _insert_before_close(part, text_before, note)
    replacements = {**prepared.written, part: new_part.encode("utf-8")}
    if created:
        replacements.update(_package_parts(pkg, part, kind))
    parts = frozenset(replacements) - {DOCUMENT_PART}
    footprint = replace(prepared.footprint, parts=parts)
    key = ("fn" if kind == "footnote" else "en") + str(ident)
    note_at = (
        build_index(replacements[part], Container(f"{kind}s", kind, str(ident)), part, key)
        .paragraphs[0]
        .address
    )
    warnings: list[str] = []
    if _marks_follow(prepared.written[DOCUMENT_PART], kind, ident):
        # ponytail: LibreOffice pairs the n-th mark with the note of the n-th smallest id, so
        # a note added before existing ones shows the wrong text there; Word reads by id.
        warnings.append(
            f"NOTE_ORDER: {kind}s after this mark have lower ids; LibreOffice may show the "
            "note texts shifted (Word does not, and renumbers when it saves)"
        )
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "note": {"id": ident, "number": None, "at": str(note_at)},
        "mark_at": str(para.address),
        "created_part": created,
        "revision_ids": sorted([*prepared.result.revision_ids, note_rid]),
        "author": full_author,
        "date": date,
        "footprint": {DOCUMENT_PART: prepared.result.footprint, part: [str(note_at)]},
    }
    if dry_run:
        return {**result, "warnings": warnings} if warnings else result
    written = write_verified(doc_path, replacements, footprint, pkg.sha256, now=now, env=env)
    try:
        result["op_id"] = record(
            doc_path,
            "batch",
            before_sha256=written.before_sha256,
            after_sha256=written.after_sha256,
            backup=written.backup,
            changes=prepared.changes,
            request=[{"op": "note.add", "at": at, "kind": kind, "text": text}],
            author=full_author,
            tag=tag,
            footprint=result["footprint"],
            extra={"undo_by_backup": True, "note": key},
            now=now,
            env=env,
        )
    except (DocControlError, OSError) as exc:
        result["op_id"] = None
        warnings.append(f"HISTORY_NOT_RECORDED: the write succeeded but could not be logged: {exc}")
    result["sha256"] = written.after_sha256
    result["backup"] = str(written.backup)
    shown = _note_numbers(Document(str(doc_path))).get(key)
    result["note"]["number"] = shown["number"] if shown else None
    if warnings:
        result["warnings"] = warnings
    return result


_STR: dict[str, Any] = {"type": "string", "minLength": 1}


_OUTPUT = outputs.result(
    ("dry_run", "note", "mark_at", "created_part", "revision_ids"),
    dry_run=outputs.DRY_RUN,
    note=outputs.obj(
        "The new note.",
        id=outputs.integer("Note id (w:id)."),
        number=outputs.nullable("string", "Displayed number (null on a dry run)."),
        at=outputs.string("Address of its paragraph, e.g. fn5/p1#9447b777."),
    ),
    mark_at=outputs.ADDRESS,
    created_part=outputs.boolean("Whether the footnotes/endnotes part was created."),
    revision_ids=outputs.REVISION_IDS,
    author=outputs.AUTHOR,
    date=outputs.DATE,
    footprint=outputs.FOOTPRINT,
    **outputs.COMMIT,
)


@operation(
    "note.add",
    "write",
    (
        "Add a footnote or endnote: the reference mark goes at a body point (p3#a1b2c3d4:10-10, "
        "after offset 10), after a range, or at the end of a paragraph (p3#a1b2c3d4); `text` is "
        "the note's one line. Mark and note are tracked insertions, so rejecting them removes the "
        "note; the notes part is created when missing. Backed up and undoable (history.undo); "
        "dry_run previews; expect.sha256 refuses a changed file. Returns the note's id, displayed "
        "number and address (fn5/p1#…)."
    ),
    {
        "type": "object",
        "properties": {
            "doc": {**_STR, "description": "Path to the .docx."},
            "at": {
                **_STR,
                "description": "Body point p3#fp:10-10 (the mark goes after the offset), a "
                "range inside one paragraph (after it) or a paragraph (at its end).",
            },
            "kind": {
                "enum": ["footnote", "endnote"],
                "description": "footnote (bottom of the page) or endnote (end of the document).",
            },
            "text": {**_STR, "description": "The note text, one line."},
            "expect": {
                "type": "object",
                "description": (
                    "Write guard: the sha256 read earlier (doc.info, or a reply's doc.sha256), "
                    'e.g. {"sha256": "9f86d081…"}; a file changed since is DOC_CHANGED_ON_DISK.'
                ),
                "properties": {"sha256": _STR},
                "additionalProperties": False,
            },
            "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
            "author": {**_STR, "description": "Revision author."},
            "tag": {**_STR, "description": "Appended to the author."},
        },
        "required": ["doc", "at", "kind", "text"],
        "additionalProperties": False,
    },
    output=_OUTPUT,
)
def note_add(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    result = add_note(
        path,
        request["at"],
        request["kind"],
        request["text"],
        expect_sha256=request.get("expect", {}).get("sha256"),
        author=request.get("author", DEFAULT_AUTHOR),
        tag=request.get("tag"),
        dry_run=request.get("dry_run", False),
    )
    warnings = result.pop("warnings", [])
    return Reply(result, doc=Document(str(path)).envelope_doc(), warnings=warnings)
