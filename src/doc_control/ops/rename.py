"""``term.rename``: rename a term everywhere, as one tracked batch of ``text.replace`` edits.

The body text is searched with the same matcher as ``find`` (accent and case folding,
whole word), every match becomes one ``text.replace`` and all of them go through
:func:`doc_control.ops.tracked.apply_edits` in a single verified write. Matches that
span several runs are replaced like any other range. ``dry_run`` plans everything and
writes nothing; the result carries counts and a sample of the changes.

Options
    ``whole_word`` (default true): no letter, digit or mark on either side.
    ``case_sensitive`` (default false): when true only the exact spelling matches and
    ``to`` is used as written.
    ``preserve_case`` (default true, ignored when case sensitive): a match written in
    capitals gets ``to`` in capitals (``LUCÍA`` becomes ``ELENA``), one in lower case gets
    it in lower case, one with a capital first letter gets ``to`` with a capital first
    letter, the exact spelling of ``from`` gets ``to`` as written, any other mix gets
    ``to`` as written.
    ``ignore_accents`` (default false): ``Lucia`` also matches ``Lucía``.
    ``inflections``: suffixes that follow the term. A string ``"s"`` renames ``Lucías``
    to ``Elenas``; an object ``{"from": "o", "to": "a"}`` renames ``Lucíao`` to
    ``Elenaa``. Each form is matched as a whole word too. Where forms overlap, the
    longest wins.

Deferred: a nested ``{"op": "term.rename"}`` inside ``batch`` (it needs the document to
expand); ``term.rename`` is its own write operation for now.

Not renamed, never corrupted
    Hidden text (runs with ``w:vanish``) and field instructions are not part of the
    visible text, so they are not searched and stay as they are. A match with a character
    in a field result, a hyperlink, content control or other structure the editor does not
    cut through is skipped and counted (``skipped_unsupported``), as is a match that would
    split a character from its combining marks (``skipped_boundary``). Everything else in
    the paragraph is still renamed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.index import DocumentIndex
from doc_control.model.search import Matcher, SearchOptions
from doc_control.ooxml.revisions import (
    DEFAULT_AUTHOR,
    ParagraphEditor,
    RevisionContext,
    format_date,
    used_ids,
)
from doc_control.ooxml.zipio import Package
from doc_control.ops.tracked import ReplaceText, TrackedResult, apply_edits, boundary_ok
from doc_control.registry import operation

DEFAULT_PREVIEW = 20
MAX_PREVIEW = 200
MAX_SUFFIX = 50
CONTEXT = 30

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
        "from": {"type": "string", "minLength": 1, "description": "Term to rename."},
        "to": {"type": "string", "minLength": 1, "description": "New term."},
        "whole_word": {"type": "boolean", "default": True, "description": "Whole words only."},
        "case_sensitive": {
            "type": "boolean",
            "default": False,
            "description": "Exact spelling only; `to` is used as written.",
        },
        "preserve_case": {
            "type": "boolean",
            "default": True,
            "description": "LUCÍA becomes ELENA, lucía becomes elena, Lucía becomes Elena.",
        },
        "ignore_accents": {
            "type": "boolean",
            "default": False,
            "description": "`Lucia` also matches `Lucía`.",
        },
        "inflections": {
            "type": "array",
            "description": (
                'Suffixes that follow the term: "s" renames Lucías to Elenas; '
                '{"from": "o", "to": "a"} renames Lucíao to Elenaa.'
            ),
            "items": {"type": ["string", "object"]},
        },
        "expect": {
            "type": "object",
            "properties": {"sha256": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        },
        "dry_run": {"type": "boolean", "description": "Plan and preview only; write nothing."},
        "preview_limit": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_PREVIEW,
            "default": DEFAULT_PREVIEW,
            "description": "Most changes echoed back in `preview`.",
        },
        "author": {"type": "string", "minLength": 1, "description": "Revision author."},
        "tag": {"type": "string", "minLength": 1, "description": "Appended to the author."},
    },
    "required": ["doc", "from", "to"],
    "additionalProperties": False,
}


@dataclass
class Counts:
    matches: int = 0
    replaced: int = 0
    unchanged: int = 0
    paragraphs: int = 0
    skipped_unsupported: int = 0
    skipped_boundary: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(vars(self))


@dataclass
class _Change:
    at: str
    paragraph: int
    start: int
    end: int
    before: str
    after: str
    context: str


@dataclass
class RenamePlan:
    edits: list[ReplaceText] = field(default_factory=list)
    changes: list[_Change] = field(default_factory=list)
    counts: Counts = field(default_factory=Counts)


def _bad(i: int, why: str) -> DocControlError:
    return DocControlError(
        ErrorCode.SCHEMA_INVALID, f"inflections[{i}]: {why}", path="$.inflections"
    )


def _forms(inflections: list[Any]) -> list[tuple[str, str]]:
    """``(suffix on the old term, suffix on the new term)``, the bare term first."""
    forms = [("", "")]
    for i, item in enumerate(inflections):
        if isinstance(item, str):
            old = new = item
        elif isinstance(item, dict) and set(item) <= {"from", "to"} and "from" in item:
            raw_old, raw_new = item["from"], item.get("to", item["from"])
            if not isinstance(raw_old, str) or not isinstance(raw_new, str):
                raise _bad(i, "'from' and 'to' must be strings")
            old, new = raw_old, raw_new
        else:
            raise _bad(i, 'use a suffix string or {"from": suffix, "to": suffix}')
        if not old or len(old) > MAX_SUFFIX or len(new) > MAX_SUFFIX:
            raise _bad(i, f"a suffix must have 1 to {MAX_SUFFIX} characters")
        if (old, new) not in forms:
            forms.append((old, new))
    return forms


def _cased(c: str) -> bool:
    return c.lower() != c.upper()


def match_case(matched: str, exact: str, target: str) -> str:
    """``target`` dressed in the capitalisation of ``matched``.

    The exact spelling of the old term, and any odd mix, give ``target`` as written. All
    capitals (two letters or more) give ``target.upper()``. Lower case gives
    ``target.lower()`` unless ``target`` has a capital after its first letter (``McDonald``).
    A capital first letter followed by lower case capitalises the first letter of
    ``target``, unless ``target`` is not all lower case or title case (``iPhone``).
    """
    if matched == exact:
        return target
    letters = [c for c in matched if _cased(c)]
    if not letters:
        return target
    if all(c == c.lower() for c in letters):
        inner = [c for c in target if _cased(c)][1:]
        return target if any(c != c.lower() for c in inner) else target.lower()
    if len(letters) > 1 and all(c == c.upper() for c in letters):
        return target.upper()
    if letters[0] == letters[0].upper() and all(c == c.lower() for c in letters[1:]):
        shape = [c for c in target if _cased(c)]
        plain = all(c == c.lower() for c in shape[1:])  # all lower case or title case
        i = next((i for i, c in enumerate(target) if _cased(c)), None)
        if i is None or not plain:
            return target
        return target[:i] + target[i].upper() + target[i + 1 :]
    return target


def plan_rename(
    index: DocumentIndex,
    old: str,
    new: str,
    *,
    whole_word: bool = True,
    case_sensitive: bool = False,
    preserve_case: bool = True,
    ignore_accents: bool = False,
    inflections: list[Any] | None = None,
) -> RenamePlan:
    """Every match of ``old`` in the body as a ``text.replace``; nothing is written."""
    matchers = [
        (
            Matcher(
                SearchOptions(
                    old + sfx_old,
                    ignore_case=not case_sensitive,
                    ignore_accents=ignore_accents,
                    whole_word=whole_word,
                )
            ),
            old + sfx_old,
            new + sfx_new,
        )
        for sfx_old, sfx_new in _forms(inflections or [])
    ]
    dress = preserve_case and not case_sensitive
    plan = RenamePlan()
    counts = plan.counts
    for para in index.paragraphs:
        text = para.text
        if not text:
            continue
        found: list[tuple[int, int, str, str]] = []
        for matcher, exact, target in matchers:
            found += [(s, e, exact, target) for s, e in matcher.spans(text)]
        if not found:
            continue
        found.sort(key=lambda m: (m[0], -m[1]))
        in_field = any(p.in_field for p in para.pieces)
        edge = 0
        counted = False
        for s, e, exact, target in found:
            if s < edge:
                continue  # inside a longer form already taken
            edge = e
            counts.matches += 1
            if not (boundary_ok(text, s) and boundary_ok(text, e)):
                counts.skipped_boundary += 1
                continue
            if in_field and any(p.in_field for p in para.pieces_in(s, e)):
                counts.skipped_unsupported += 1
                continue
            matched = text[s:e]
            after = match_case(matched, exact, target) if dress else target
            if after == matched:
                counts.unchanged += 1
                continue
            at = f"{para.address}:{s}-{e}"
            lo, hi = max(0, s - CONTEXT), min(len(text), e + CONTEXT)
            context = ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")
            plan.edits.append(ReplaceText(at, after))
            plan.changes.append(_Change(at, para.number, s, e, matched, after, context))
            counts.replaced += 1
            if not counted:
                counts.paragraphs += 1
                counted = True
    return plan


def _drop_unsupported(pkg: Package, index: DocumentIndex, plan: RenamePlan) -> RenamePlan:
    """Keep only the edits the editor can make (each tried on a throwaway copy)."""
    ctx = RevisionContext(used_ids(pkg), "check", format_date(None))
    kept = RenamePlan(counts=plan.counts)
    for edit, change in zip(plan.edits, plan.changes, strict=True):
        para = index.paragraph(change.paragraph)
        elem = index.spans.parse_paragraph(para.span_index)
        try:
            ParagraphEditor(elem, para, ctx).replace(change.start, change.end, change.after)
        except DocControlError as err:
            if err.code is not ErrorCode.UNSUPPORTED_CONTENT:
                raise
            plan.counts.skipped_unsupported += 1
            plan.counts.replaced -= 1
            continue
        kept.edits.append(edit)
        kept.changes.append(change)
    kept.counts.paragraphs = len({c.paragraph for c in kept.changes})
    return kept


@operation(
    "term.rename",
    "write",
    "Rename a term everywhere as one tracked batch: whole word, case, preserve case and "
    "inflections; dry_run previews with counts.",
    _SCHEMA,
)
def term_rename(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    old, new = request["from"], request["to"]
    pkg = Package(path)
    index = DocumentIndex.from_package(pkg)
    plan = plan_rename(
        index,
        old,
        new,
        whole_word=request.get("whole_word", True),
        case_sensitive=request.get("case_sensitive", False),
        preserve_case=request.get("preserve_case", True),
        ignore_accents=request.get("ignore_accents", False),
        inflections=request.get("inflections"),
    )
    dry_run = request.get("dry_run", False)
    expect = request.get("expect", {}).get("sha256") or pkg.sha256
    author = request.get("author", DEFAULT_AUTHOR)
    tag = request.get("tag")

    def apply(edits: list[ReplaceText]) -> TrackedResult:
        return apply_edits(
            path, edits, expect_sha256=expect, author=author, tag=tag, dry_run=dry_run
        )

    applied: TrackedResult | None = None
    if plan.edits:
        try:
            applied = apply(plan.edits)
        except DocControlError as err:
            if err.code is not ErrorCode.UNSUPPORTED_CONTENT:
                raise
            plan = _drop_unsupported(pkg, index, plan)
            if plan.edits:
                applied = apply(plan.edits)
    counts = plan.counts
    limit = request.get("preview_limit", DEFAULT_PREVIEW)
    result: dict[str, Any] = {
        "from": old,
        "to": new,
        "dry_run": dry_run,
        "counts": counts.to_dict(),
        "preview": [
            {"at": c.at, "before": c.before, "after": c.after, "context": c.context}
            for c in plan.changes[:limit]
        ],
    }
    warnings: list[str] = []
    if applied is not None:
        body = applied.to_dict()
        keys = ("address_map", "footprint", "revision_ids", "author", "date", "sha256", "backup")
        for key in keys:
            if key in body:
                result[key] = body[key]
        warnings += applied.warnings
    elif not dry_run:
        result["sha256"] = pkg.sha256
    if counts.skipped_unsupported:
        warnings.append(
            f"{counts.skipped_unsupported} match(es) inside a field, link or other structure"
            " were left unchanged"
        )
    if counts.skipped_boundary:
        warnings.append(
            f"{counts.skipped_boundary} match(es) would split a character from its marks"
            " and were left unchanged"
        )
    return Reply(result, doc=Document(str(path)).envelope_doc(), warnings=warnings)
