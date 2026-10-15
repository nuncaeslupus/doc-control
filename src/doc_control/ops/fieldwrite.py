"""``toc.insert`` and ``field.update``: the write side of fields (LAYOUT T7).

``toc.insert`` puts a table-of-contents field after a body paragraph as tracked
insertions: an optional title paragraph, a paragraph with the field's code and a
placeholder result, and a paragraph closing the field (the shape Word writes). The code is
``TOC \\o "1-3" \\h \\z \\u`` and the field is marked dirty, so ``field.list`` shows it as
stale until ``field.update`` fills it.

``field.update`` recomputes the results of the body's TOC, page, page-count,
word/character-count, sequence and reference fields (REF, PAGEREF; SEQ, which numbers
captions). LibreOffice recomputes all of these from a wrong cached result (the layout tests
check REF, PAGEREF and SEQ with deliberately wrong values). SECTIONPAGES, SECTION and
NOTEREF are not recomputed (LibreOffice keeps their cached results, so a stale value cannot
be told from a current one): they are listed under ``skipped``. INDEX is never recomputed:
LibreOffice ignores the INDEX and XE switches, lists ``Term:Sub`` entries flat and writes
its own separators, so it would overwrite a correct index with a wrong one. Every INDEX is
left byte for byte as it is and listed under ``skipped``, with a pointer to Word (F9).
A copy of the document goes through headless LibreOffice (see
:mod:`doc_control.ooxml.soffice`), which rebuilds the indexes and refreshes the fields; the
fields of that copy are paired with the original's by type and order, and only the result
runs are written back, as tracked changes: the old result runs become ``w:del`` (inside
the existing ``w:ins`` when the run was a tracked insertion), the new text is a ``w:ins``,
and the lines of a multi-line result beyond the first are new inserted paragraphs. Nothing
else changes, except that the ``w:dirty`` flag of an updated field, or of a field whose
result was already right, is cleared. Fields whose copy cannot be paired (LibreOffice turned
them into plain text, or the counts differ) are listed under ``skipped``. REF takes
LibreOffice's text, not Word's: a REF to a missing bookmark comes back as an error text, and
a ``REF \\n`` to an unnumbered paragraph comes back empty; neither is written, the field
keeps its result and is listed under ``skipped``. Without LibreOffice the operation refuses
with ``ENGINE_UNAVAILABLE`` naming the stale fields; ``set_update_on_open`` sets
``w:updateFields`` in ``word/settings.xml`` instead, so Word offers to update the fields
when it opens the file.

Both writes are one verified write each; their log entry carries ``undo_by_backup``, so undo
puts the backup back while the file is exactly what the write produced.
"""

from __future__ import annotations

import copy
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doc_control import outputs
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.index import DocumentIndex, build_index
from doc_control.model.parts import W
from doc_control.model.styles import load_styles
from doc_control.ooxml.revisions import (
    DEFAULT_AUTHOR,
    DELTEXT,
    INS,
    PPR,
    RPR,
    P,
    R,
    RevisionContext,
    T,
    format_author,
    format_date,
    has_section_break,
    inserted_content,
    new_paragraph,
    set_mark,
    text_children,
    used_ids,
)
from doc_control.ooxml.soffice import refresh_fields, soffice_path
from doc_control.ooxml.splice import DocumentSpans
from doc_control.ooxml.zipio import Package
from doc_control.ops.fields import _SKIP, _type_of, collect_rows
from doc_control.ops.tracked import check_name, check_text, log_changes, story_of
from doc_control.registry import operation
from doc_control.safety.history import record
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, Footprint, write_verified

SETTINGS_PART = "word/settings.xml"
PLACEHOLDER = "Update the field to build the table of contents."
#: Field types ``field.update`` recomputes (body story). Anything else keeps its result.
UPDATABLE = frozenset(
    {
        "TOC",
        "PAGEREF",
        "REF",
        "SEQ",
        "PAGE",
        "NUMPAGES",
        "NUMWORDS",
        "NUMCHARS",
    }
)
#: Types LibreOffice does not evaluate: it hands back the cached result, so the operation
#: cannot tell a current value from a stale one. They are reported under ``skipped``.
NOT_RECOMPUTED = frozenset({"SECTIONPAGES", "SECTION", "NOTEREF"})
#: Types the operation leaves alone because LibreOffice's result would be wrong, with why.
LEFT_ALONE = {
    "INDEX": "INDEX is not recomputed: LibreOffice ignores the index switches, lists "
    "sub-entries flat and changes the separators, so the index is left as it is; rebuild "
    "it in Word (select it and press F9)",
}
#: LibreOffice's text for a REF it cannot resolve (a missing bookmark), as it writes it to a
#: .docx. Word would show its own error, or the number LibreOffice leaves empty, so such a
#: result is not written: the field keeps the result it has and is listed under ``skipped``.
_REF_ERROR = re.compile(r"^\s*error\b", re.IGNORECASE)
_LEVELS = re.compile(r"^([1-9])(?:-([1-9]))?$")
_FLD_TYPE = W + "fldCharType"
_DIRTY = W + "dirty"
_TAB = W + "tab"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_DEFAULT_TEXT_WIDTH = 9350
_MAX_NAMED = 8
#: CT_Settings children that come after ``w:updateFields``, in schema order.
_AFTER_UPDATE_FIELDS = (
    "hdrShapeDefaults|footnotePr|endnotePr|compat|docVars|rsids|mathPr|attachedSchema|"
    "themeFontLang|clrSchemeMapping|doNotIncludeSubdocsInStats|doNotAutoCompressPictures|"
    "forceUpgrade|captions|readModeInkLockDown|smartTagType|schemaLibrary|shapeDefaults|"
    "doNotEmbedSmartTags|decimalSymbol|listSeparator"
)


