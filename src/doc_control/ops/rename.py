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
    ``match_level`` (no default: without it nothing changes): search with ``find``'s
    levelled matcher. ``default`` also ignores case, accents and punctuation, ``loose``
    symbols too, and ``exact`` keeps the options above; Word's Find codes in ``from`` are
    read as their characters. Every change in ``preview`` (and every ``skipped`` row)
    then carries ``match_level``, the strictest level it meets, and ``matches_by_level``
    counts ``counts.matches`` by level. It never widens to a looser level on its own.

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
from doc_control.model.loosefold import LEVELS, Level
from doc_control.model.search import LevelMatcher, Matcher, SearchOptions
from doc_control.model.wordcodes import translate_codes
from doc_control.ooxml.revisions import (
    DEFAULT_AUTHOR,
    ParagraphEditor,
    RevisionContext,
    format_date,
    used_ids,
)
from doc_control.ooxml.zipio import Package
from doc_control.ops.find import check_level
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
        "match_level": {
            "type": "string",
            "enum": ["exact", "default", "loose"],
            "description": "Fold the search as `find` does: `default` ignores case, accents"
            " and punctuation (symbols such as `$` still count), `loose` drops symbols too;"
            " `exact` is the rename as without it. When passed, Word's Find codes in `from`"
            " are read as their characters, each `preview` change and `skipped` row gains"
            " `match_level` (the strictest level it meets) and the reply gains"
            " `matches_by_level`, adding up to `counts.matches`. No retry at a looser level."
            " Two meanings of exact: without `match_level` the rename is today's, with ^"
            ' codes literal and no levels; `match_level: "exact"` keeps that rule but reads'
            " the ^ codes and tags each change (`exact` there: character for character)."
            " Not with `case_sensitive` or `ignore_accents: false` at a folded level.",
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
    level: Level | None = None


@dataclass
class RenamePlan:
    edits: list[ReplaceText] = field(default_factory=list)
    changes: list[_Change] = field(default_factory=list)
    counts: Counts = field(default_factory=Counts)
    skipped: list[dict[str, Any]] = field(default_factory=list)  # where and why
    by_level: dict[Level, int] | None = None  # with a match_level: counts.matches per level


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
    match_level: Level | None = None,
) -> RenamePlan:
    """Every match of ``old`` in the body as a ``text.replace``; nothing is written.

    With ``match_level`` the search is ``find``'s :class:`LevelMatcher` at that level, and
    each change and skip is tagged with the strictest level it meets.
    """
    folds = match_level in ("default", "loose")
    if match_level is not None:
        old = translate_codes(old)

    def matcher(term: str) -> Matcher | LevelMatcher:
        options = SearchOptions(
            term,
            ignore_case=folds or not case_sensitive,
            ignore_accents=folds or ignore_accents,
            whole_word=whole_word,
        )
        return Matcher(options) if match_level is None else LevelMatcher(options, match_level)

    matchers = [
        (matcher(old + sfx_old), old + sfx_old, new + sfx_new)
        for sfx_old, sfx_new in _forms(inflections or [])
    ]
    dress = preserve_case and not case_sensitive
    plan = RenamePlan()
    counts = plan.counts
    by_level: dict[Level, int] = {}
    for para in index.paragraphs:
        text = para.text
        if not text:
            continue
        found: list[tuple[int, int, str, str, Level | None]] = []
        for search, exact, target in matchers:
            spans = list(search.spans(text))
            levels = search.levels if isinstance(search, LevelMatcher) else {}
            found += [(s, e, exact, target, levels.get((s, e))) for s, e in spans]
        if not found:
            continue
        found.sort(key=lambda m: (m[0], -m[1]))
        in_field = any(p.in_field for p in para.pieces)
        edge = 0
        counted = False
        for s, e, exact, target, level in found:
            if s < edge:
                continue  # inside a longer form already taken
            edge = e
            counts.matches += 1
            if level is not None:
                by_level[level] = by_level.get(level, 0) + 1
            if not (boundary_ok(text, s) and boundary_ok(text, e)):
                counts.skipped_boundary += 1
                plan.skipped.append(_skip(para.address, text, s, e, "boundary", level))
                continue
            if in_field and any(p.in_field for p in para.pieces_in(s, e)):
                counts.skipped_unsupported += 1
                plan.skipped.append(_skip(para.address, text, s, e, "field", level))
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
            plan.changes.append(_Change(at, para.number, s, e, matched, after, context, level))
            counts.replaced += 1
            if not counted:
                counts.paragraphs += 1
                counted = True
    if match_level is not None:
        plan.by_level = {level: by_level[level] for level in LEVELS if level in by_level}
    return plan


def _skip(
    address: Any, text: str, start: int, end: int, reason: str, level: Level | None = None
) -> dict[str, Any]:
    """Where a match was left alone and why: ``field`` (a field result), ``boundary``
    (it would split a character from its marks) or ``unsupported_structure``; with the
    level it matched at when the rename has a ``match_level``."""
    out = {"at": f"{address}:{start}-{end}", "text": text[start:end], "reason": reason}
    return out if level is None else {**out, "match_level": level}


def _order(at: str) -> tuple[int, int]:
    head, _, span = at.partition(":")
    return int(head[1:].split("#")[0]), int(span.split("-")[0])


def _drop_unsupported(pkg: Package, index: DocumentIndex, plan: RenamePlan) -> RenamePlan:
    """Keep only the edits the editor can make (each tried on a throwaway copy)."""
    ctx = RevisionContext(used_ids(pkg), "check", format_date(None))
    kept = RenamePlan(counts=plan.counts, skipped=plan.skipped, by_level=plan.by_level)
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
            skip = {"at": change.at, "text": change.before, "reason": "unsupported_structure"}
            if change.level is not None:
                skip["match_level"] = change.level
            kept.skipped.append(skip)
            continue
        kept.edits.append(edit)
        kept.changes.append(change)
    kept.counts.paragraphs = len({c.paragraph for c in kept.changes})
    return kept


@operation(
    "term.rename",
    "write",
    (
        "Rename a term or a character's name everywhere (Lucía to Elena) as one tracked "
        "batch: whole word, case, preserve case and inflections. Use it instead of many "
        "text.replace edits; run dry_run first to check the counts. Exact unless "
        "`match_level` folds it; then each change says the level it matched at."
    ),
    _SCHEMA,
)
def term_rename(request: dict[str, Any]) -> Reply:
    path = Path(request["doc"])
    old, new = request["from"], request["to"]
    level: Level | None = request.get("match_level")
    if level is not None:
        _check_level(request, level)
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
        match_level=level,
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
        "skipped": sorted(plan.skipped, key=lambda s: _order(s["at"]))[:limit],
        "preview": [_preview(c) for c in plan.changes[:limit]],
    }
    if plan.by_level is not None:
        result["matches_by_level"] = plan.by_level
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


def _preview(change: _Change) -> dict[str, Any]:
    out = {
        "at": change.at,
        "before": change.before,
        "after": change.after,
        "context": change.context,
    }
    return out if change.level is None else {**out, "match_level": change.level}


def _check_level(request: dict[str, Any], level: Level) -> None:
    """``find``'s check, and a folded level ignores case: no ``case_sensitive`` with it."""
    check_level(request, level)
    if level != "exact" and request.get("case_sensitive"):
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"match_level {level!r} ignores case; drop case_sensitive: true",
            path="$.case_sensitive",
        )
