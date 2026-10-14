"""Write section and settings changes (LAYOUT T3), the library under ``layout.apply``.

:func:`apply_layout_edits` takes a list of edits, all addressed against the document as it is
now, and writes them in one verified step:

``{"kind": "section.props", "section": 2, "props": {...}}``
    Change the page setup of section ``section`` (1-based, as ``sections.list`` numbers
    them) as a tracked ``w:sectPrChange``; ``props`` are those of
    :mod:`doc_control.ooxml.sections`. Several edits of one section are merged.
``{"kind": "section.break", "after": "p3#fp", "type": "oddPage"}``
    Insert a section break after that paragraph, as a tracked inserted paragraph that ends
    the section. The section before the break keeps the setup the whole had; the section
    after it starts as ``type`` (a tracked change of the old closing ``w:sectPr``) and drops
    what would otherwise repeat at the break: a page-number start and a different first
    page. Optional ``props`` set the page setup of the section the break ends, which is part
    of the insertion and so needs no record of its own.
``{"kind": "section.unbreak", "at": "p3#fp"}``
    Remove the section break that ends that paragraph, tracked as a deleted paragraph
    mark: accepting it joins the section with the next one. Refused before a table or at the
    end of a container, where accepting could not join. The history log names the mark
    revisions of breaks and unbreaks (``break_marks``): only those may take a section break
    away when resolved; another section closer keeps its mark.
``{"kind": "settings", "even_and_odd_headers": true, "mirror_margins": false}``
    Flags of ``word/settings.xml``.

Word cannot track a header or footer reference change or a settings change. Those are
refused (``UNSUPPORTED_CONTENT``) unless ``untracked`` is true (LAYOUT D-L2); either way the
write is logged, and undo puts the backup back while the file is what the write left.

Nothing is written unless the result passes the layout footprint
(:mod:`doc_control.safety.layout`) and :func:`untracked_section_changes` finds no section
change without its record.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.ooxml.revisions import (
    DEFAULT_AUTHOR,
    PPR,
    RevisionContext,
    format_author,
    format_date,
    unsupported,
    used_ids,
)
from doc_control.ooxml.sections import (
    BREAK_TYPES,
    SECT_PR,
    change_section,
    check_props,
    effective,
    has_refs,
    mark_break_removed,
    middle_setup,
    new_break,
    section_closers,
    set_props,
)
from doc_control.ooxml.settingsflags import SETTINGS_PART, read_flag, set_flag
from doc_control.ooxml.splice import DocumentSpans, scan
from doc_control.ooxml.zipio import build_package
from doc_control.ops.tracked import next_in_container
from doc_control.safety.history import record, to_text
from doc_control.safety.layout import layout_footprint, untracked_section_changes
from doc_control.safety.lock import check_changed, check_unlocked
from doc_control.safety.verify import DOCUMENT_PART, write_verified

_SETTING_KEYS = {"even_and_odd_headers": "evenAndOddHeaders", "mirror_margins": "mirrorMargins"}


@dataclass(frozen=True)
class LayoutWriteResult:
    changes: list[dict[str, Any]]
    dry_run: bool
    before_sha256: str
    after_sha256: str | None = None
    backup: Path | None = None
    op_id: str | None = None
    revision_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    package: bytes | None = None  # a dry run's resulting file, for the caller to check


def _bad(message: str, **details: object) -> DocControlError:
    return DocControlError(ErrorCode.SCHEMA_INVALID, message, **details)


def _section_of(closers: Sequence[int | None], span_index: int) -> int:
    return next((k + 1 for k, c in enumerate(closers) if c is None or c > span_index), len(closers))


def _closing_sect(elem: ET.Element) -> ET.Element:
    sect = elem.find(f"{PPR}/{SECT_PR}")
    assert sect is not None
    return sect


def apply_layout_edits(
    doc_path: Path,
    edits: Sequence[Mapping[str, Any]],
    *,
    untracked: bool = False,
    dry_run: bool = False,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    expect_sha256: str | None = None,
    env: Mapping[str, str] | None = None,
    stage: Callable[[str], None] | None = None,
) -> LayoutWriteResult:
    """Write ``edits`` to ``doc_path`` in one verified write (see the module docstring)."""
    if not edits:
        raise _bad("no layout edits given")
    if not doc_path.is_file():
        raise DocControlError(
            ErrorCode.DOC_NOT_FOUND, f"no such file: {doc_path}", path=str(doc_path)
        )
    if not dry_run:
        check_unlocked(doc_path)
    check_changed(doc_path, expect_sha256)
    doc = Document(str(doc_path))
    pkg = doc.pkg
    spans = doc.index.spans
    closers = section_closers(spans)
    ctx = RevisionContext(used_ids(pkg), format_author(author, tag), format_date(now))

    # Plan, against the document as it is.
    props_by_section: dict[int, dict[str, Any]] = {}
    section_edit: dict[int, int] = {}  # section -> first edit that touched it
    breaks: dict[int, tuple[int, str]] = {}  # paragraph span index -> (edit, type)
    unbreaks: dict[int, int] = {}
    break_props: dict[int, Mapping[str, Any]] = {}
    flags: dict[str, bool] = {}
    for n, edit in enumerate(edits):
        kind = edit.get("kind")
        if kind == "section.props":
            number = edit.get("section")
            if isinstance(number, bool) or not isinstance(number, int):
                raise _bad(f"edit {n}: 'section' must be an integer")
            if not 1 <= number <= len(closers):
                raise _bad(
                    f"edit {n}: the document has {len(closers)} sections, not {number}",
                    sections=len(closers),
                )
            props = edit.get("props")
            if not isinstance(props, Mapping):
                raise _bad(f"edit {n}: 'props' must be an object")
            check_props(props)
            props_by_section.setdefault(number, {}).update(props)
            section_edit.setdefault(number, n)
        elif kind == "section.break":
            para = doc.index.resolve_address(str(edit.get("after", "")))
            value = edit.get("type", "nextPage")
            if value not in BREAK_TYPES:
                raise _bad(f"edit {n}: type must be one of {', '.join(BREAK_TYPES)}", got=value)
            if doc.cells[para.number - 1] != -1:
                raise unsupported(
                    "a section break cannot go inside a table", paragraph=str(para.address)
                )
            if para.span_index in closers:
                raise unsupported(
                    f"{para.address} already ends a section", paragraph=str(para.address)
                )
            if para.number == len(doc.index):
                raise unsupported(
                    "no content follows the last paragraph: a break there starts an empty section",
                    paragraph=str(para.address),
                )
            if next_in_container(doc.index, para) is None:
                raise unsupported(
                    "a tracked section break cannot go right before a table or at the end of "
                    "a container: rejecting it would leave the break behind",
                    paragraph=str(para.address),
                )
            if para.span_index in breaks:
                raise DocControlError(
                    ErrorCode.BATCH_CONFLICT, f"two breaks after {para.address}", edits=[n]
                )
            part_props = edit.get("props")
            if part_props is not None:
                if not isinstance(part_props, Mapping):
                    raise _bad(f"edit {n}: 'props' must be an object")
                check_props(part_props)
                if has_refs(part_props):
                    raise _bad(f"edit {n}: a break's props take no header or footer references")
                break_props[para.span_index] = part_props
            breaks[para.span_index] = (n, value)
        elif kind == "section.unbreak":
            para = doc.index.resolve_address(str(edit.get("at", "")))
            if para.span_index not in closers:
                raise unsupported(
                    f"{para.address} does not end a section", paragraph=str(para.address)
                )
            if next_in_container(doc.index, para) is None:
                raise unsupported(
                    "a tracked section break removal cannot be made right before a table or at "
                    "the end of a container: accepting it would leave the break behind",
                    paragraph=str(para.address),
                )
            unbreaks[para.span_index] = n
        elif kind == "settings":
            given = {k for k in edit if k != "kind"}
            if not given or given - set(_SETTING_KEYS):
                raise _bad(f"edit {n}: settings take {', '.join(_SETTING_KEYS)}")
            for key in given:
                if not isinstance(edit[key], bool):
                    raise _bad(f"edit {n}: {key} must be true or false")
                flags[_SETTING_KEYS[key]] = edit[key]
        else:
            raise _bad(f"edit {n}: unknown kind {kind!r}")

    by_section: dict[int, list[int]] = {}
    for span_index in sorted(breaks):
        # The section a break splits is closed by the first closer at or after the paragraph;
        # that closer stays and becomes the section after the last break.
        by_section.setdefault(_section_of(closers, span_index), []).append(span_index)
    for number, group in by_section.items():
        value = breaks[group[-1]][1]
        source = _source_setup(spans, closers[number - 1])
        implied: dict[str, Any] = {"break": value}
        if source["page_number_start"] is not None:
            implied["page_number_start"] = None
        if source["title_page"]:
            implied["title_page"] = False
        props_by_section[number] = {**implied, **props_by_section.get(number, {})}

    if any(has_refs(p) for p in props_by_section.values()) and not untracked:
        raise unsupported(
            "header and footer references cannot be tracked in a section property change; "
            "pass untracked: true"
        )
    if flags and not untracked:
        raise unsupported(
            "Word cannot track a settings change (even/odd headers, mirror margins); "
            "pass untracked: true"
        )

    # Build.
    changes: list[dict[str, Any]] = []
    elems: dict[int, ET.Element] = {}  # paragraph span index -> edited element
    inserted: dict[int, ET.Element] = {}
    body_sect: ET.Element | None = None

    def mark(kind: str, at: str, tracked: bool, before: int, **extra: Any) -> None:
        changes.append(
            {
                "kind": kind,
                "at": at,
                "tracked": tracked,
                "revision_ids": ctx.revision_ids[before:],
                **extra,
            }
        )

    for number in sorted(props_by_section):
        closer = closers[number - 1]
        props = props_by_section[number]
        before_ids = len(ctx.revision_ids)
        if closer is None:
            sect = body_sect = spans.parse_body_sect()
        else:
            elems[closer] = spans.parse_paragraph(closer)
            sect = _closing_sect(elems[closer])
        if change_section(sect, props, ctx, untracked=untracked, where={"section": number}):
            tracked = len(ctx.revision_ids) > before_ids
            mark(
                "section.props",
                f"section {number}",
                tracked,
                before_ids,
                section=number,
                props=dict(props),
            )
        elif closer is None:
            body_sect = None  # nothing to change there
        else:
            del elems[closer]

    for span_index in sorted(breaks):
        before_ids = len(ctx.revision_ids)
        number = _section_of(closers, span_index)
        closer = closers[number - 1]
        source_sect = (
            spans.parse_body_sect()
            if closer is None
            else _closing_sect(spans.parse_paragraph(closer))
        )
        group = by_section[number]
        if span_index != group[0]:
            # A later break in the same section: it starts as the earlier break made it, and
            # carries neither the page-number restart nor the title page of the first part.
            source_sect = middle_setup(source_sect, breaks[group[group.index(span_index) - 1]][1])
        inserted[span_index] = new_break(source_sect, ctx)
        if span_index in break_props:
            before_sect = inserted[span_index].find(f"{PPR}/{SECT_PR}")
            assert before_sect is not None
            set_props(before_sect, break_props[span_index])
        para = next(p for p in doc.index.paragraphs if p.span_index == span_index)
        mark("section.break", str(para.address), True, before_ids, type=breaks[span_index][1])

    for span_index in sorted(unbreaks):
        before_ids = len(ctx.revision_ids)
        if span_index not in elems:
            elems[span_index] = spans.parse_paragraph(span_index)
        mark_break_removed(elems[span_index], ctx)
        para = next(p for p in doc.index.paragraphs if p.span_index == span_index)
        mark("section.unbreak", str(para.address), True, before_ids)

    settings = pkg.read(SETTINGS_PART) if SETTINGS_PART in pkg else None
    new_settings = settings
    if flags:
        if settings is None:
            if any(flags.values()):
                raise unsupported(f"the document has no {SETTINGS_PART}")
        else:
            assert new_settings is not None
            for name, on in flags.items():
                before_flag = read_flag(new_settings, name)
                new_settings = set_flag(new_settings, name, on)
                if before_flag != on:
                    mark("settings", name, False, len(ctx.revision_ids), flag=name, value=on)

    if not changes:
        return LayoutWriteResult([], dry_run, pkg.sha256)

    replacements_p = {i: spans.serialize(e, i) for i, e in elems.items()}
    insert_after = {i: [spans.serialize(p, i)] for i, p in inserted.items()}
    new_doc = spans.splice(replacements_p, insert_after=insert_after)
    if body_sect is not None:
        new_doc = scan(new_doc).replace_body_sect(spans.serialize_body_sect(body_sect))
    written_parts: dict[str, bytes] = {}
    if new_doc != spans.data:
        written_parts[DOCUMENT_PART] = new_doc
    if new_settings is not None and new_settings != settings:
        written_parts[SETTINGS_PART] = new_settings

    problems = untracked_section_changes(
        spans.data, new_doc, insert_after.keys(), allow_refs=untracked
    )
    if problems:
        raise DocControlError(
            ErrorCode.INTERNAL, "self-check: " + "; ".join(problems), problems=problems
        )

    footprint = layout_footprint(
        replaced=elems,
        inserted_after=insert_after,
        body_section=body_sect is not None,
        settings=[n for n in flags if written_parts.get(SETTINGS_PART) is not None],
    )
    if dry_run:
        package = build_package(pkg, written_parts)  # the parts must at least assemble
        return LayoutWriteResult(
            changes, True, pkg.sha256, revision_ids=list(ctx.revision_ids), package=package
        )
    written = write_verified(
        doc_path, written_parts, footprint, pkg.sha256, now=now, env=env, stage=stage
    )
    warnings: list[str] = []
    op_id: str | None = None
    logged: list[dict[str, Any]] = [
        {
            "kind": "replace",
            "index": i,
            "before": to_text(spans.paragraph_bytes(i)),
            "after": to_text(replacements_p[i]),
            "text_before": next(p.text for p in doc.index.paragraphs if p.span_index == i),
            "deleted": False,
        }
        for i in sorted(replacements_p)
    ]
    # Whole-part changes are not paragraphs; they are logged so the entry can be undone.
    if body_sect is not None:
        logged.append({"kind": "part", "part": DOCUMENT_PART, "what": "body section properties"})
    if SETTINGS_PART in written_parts:
        logged.append({"kind": "part", "part": SETTINGS_PART, "flags": sorted(flags)})
    try:
        op_id = record(
            doc_path,
            "batch",
            before_sha256=written.before_sha256,
            after_sha256=written.after_sha256,
            backup=written.backup,
            changes=logged,
            request=[{"op": str(e.get("kind")), **e} for e in edits],
            author=ctx.author,
            tag=tag,
            footprint={DOCUMENT_PART: sorted(elems)},
            extra={
                "undo_by_backup": True,
                "layout": True,
                "untracked": untracked,
                # The marks revision.accept/reject may remove although they end a section.
                "break_marks": [
                    {"id": rid, "author": ctx.author, "date": ctx.date}
                    for c in changes
                    if c["kind"] in ("section.break", "section.unbreak")
                    for rid in c["revision_ids"]
                ],
            },
            now=now,
            env=env,
        )
    except (DocControlError, OSError) as exc:
        warnings.append(f"HISTORY_NOT_RECORDED: the write succeeded but could not be logged: {exc}")
    return LayoutWriteResult(
        changes,
        False,
        written.before_sha256,
        written.after_sha256,
        written.backup,
        op_id,
        list(ctx.revision_ids),
        warnings,
    )


def _source_setup(spans: DocumentSpans, closer: int | None) -> dict[str, Any]:
    if closer is None:
        return effective(spans.parse_body_sect())
    return effective(_closing_sect(spans.parsed(closer)))


__all__ = ["LayoutWriteResult", "apply_layout_edits"]