def _el(tag: str, attrs: Mapping[str, str] | None = None, *children: ET.Element) -> ET.Element:
    elem = ET.Element(W + tag, dict(attrs or {}))
    elem.extend(children)
    return elem


def _field_char(kind: str, dirty: bool = False) -> ET.Element:
    attrs = {_FLD_TYPE: kind, **({_DIRTY: "true"} if dirty else {})}
    return _el("r", None, _el("fldChar", attrs))


def _code_run(code: str) -> ET.Element:
    instr = _el("instrText")
    instr.set(_XML_SPACE, "preserve")
    instr.text = code
    return _el("r", None, instr)


def _text_width(data: bytes) -> int:
    """Width of the text area in twips from the body's last section, for the page-number tab."""
    try:
        sect = ET.fromstring(data).find(f"{W}body/{W}sectPr")
    except ET.ParseError:
        return _DEFAULT_TEXT_WIDTH
    if sect is None:
        return _DEFAULT_TEXT_WIDTH
    size, margin = sect.find(W + "pgSz"), sect.find(W + "pgMar")
    try:
        assert size is not None and margin is not None
        width = (
            int(size.get(W + "w", ""))
            - int(margin.get(W + "left", ""))
            - int(margin.get(W + "right", ""))
        )
    except (AssertionError, ValueError):
        return _DEFAULT_TEXT_WIDTH
    return width if width > 0 else _DEFAULT_TEXT_WIDTH


def _style_or_tabs(ppr: ET.Element, style: str | None, known: frozenset[str], width: int) -> None:
    """Give a new paragraph its style when the document has it, else a right tab with dots."""
    if style is not None and style in known:
        ppr.insert(0, _el("pStyle", {W + "val": style}))
        return
    tabs = _el(
        "tabs", None, _el("tab", {W + "val": "right", W + "leader": "dot", W + "pos": str(width)})
    )
    ppr.insert(max(len(ppr) - 1, 0), tabs)  # before the mark's rPr


def _ins(ctx: RevisionContext, runs: list[ET.Element]) -> ET.Element:
    ins = inserted_content(ctx, runs=runs)
    assert ins is not None  # runs is never empty here
    return ins


def _refuse_file(doc_path: Path) -> None:
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )


def _commit(
    doc_path: Path,
    pkg: Package,
    replacements: Mapping[str, bytes],
    footprint: Footprint,
    *,
    changes: list[dict[str, Any]],
    request: dict[str, Any],
    author: str,
    tag: str | None,
    now: datetime,
    env: Mapping[str, str] | None,
    extra: Mapping[str, Any],
    result: dict[str, Any],
    warnings: list[str],
) -> None:
    """Write, log and fill ``result`` with the new hash, backup and op id."""
    written = write_verified(doc_path, replacements, footprint, pkg.sha256, now=now, env=env)
    try:
        result["op_id"] = record(
            doc_path,
            "batch",
            before_sha256=written.before_sha256,
            after_sha256=written.after_sha256,
            backup=written.backup,
            changes=changes,
            request=[request],
            author=author,
            tag=tag,
            footprint=result["footprint"],
            extra={"undo_by_backup": True, **extra},
            now=now,
            env=env,
        )
    except (DocControlError, OSError) as exc:
        result["op_id"] = None
        warnings.append(f"HISTORY_NOT_RECORDED: the write succeeded but could not be logged: {exc}")
    result["sha256"] = written.after_sha256
    result["backup"] = str(written.backup)


# --- toc.insert ---------------------------------------------------------------------------


def insert_field_block(
    doc_path: Path,
    after: str,
    *,
    op: str,
    code: str,
    title: str,
    title_style: str,
    entry_style: str,
    placeholder: str,
    request: dict[str, Any],
    expect_sha256: str | None,
    author: str,
    tag: str | None,
    now: datetime | None,
    dry_run: bool,
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Insert, after the body paragraph ``after``, a field that builds a list (TOC or INDEX):
    an optional title paragraph, a paragraph with the field's code and a placeholder
    result, and a paragraph closing the field, all tracked insertions."""
    check_name(author, "author")
    if tag is not None:
        check_name(tag, "tag")
    check_text(title, "title")
    if "\n" in title:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "the title must be one line")
    if story_of(after):
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            f"{op} works at a body address: it cannot go in a note, header or comment",
            address=after,
        )
    _refuse_file(doc_path)
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    now = now or datetime.now(UTC)
    index = DocumentIndex.from_package(pkg)
    anchor = index.resolve_address(after)
    spans = index.spans
    if spans.paragraphs[anchor.span_index].nested:
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            f"{op} cannot go after a paragraph inside a text box",
            address=after,
        )

    styles = frozenset(s.id for s in load_styles(pkg).styles)
    width = _text_width(pkg.read(DOCUMENT_PART))
    full_author = format_author(author, tag)
    ctx = RevisionContext(used_ids(pkg), full_author, format_date(now))

    new: list[ET.Element] = []
    if title:
        para, _ = new_paragraph(None, ctx, inserted_mark=True)
        ppr = para.find(PPR)
        assert ppr is not None
        run = _el("r", None, *text_children(title))
        if title_style in styles:
            ppr.insert(0, _el("pStyle", {W + "val": title_style}))
        else:
            run.insert(0, _el("rPr", None, _el("b")))
        para.append(_ins(ctx, [run]))
        new.append(para)
    body, _ = new_paragraph(None, ctx, inserted_mark=True)
    body_ppr = body.find(PPR)
    assert body_ppr is not None
    _style_or_tabs(body_ppr, entry_style, styles, width)
    opening = [_field_char("begin", dirty=True), _code_run(code), _field_char("separate")]
    holder = _el("r", None, *text_children(placeholder))
    for runs in (opening, [holder]):
        body.append(_ins(ctx, runs))
    new.append(body)
    closing, _ = new_paragraph(None, ctx, inserted_mark=True)
    closing.append(_ins(ctx, [_field_char("end")]))
    new.append(closing)

    added = [spans.serialize(para, anchor.span_index) for para in new]
    data = spans.splice(insert_after={anchor.span_index: added})
    footprint = Footprint(inserted_after=frozenset({anchor.span_index}))
    field_number = anchor.number + (2 if title else 1)
    field_at = str(build_index(data).paragraph(field_number).address)
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "at": field_at,
        "after": str(anchor.address),
        "code": code.strip(),
        "title": title or None,
        "revision_ids": ctx.revision_ids,
        "author": full_author,
        "date": format_date(now),
        "footprint": {DOCUMENT_PART: [str(anchor.address)]},
    }
    if dry_run:
        return result
    warnings: list[str] = []
    changes = log_changes(spans, index, {}, {anchor.span_index: added}, {})
    _commit(
        doc_path,
        pkg,
        {DOCUMENT_PART: data},
        footprint,
        changes=changes,
        request={"op": op, "after": after, **request},
        author=full_author,
        tag=tag,
        now=now,
        env=env,
        extra={op.split(".")[0]: field_at},
        result=result,
        warnings=warnings,
    )
    if warnings:
        result["warnings"] = warnings
    return result


def insert_toc(
    doc_path: Path,
    after: str,
    *,
    levels: str = "1-3",
    title: str = "Contents",
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Insert a tracked table-of-contents field after the body paragraph ``after``."""
    match = _LEVELS.match(levels)
    if match is None or (match[2] is not None and int(match[2]) < int(match[1])):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f'levels must be like "1-3" or "2", not {levels!r}'
        )
    low, high = match[1], match[2] or match[1]
    return insert_field_block(
        doc_path,
        after,
        op="toc.insert",
        code=f' TOC \\o "{low}-{high}" \\h \\z \\u ',
        title=title,
        title_style="TOCHeading",
        entry_style="TOC1",
        placeholder=PLACEHOLDER,
        request={"levels": levels, "title": title},
        expect_sha256=expect_sha256,
        author=author,
        tag=tag,
        now=now,
        dry_run=dry_run,
        env=env,
    )


_STR: dict[str, Any] = {"type": "string", "minLength": 1}
_EXPECT: dict[str, Any] = {
    "type": "object",
    "description": (
        "Write guard: the sha256 read earlier (doc.info, or a reply's doc.sha256), "
        'e.g. {"sha256": "9f86d081…"}; a file changed since is DOC_CHANGED_ON_DISK.'
    ),
    "properties": {"sha256": _STR},
    "additionalProperties": False,
}


_TOC_OUTPUT = outputs.result(
    ("dry_run", "at", "after", "code", "title", "revision_ids"),
    at=outputs.string("Address of the field's paragraph."),
    after=outputs.string("Address of the paragraph it went after."),
    code=outputs.string("The field code."),
    title=outputs.nullable("string", "The title paragraph's text, or null for none."),
    **outputs.FIELD_WRITE_FIELDS,
)


@operation(
    "toc.insert",
    "write",
    (
        "Insert a table of contents (a TOC field) after a body paragraph `after` (p1#a1b2c3d4), as "
        'tracked insertions: a title (default "Contents", "" for none), the field listing heading '
        'levels `levels` (default "1-3") with a placeholder result, and a closing paragraph. Run '
        "field.update next to fill in the entries and page numbers (until then field.list shows it "
        "stale). Backed up and undoable (history.undo); dry_run previews. Returns the field "
        "paragraph's address."
    ),
    {
        "type": "object",
        "properties": {
            "doc": {**_STR, "description": "Path to the .docx."},
            "after": {**_STR, "description": "Address of the body paragraph it goes after."},
            "levels": {
                "type": "string",
                "description": 'Heading levels it lists, e.g. "1-3" (default) or "2".',
            },
            "title": {
                "type": "string",
                "description": 'Title paragraph above it (default "Contents"); "" for none.',
            },
            "expect": _EXPECT,
            "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
            "author": {**_STR, "description": "Revision author."},
            "tag": {**_STR, "description": "Appended to the author."},
        },
        "required": ["doc", "after"],
        "additionalProperties": False,
    },
    output=_TOC_OUTPUT,
)
def toc_insert(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    result = insert_toc(
        path,
        request["after"],
        levels=request.get("levels", "1-3"),
        title=request.get("title", "Contents"),
        expect_sha256=request.get("expect", {}).get("sha256"),
        author=request.get("author", DEFAULT_AUTHOR),
        tag=request.get("tag"),
        dry_run=request.get("dry_run", False),
    )
    warnings = result.pop("warnings", [])
    return Reply(result, doc=Document(str(path)).envelope_doc(), warnings=warnings)


# --- reading the fields of a document body --------------------------------------------------


def _events(nodes: Iterable[ET.Element], text: bool = False) -> Iterator[tuple[str, ET.Element]]:
    """Field characters, instruction text, simple fields (and with ``text`` the visible
    text and tabs) of ``nodes`` in document order. Deleted content, text boxes and
    ``mc:Fallback`` branches are left out, as in :mod:`doc_control.ops.fields`."""
    for node in nodes:
        tag = node.tag
        if tag in _SKIP or tag == W + "txbxContent":
            continue
        if tag == W + "fldChar":
            yield "char", node
        elif tag == W + "instrText":
            yield "code", node
        elif tag == W + "fldSimple":
            yield "simple", node
        elif text and tag == T:
            yield "text", node
        elif text and tag == _TAB:
            yield "tab", node
        else:
            yield from _events(node, text)


def _flat_text(nodes: Iterable[ET.Element]) -> str:
    out: list[str] = []
    for kind, node in _events(nodes, text=True):
        if kind == "text":
            out.append(node.text or "")
        elif kind == "tab":
            out.append("\t")
        elif kind == "simple":
            out.append(_flat_text(list(node)))
    return "".join(out)


@dataclass
class _Loc:
    """An outermost field of the body, with where its marks are."""

    type: str
    code: str
    begin: tuple[int, int]  # (paragraph span index, child index of the paragraph)
    begin_char: ET.Element | None = None
    sep: tuple[int, int] | None = None
    end: tuple[int, int] | None = None
    paras: list[int] = field(default_factory=list)
    simple: ET.Element | None = None


class _Body:
    """The body's paragraphs, parsed on demand and kept so edits to them accumulate."""

    def __init__(self, spans: DocumentSpans) -> None:
        self.spans = spans
        self.elems: dict[int, ET.Element] = {}

    def elem(self, i: int) -> ET.Element:
        if i not in self.elems:
            self.elems[i] = self.spans.parse_paragraph(i)
        return self.elems[i]

    def locate(self) -> list[_Loc]:
        found: list[_Loc] = []
        cur: _Loc | None = None
        depth = 0
        for i, span in enumerate(self.spans.paragraphs):
            if span.nested:
                continue
            if depth and cur is not None:
                cur.paras.append(i)
            raw = self.spans.paragraph_bytes(i)
            if b"fldChar" not in raw and b"fldSimple" not in raw:
                continue
            for ci, child in enumerate(self.elem(i)):
                for kind, node in _events((child,)):
                    if kind == "simple":
                        if depth == 0:
                            code = node.get(W + "instr", "").strip()
                            found.append(
                                _Loc(_type_of(code), code, (i, ci), paras=[i], simple=node)
                            )
                    elif kind == "code":
                        if depth == 1 and cur is not None and cur.sep is None:
                            cur.code += node.text or ""
                    else:
                        what = node.get(_FLD_TYPE)
                        if what == "begin":
                            if depth == 0:
                                cur = _Loc("", "", (i, ci), begin_char=node, paras=[i])
                            depth += 1
                        elif what == "separate" and depth == 1 and cur is not None:
                            if cur.sep is None:
                                cur.sep = (i, ci)
                        elif what == "end" and depth:
                            depth -= 1
                            if depth == 0 and cur is not None:
                                cur.end = (i, ci)
                                cur.code = cur.code.strip()
                                cur.type = _type_of(cur.code)
                                found.append(cur)
                                cur = None
        return found


@dataclass
class _Fresh:
    """A field of the LibreOffice copy: its type and result lines (one per paragraph)."""

    type: str
    lines: list[str]
    styles: list[str | None]


def _body_paragraphs(node: ET.Element) -> Iterator[ET.Element]:
    for child in node:
        if child.tag == P:
            yield child
        elif child.tag in (
            W + "sdt",
            W + "sdtContent",
            W + "tbl",
            W + "tr",
            W + "tc",
            W + "customXml",
            INS,
        ):
            yield from _body_paragraphs(child)


def _style_of(para: ET.Element) -> str | None:
    style = para.find(f"{PPR}/{W}pStyle")
    return style.get(W + "val") if style is not None else None


def _fresh_fields(docx: bytes) -> list[_Fresh]:
    with zipfile.ZipFile(io.BytesIO(docx)) as zf:
        body = ET.fromstring(zf.read(DOCUMENT_PART)).find(W + "body")
    if body is None:
        return []
    out: list[_Fresh] = []
    cur: _Fresh | None = None
    code = ""
    separated = False
    depth = 0
    for para in _body_paragraphs(body):
        style = _style_of(para)
        if depth and cur is not None and separated:
            cur.lines.append("")
            cur.styles.append(style)
        for kind, node in _events(para, text=True):
            if kind == "simple" and depth == 0:
                code = node.get(W + "instr", "").strip()
                out.append(_Fresh(_type_of(code), [_flat_text(list(node))], [style]))
            elif kind == "code" and depth == 1 and not separated:
                code += node.text or ""
            elif kind in ("text", "tab", "simple") and cur is not None and separated:
                piece = (
                    node.text or ""
                    if kind == "text"
                    else "\t"
                    if kind == "tab"
                    else _flat_text(list(node))
                )
                cur.lines[-1] += piece
            elif kind == "char":
                what = node.get(_FLD_TYPE)
                if what == "begin":
                    if depth == 0:
                        cur, code, separated = _Fresh("", [], []), "", False
                    depth += 1
                elif what == "separate" and depth == 1 and cur is not None and not separated:
                    separated = True
                    cur.lines, cur.styles = [""], [style]
                elif what == "end" and depth:
                    depth -= 1
                    if depth == 0 and cur is not None:
                        cur.type = _type_of(code.strip())
                        while cur.lines and not cur.lines[-1]:
                            cur.lines.pop()
                            cur.styles.pop()
                        out.append(cur)
                        cur = None
    return out


# --- tracked replacement of a result ---------------------------------------------------------


def _wrap_deleted(run: ET.Element, ctx: RevisionContext) -> ET.Element:
    for node in run.iter():
        if node.tag == T:
            node.tag = DELTEXT
        elif node.tag == W + "instrText":
            node.tag = W + "delInstrText"
    deleted = ctx.revision("del")
    deleted.append(run)
    return deleted


def _track_delete(parent: ET.Element, start: int, stop: int, ctx: RevisionContext) -> None:
    """Make the live runs ``parent[start:stop]`` tracked deletions, in place. A run in a
    tracked insertion (or a hyperlink) is wrapped inside it, so the insertion still holds
    it; deletions that are already there stay."""
    for k in range(start, stop):
        node = parent[k]
        if node.tag == R:
            parent[k] = _wrap_deleted(node, ctx)
        elif node.tag in (INS, W + "hyperlink", W + "smartTag"):
            _track_delete(node, 0, len(node), ctx)


def _first_rpr(segments: Sequence[tuple[ET.Element, int, int]]) -> ET.Element | None:
    """A copy of the formatting of the first live run of the old result."""

    def search(nodes: Iterable[ET.Element]) -> ET.Element | None:
        for node in nodes:
            if node.tag == R:
                rpr = node.find(RPR)
                if rpr is not None:
                    return rpr
            elif node.tag in (INS, W + "hyperlink", W + "smartTag"):
                found = search(node)
                if found is not None:
                    return found
        return None

    for parent, start, stop in segments:
        found = search(list(parent)[start:stop])
        if found is not None:
            clone = copy.deepcopy(found)
            for change in clone.findall(W + "rPrChange"):
                clone.remove(change)
            return clone
    return None


def _new_run(text: str, rpr: ET.Element | None) -> ET.Element:
    run = ET.Element(R)
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    run.extend(text_children(text))
    return run


def _content_start(para: ET.Element) -> int:
    return 1 if len(para) and para[0].tag == PPR else 0


def _pure_mark(child: ET.Element) -> bool:
    return not any(node.tag in (T, _TAB) for node in child.iter())


@dataclass
class _Plan:
    loc: _Loc
    segments: list[tuple[ET.Element, int, int]]
    middles: list[int]
    old: list[str]


def _plan(loc: _Loc, body: _Body) -> _Plan | str:
    """Where the result of ``loc`` is, or why it cannot be rewritten."""
    if loc.simple is not None:
        simple = loc.simple
        return _Plan(loc, [(simple, 0, len(simple))], [], [_flat_text(list(simple))])
    if loc.sep is None or loc.end is None:
        return "the field has no separator or is not closed"
    (bi, _), (si, sci), (ei, eci) = loc.begin, loc.sep, loc.end
    if si != bi:
        return "the separator is not in the paragraph that opens the field"
    first = body.elem(bi)
    last = body.elem(ei)
    if not _pure_mark(first[sci]) or not _pure_mark(last[eci]):
        return "its result is inside the same run container as a field mark"
    if bi == ei:
        segments = [(first, sci + 1, eci)] if sci != eci else []
        middles: list[int] = []
    else:
        middles = [i for i in loc.paras if bi < i < ei]
        for i in middles:
            if has_section_break(body.elem(i)):
                return "a paragraph of the result ends a section"
        segments = [(first, sci + 1, len(first))]
        segments += [
            (body.elem(i), _content_start(body.elem(i)), len(body.elem(i))) for i in middles
        ]
        segments.append((last, _content_start(last), eci))
    old = [_flat_text(list(parent)[a:z]) for parent, a, z in segments]
    return _Plan(loc, segments, middles, old)


def _trim(lines: list[str]) -> list[str]:
    out = list(lines)
    while out and not out[-1]:
        out.pop()
    return out


def _entry(
    text: str,
    style: str | None,
    rpr: ET.Element | None,
    known: frozenset[str],
    width: int,
    ctx: RevisionContext,
) -> ET.Element:
    """A new paragraph holding one more line of a result, with an inserted paragraph mark."""
    para, _ = new_paragraph(None, ctx, inserted_mark=True)
    ppr = para.find(PPR)
    assert ppr is not None
    _style_or_tabs(ppr, style, known, width)
    ins = inserted_content(ctx, runs=[_new_run(text, rpr)]) if text else None
    if ins is not None:
        para.append(ins)
    return para


def _rewrite(
    plan: _Plan,
    fresh: _Fresh,
    body: _Body,
    ctx: RevisionContext,
    known: frozenset[str],
    width: int,
) -> tuple[set[int], dict[int, list[ET.Element]]]:
    """Replace the old result runs of ``plan`` by ``fresh`` as tracked changes; returns the
    paragraphs rewritten and the paragraphs to insert after one."""
    loc = plan.loc
    rpr = _first_rpr(plan.segments)
    touched: set[int] = {loc.begin[0]}
    added: dict[int, list[ET.Element]] = {}
    for parent, start, stop in plan.segments:
        _track_delete(parent, start, stop, ctx)
    for i in plan.middles:  # after the runs: this may add a w:pPr, which shifts the indexes
        set_mark(body.elem(i), "del", ctx)
        touched.add(i)
    if loc.end is not None:
        touched.add(loc.end[0])
    lines = fresh.lines
    if lines and lines[0]:
        ins = inserted_content(ctx, runs=[_new_run(lines[0], rpr)])
        assert ins is not None
        if loc.simple is not None:
            loc.simple.append(ins)
        else:
            assert loc.end is not None
            first = body.elem(loc.begin[0])
            if loc.end[0] == loc.begin[0]:
                first.insert(loc.end[1], ins)
            else:
                first.append(ins)
    if len(lines) > 1:
        added[loc.begin[0]] = [
            _entry(text, fresh.styles[k], rpr, known, width, ctx)
            for k, text in enumerate(lines)
            if k > 0
        ]
    if loc.begin_char is not None:
        loc.begin_char.attrib.pop(_DIRTY, None)
    return touched, added


# --- field.update ----------------------------------------------------------------------------


def _set_update_on_open(
    doc_path: Path,
    *,
    expect_sha256: str | None,
    author: str,
    tag: str | None,
    now: datetime,
    dry_run: bool,
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    _refuse_file(doc_path)
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    if SETTINGS_PART not in pkg:
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT,
            "the document has no settings part to set updateFields in",
        )
    text = pkg.read(SETTINGS_PART).decode("utf-8")
    prefix_match = re.search(r'xmlns:(\w+)="' + re.escape(W[1:-1]) + '"', text)
    root = re.search(r"<(\w+):settings[\s>]", text)
    if prefix_match is None or root is None or text.rstrip().endswith("/>"):
        raise DocControlError(
            ErrorCode.UNSUPPORTED_CONTENT, "word/settings.xml has an unexpected shape"
        )
    w = root.group(1)
    existing = re.search(rf"<{w}:updateFields\b[^>]*?(?:/>|>\s*</{w}:updateFields>)", text)
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "set_update_on_open": True,
        "updated": [],
        "skipped": [],
        "footprint": {SETTINGS_PART: ["updateFields"]},
    }
    if existing is not None:
        on = not re.search(r'val="(0|false|off)"', existing.group(0))
        if on:
            result["changed"] = False
            return result
        element = f'<{w}:updateFields {w}:val="true"/>'
        new = text[: existing.start()] + element + text[existing.end() :]
    else:
        after = re.search(rf"<{w}:(?:{_AFTER_UPDATE_FIELDS})\b", text)
        at = after.start() if after else text.rindex(f"</{w}:settings>")
        element = f'<{w}:updateFields {w}:val="true"/>'
        new = text[:at] + element + text[at:]
    result["changed"] = True
    if dry_run:
        return result
    full_author = format_author(author, tag)
    warnings: list[str] = []
    _commit(
        doc_path,
        pkg,
        {SETTINGS_PART: new.encode("utf-8")},
        Footprint(parts=frozenset({SETTINGS_PART})),
        changes=[
            {
                "kind": "settings",
                "part": SETTINGS_PART,
                "index": 0,
                "before": existing.group(0) if existing is not None else "",
                "after": element,
            }
        ],
        request={"op": "field.update", "set_update_on_open": True},
        author=full_author,
        tag=tag,
        now=now,
        env=env,
        extra={},
        result=result,
        warnings=warnings,
    )
    if warnings:
        result["warnings"] = warnings
    return result


def _name_fields(rows: Sequence[Mapping[str, Any]]) -> str:
    named = [f"{r['at']} ({r['type'] or 'field'})" for r in rows[:_MAX_NAMED]]
    more = len(rows) - len(named)
    return ", ".join(named) + (f" and {more} more" if more > 0 else "")


def update_fields(
    doc_path: Path,
    *,
    set_update_on_open: bool = False,
    expect_sha256: str | None = None,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Recompute the field results of the body through LibreOffice; see the module doc."""
    check_name(author, "author")
    if tag is not None:
        check_name(tag, "tag")
    now = now or datetime.now(UTC)
    if set_update_on_open:
        return _set_update_on_open(
            doc_path,
            expect_sha256=expect_sha256,
            author=author,
            tag=tag,
            now=now,
            dry_run=dry_run,
            env=env,
        )
    _refuse_file(doc_path)
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    pkg = Package(doc_path)
    index = DocumentIndex.from_package(pkg)
    spans = index.spans
    body = _Body(spans)
    all_located = [
        loc for loc in body.locate() if loc.type in UPDATABLE | NOT_RECOMPUTED | LEFT_ALONE.keys()
    ]
    located = [loc for loc in all_located if loc.type in UPDATABLE]
    addresses = {p.span_index: str(p.address) for p in index.paragraphs}
    not_recomputed = [
        {
            "at": addresses.get(loc.begin[0], ""),
            "type": loc.type,
            "reason": LEFT_ALONE.get(
                loc.type,
                f"{loc.type} is not recomputed: LibreOffice keeps its cached result, "
                "so the stored value is left as it is",
            ),
        }
        for loc in all_located
        if loc.type not in UPDATABLE
    ]
    full_author = format_author(author, tag)
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "updated": [],
        "unchanged": 0,
        "skipped": not_recomputed,
        "revision_ids": [],
        "author": full_author,
        "date": format_date(now),
        "footprint": {DOCUMENT_PART: []},
    }
    if not located:
        return result
    soffice = soffice_path()
    if soffice is None:
        stale = [r for r in collect_rows(Document(str(doc_path)), ("body", "notes", "headers"))]
        stale = [r for r in stale if r["stale"] and r["type"] in UPDATABLE]
        raise DocControlError(
            ErrorCode.ENGINE_UNAVAILABLE,
            "LibreOffice (soffice) is not available, so field results cannot be recomputed; "
            + (
                f"stale fields: {_name_fields(stale)}"
                if stale
                else f"the {len(located)} field(s) to update are not flagged stale"
            )
            + '. Install LibreOffice, or use {"set_update_on_open": true} so Word updates '
            "them when it opens the file",
            stale=[{"at": r["at"], "type": r["type"], "code": r["code"]} for r in stale],
            path=str(doc_path),
        )
    fresh_by_type: dict[str, list[_Fresh]] = {}
    for item in _fresh_fields(refresh_fields(doc_path, soffice)):
        fresh_by_type.setdefault(item.type, []).append(item)
    known = frozenset(s.id for s in load_styles(pkg).styles)
    width = _text_width(pkg.read(DOCUMENT_PART))
    ctx = RevisionContext(used_ids(pkg), full_author, format_date(now))

    by_type: dict[str, list[_Loc]] = {}
    for loc in located:
        by_type.setdefault(loc.type, []).append(loc)
    pairs: dict[int, _Fresh] = {}
    skipped: list[dict[str, str]] = []

    def skip(loc: _Loc, why: str) -> None:
        skipped.append({"at": addresses.get(loc.begin[0], ""), "type": loc.type, "reason": why})

    for kind, locs in by_type.items():
        fresh_list = fresh_by_type.get(kind, [])
        if len(fresh_list) != len(locs):
            for loc in locs:
                skip(loc, f"LibreOffice returned {len(fresh_list)} {kind} field(s) for {len(locs)}")
            continue
        for loc, match in zip(locs, fresh_list, strict=True):
            pairs[id(loc)] = match

    replacements: dict[int, bytes] = {}
    insert_after: dict[int, list[bytes]] = {}
    touched: set[int] = set()
    inserted: dict[int, list[ET.Element]] = {}
    unchanged = 0
    updated: list[dict[str, Any]] = []
    cleared: list[str] = []
    for loc in reversed(located):  # later fields first: edits never move an earlier mark
        fresh = pairs.get(id(loc))
        if fresh is None:
            continue
        plan = _plan(loc, body)
        if isinstance(plan, str):
            skip(loc, plan)
            continue
        if len(fresh.lines) > 1 and (
            loc.simple is not None or loc.begin[0] == (loc.end or loc.begin)[0]
        ):
            skip(loc, "the new result has several lines but the field sits in one paragraph")
            continue
        if loc.begin[0] in inserted:
            skip(loc, "another field already adds paragraphs after the same paragraph")
            continue
        at = addresses.get(loc.begin[0], "")
        before = _trim(plan.old)
        after = _trim(fresh.lines)
        if loc.type == "REF" and (not "".join(after).strip() or _REF_ERROR.match(after[0])):
            skip(
                loc,
                "LibreOffice gave this REF "
                + ("an error" if "".join(after).strip() else "an empty result")
                + " (a missing bookmark, or a number it does not compute), so its result "
                "is left as it is; update it in Word (F9)",
            )
            continue
        if before == after:
            unchanged += 1
            if loc.begin_char is not None and _DIRTY in loc.begin_char.attrib:
                del loc.begin_char.attrib[_DIRTY]
                touched.add(loc.begin[0])
                cleared.append(at)
            continue
        paras, added = _rewrite(plan, fresh, body, ctx, known, width)
        touched |= paras
        inserted.update(added)
        updated.append(
            {"at": at, "type": loc.type, "code": loc.code, "before": before, "after": after}
        )
    updated.reverse()
    skipped.reverse()
    skipped = not_recomputed + skipped
    for i in touched:
        replacements[i] = spans.serialize(body.elem(i), i)
    for i, paragraphs in inserted.items():
        insert_after[i] = [spans.serialize(p, i) for p in paragraphs]
    result.update(
        updated=updated,
        unchanged=unchanged,
        skipped=skipped,
        revision_ids=ctx.revision_ids,
        footprint={DOCUMENT_PART: [e["at"] for e in updated] + cleared},
        written=False,
    )
    if not touched:
        return result
    data = spans.splice(replacements, insert_after=insert_after)
    if dry_run:
        return result
    footprint = Footprint(replaced=frozenset(replacements), inserted_after=frozenset(insert_after))
    changes = log_changes(spans, index, replacements, insert_after, {})
    warnings: list[str] = []
    _commit(
        doc_path,
        pkg,
        {DOCUMENT_PART: data},
        footprint,
        changes=changes,
        request={"op": "field.update"},
        author=full_author,
        tag=tag,
        now=now,
        env=env,
        extra={},
        result=result,
        warnings=warnings,
    )
    result["written"] = True
    if warnings:
        result["warnings"] = warnings
    return result


_UPDATE_OUTPUT = outputs.result(
    ("dry_run", "updated", "skipped", "footprint"),
    dry_run=outputs.DRY_RUN,
    updated=outputs.arr(
        "Fields whose result changed.",
        outputs.obj(
            "An updated field.",
            at=outputs.ADDRESS,
            type=outputs.string("Field type (TOC, PAGE, REF...)."),
            code=outputs.string("Field code."),
            before=outputs.strings("Result paragraphs before."),
            after=outputs.strings("Result paragraphs after."),
        ),
    ),
    unchanged=outputs.integer("Fields whose result was already right."),
    skipped=outputs.arr(
        "Fields left as they are, with why: SECTIONPAGES, SECTION and NOTEREF (LibreOffice "
        "keeps their cached results), every INDEX (rebuild it in Word, F9), a REF that "
        "resolves to an error or to nothing, and fields not paired with LibreOffice's.",
        outputs.obj(
            "A skipped field.",
            at=outputs.string("Its address."),
            type=outputs.string("Field type."),
            reason=outputs.string("Why it was not recomputed."),
        ),
    ),
    written=outputs.boolean("Whether the document was written."),
    set_update_on_open=outputs.boolean("True when w:updateFields was set instead."),
    changed=outputs.boolean("set_update_on_open: whether the setting changed."),
    revision_ids=outputs.REVISION_IDS,
    author=outputs.AUTHOR,
    date=outputs.DATE,
    footprint=outputs.FOOTPRINT,
    **outputs.COMMIT,
)


@operation(
    "field.update",
    "write",
    (
        "Recompute the body's TOC, PAGE, NUMPAGES, SEQ and REF/PAGEREF results with headless "
        "LibreOffice and write only those result runs, as tracked changes (old result struck, new "
        "inserted). Needs LibreOffice (soffice): without it, ENGINE_UNAVAILABLE names the stale "
        "fields; set_update_on_open instead sets w:updateFields so Word updates them on opening. "
        "Citations are never changed; INDEX, SECTIONPAGES, SECTION, NOTEREF and unresolvable REFs "
        "are left and listed under `skipped`. Run after toc.insert, caption.add or xref.add. "
        "Backed up and undoable; dry_run previews; slow (seconds)."
    ),
    {
        "type": "object",
        "properties": {
            "doc": {**_STR, "description": "Path to the .docx."},
            "set_update_on_open": {
                "type": "boolean",
                "description": "Only set w:updateFields in settings.xml (no LibreOffice).",
            },
            "expect": _EXPECT,
            "dry_run": {"type": "boolean", "description": "Preview only; write nothing."},
            "author": {**_STR, "description": "Revision author."},
            "tag": {**_STR, "description": "Appended to the author."},
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
    output=_UPDATE_OUTPUT,
)
def fields_update(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    result = update_fields(
        path,
        set_update_on_open=request.get("set_update_on_open", False),
        expect_sha256=request.get("expect", {}).get("sha256"),
        author=request.get("author", DEFAULT_AUTHOR),
        tag=request.get("tag"),
        dry_run=request.get("dry_run", False),
    )
    warnings = result.pop("warnings", [])
    return Reply(result, doc=Document(str(path)).envelope_doc(), warnings=warnings)
