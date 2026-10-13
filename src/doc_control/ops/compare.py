"""``compare``: write a NEW redline ``.docx`` from two versions of a document.

Request: ``old`` and ``new`` (paths to ``.docx`` files), ``output`` (path of the redline to
create; it must not exist and must not be one of the inputs), optional ``author`` (default
``Claude``), ``tag`` and ``date`` (ISO 8601 with a time zone; default: now).

The output is a copy of the OLD document's package in which every difference is a tracked
change by one author, so that accepting all gives the text of the NEW version and
rejecting all gives the text of the OLD one. Neither input is touched. Paragraphs are
aligned by :func:`doc_control.model.align.align` (the engine of ``diff``) and the changes
are written with the tracked-text primitives of ``para.*`` / ``text.replace``
(:func:`doc_control.ops.tracked.prepare_edits`):

* ``unchanged`` paragraphs stay as they are;
* ``modified`` paragraphs get word-level ``w:del`` / ``w:ins`` (``difflib`` on word tokens,
  whitespace kept with the word before it), so only the words that changed are marked;
* ``deleted`` paragraphs are tracked deletions, paragraph mark included;
* ``inserted`` paragraphs are tracked insertions, with the properties of the paragraph
  they follow in the old document (the old package has no style of the new one to copy);
* ``moved`` paragraphs are a tracked deletion at the old place plus a tracked copy at the
  new one (v1 has no ``w:moveFrom`` / ``w:moveTo``); a moved paragraph that was also
  edited is deleted and inserted with its new text;
* ``merged`` paragraphs: one replacement over the changed span that also deletes the
  marks between the old paragraphs, so accepted they are one paragraph;
* ``split`` paragraphs: the old paragraph is edited into the first part and the other
  parts are insertions.

Result: ``output`` (path, sha256), ``summary`` (paragraphs by kind, from ``diff``),
``revisions`` (count of tracked changes written), ``author``, ``date``. The envelope's
``doc`` is the old file. A warning is given when Word cannot represent a deletion (see
``_Plan.trailing``): the paragraph text is deleted and an empty paragraph remains.
"""

from __future__ import annotations

import itertools
import re
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.align import (
    MERGED,
    MODIFIED,
    MOVED,
    SPLIT,
    UNCHANGED,
    Alignment,
    align,
)
from doc_control.model.index import DocumentIndex, Paragraph, build_index
from doc_control.ooxml.plaintext import refuse_write
from doc_control.ooxml.revisions import (
    DEFAULT_AUTHOR,
    ParagraphEditor,
    RevisionContext,
    format_author,
    format_date,
    mark_state,
)
from doc_control.ooxml.zipio import Package, build_package, commit_bytes
from doc_control.ops.tracked import (
    DeleteParagraphs,
    Edit,
    InsertParagraph,
    MoveParagraphs,
    ReplaceText,
    boundary_ok,
    next_in_container,
    prepare_edits,
)
from doc_control.registry import operation
from doc_control.safety.verify import DOCUMENT_PART, verify_footprint

__all__ = ["CompareResult", "compare_documents", "word_edits"]

_TOKEN = re.compile(r"\w+\s*|[^\w\s]\s*|\s+")


# -- word-level differences -------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    """Words (with the whitespace after them) and punctuation marks, never splitting a
    character from its combining marks."""
    out: list[str] = []
    for m in _TOKEN.finditer(text):
        if out and not boundary_ok(text, m.start()):
            out[-1] += m.group()
        else:
            out.append(m.group())
    return out


def _offsets(tokens: list[str]) -> list[int]:
    out = [0]
    for tok in tokens:
        out.append(out[-1] + len(tok))
    return out


def word_edits(old: str, new: str) -> list[tuple[int, int, str]]:
    """Replacements ``(start, end, text)`` over ``old`` (offsets, in order, never touching)
    that turn it into ``new``, at word level."""
    if old == new:
        return []
    a, b = _tokens(old), _tokens(new)
    oa, ob = _offsets(a), _offsets(b)
    out: list[tuple[int, int, str]] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            out.append((oa[i1], oa[i2], new[ob[j1] : ob[j2]]))
    return out


def _common_edges(old: str, new: str) -> tuple[int, int]:
    """Characters of ``old`` that open it and close it identically in ``new`` (whole tokens)."""
    a, b = _tokens(old), _tokens(new)
    head = 0
    while head < min(len(a), len(b)) and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < min(len(a), len(b)) - head and a[-1 - tail] == b[-1 - tail]:
        tail += 1
    return sum(map(len, a[:head])), sum(map(len, a[len(a) - tail :]))


# -- the plan -----------------------------------------------------------------------------------


def _chain(pairs: list[tuple[int, int]]) -> list[int]:
    """Indexes (into ``pairs``, sorted by old) of a longest chain increasing in new."""
    tails: list[int] = []
    tail_at: list[int] = []
    prev = [-1] * len(pairs)
    for i, (_, n) in enumerate(pairs):
        k = bisect_left(tails, n)
        if k == len(tails):
            tails.append(n)
            tail_at.append(i)
        else:
            tails[k] = n
            tail_at[k] = i
        prev[i] = tail_at[k - 1] if k else -1
    out: list[int] = []
    i = tail_at[-1] if tail_at else -1
    while i >= 0:
        out.append(i)
        i = prev[i]
    return out[::-1]


@dataclass
class _Plan:
    """What happens to each paragraph; indexes are 0-based."""

    old: DocumentIndex
    new: DocumentIndex
    kept: dict[int, int] = field(default_factory=dict)  # old -> new, in place
    tails: dict[int, list[int]] = field(default_factory=dict)  # merge head -> swallowed olds
    moves: dict[int, int] = field(default_factory=dict)  # new -> old (copy of its text)
    removed: set[int] = field(default_factory=set)  # olds deleted outright
    moved_out: set[int] = field(default_factory=set)  # olds that leave by a tracked move
    warnings: list[str] = field(default_factory=list)
    trailing: bool = False  # a deletion Word cannot represent was approximated


def _plan(old: DocumentIndex, new: DocumentIndex, alignment: Alignment) -> _Plan:
    plan = _Plan(old, new)
    pairs = [
        (c.old[0], c.new[0], c)
        for c in alignment.changes
        if c.kind in (UNCHANGED, MODIFIED, MOVED, SPLIT, MERGED) and c.old and c.new
    ]
    chain = {pairs[i][0] for i in _chain([(o, n) for o, n, _ in pairs])}
    consumed: set[int] = set()
    for o, n, change in pairs:
        if o not in chain:
            continue
        plan.kept[o] = n
        if change.kind == MERGED and len(change.old) > 1:
            run = [old.paragraphs[i] for i in change.old]
            if all(next_in_container(old, a) is b for a, b in itertools.pairwise(run)) and list(
                change.old
            ) == list(range(change.old[0], change.old[0] + len(run))):
                plan.tails[o] = list(change.old[1:])
                consumed.update(change.old[1:])
    # Move candidates: identical text, not in the chain, and the original can leave.
    for o, n, change in pairs:
        if (
            o not in plan.kept
            and change.kind in (UNCHANGED, MOVED)
            and old.paragraphs[o].text == new.paragraphs[n].text
            and next_in_container(old, old.paragraphs[o]) is not None
        ):
            plan.moves[n] = o
            plan.moved_out.add(o)
    matched_new = set(plan.kept.values())
    last_old, last_new = len(old) - 1, len(new) - 1
    if (
        last_old not in plan.kept
        and last_old not in consumed
        and last_new not in matched_new
        and last_old >= 0
        and last_new >= 0
    ):
        # The last paragraphs end their documents: the old one cannot be deleted (Word cannot
        # remove that mark), so it becomes the new one.
        plan.kept[last_old] = last_new
        source = plan.moves.pop(last_new, None)
        if source is not None:
            plan.moved_out.discard(source)
        matched_new.add(last_new)
    plan.removed = set(range(len(old))) - set(plan.kept) - consumed - plan.moved_out
    return plan


def _anchor(plan: _Plan, kept_by_new: dict[int, int]) -> dict[int, int | None]:
    """For each new paragraph to insert: the old paragraph it follows (None: at the start)."""
    out: dict[int, int | None] = {}
    last: int | None = None
    for j in range(len(plan.new)):
        if j in kept_by_new:
            o = kept_by_new[j]
            last = plan.tails[o][-1] if o in plan.tails else o
        else:
            out[j] = last
    return out


# -- the edits ----------------------------------------------------------------------------------


def _at(para: Paragraph, start: int | None = None, end: int | None = None) -> str:
    if start is None:
        return str(para.address)
    return f"{para.address}:{start}-{end}"


def _cross(first: Paragraph, start: int, last: Paragraph, end: int) -> str:
    return f"{first.address}:{start}..{last.address}:{end}"


def _merge_edit(
    old: DocumentIndex, head: int, tails: list[int], text: str
) -> tuple[Paragraph, int, Paragraph, int, str]:
    """One replacement from the old paragraphs ``head`` + ``tails`` (marks between them
    deleted) to ``text``: ``(first, offset, last, offset, new text)``."""
    paras = [old.paragraphs[i] for i in (head, *tails)]
    joined = "".join(p.text for p in paras)
    lead, trail = _common_edges(joined, text)
    cuts = [len(p.text) for p in paras]
    first_end = cuts[0]
    last_start = len(joined) - cuts[-1]
    start = min(lead, first_end)
    end = max(len(joined) - trail, last_start)
    replacement = text[start : len(text) - (len(joined) - end)]
    return paras[0], start, paras[-1], end - last_start, replacement


def _unmove_trailing(plan: _Plan) -> None:
    """Moves whose original sits right before a removed last paragraph of a container become
    a plain deletion plus an insertion: the whole run is then removed as one."""
    old = plan.old
    by_old = {o: n for n, o in plan.moves.items()}
    for b in sorted(plan.removed):
        if next_in_container(old, old.paragraphs[b]) is not None:
            continue
        j = b - 1
        while (
            j in plan.removed | plan.moved_out
            and next_in_container(old, old.paragraphs[j]) is (old.paragraphs[j + 1])
        ):
            if j in plan.moved_out:
                plan.moved_out.discard(j)
                plan.removed.add(j)
                if j in by_old:
                    plan.moves.pop(by_old[j], None)
            j -= 1


def _edits(plan: _Plan) -> list[Edit]:
    old, new = plan.old, plan.new
    edits: list[Edit] = []
    kept_by_new = {n: o for o, n in plan.kept.items()}
    anchors = _anchor(plan, kept_by_new)
    anchored = {a for a in anchors.values() if a is not None}
    for j in [j for j, o in plan.moves.items() if not _can_move(plan, o, anchors.get(j))]:
        gone = plan.moves.pop(j)  # no tracked move possible: delete and insert
        plan.moved_out.discard(gone)
        plan.removed.add(gone)

    _unmove_trailing(plan)
    # Removal runs and the trailing special case: the last paragraph of a container.
    swallow: dict[int, tuple[Paragraph, int]] = {}  # kept old -> last paragraph it absorbs
    runs: list[list[int]] = []
    for o in sorted(plan.removed):
        para = old.paragraphs[o]
        if runs and runs[-1][-1] == o - 1 and next_in_container(old, old.paragraphs[o - 1]) is para:
            runs[-1].append(o)
        else:
            runs.append([o])
    delete_runs: list[list[int]] = []
    for run in runs:
        last = old.paragraphs[run[-1]]
        if next_in_container(old, last) is not None:
            delete_runs.append(run)
            continue
        prev = run[0] - 1
        if (
            prev >= 0
            and prev not in anchored
            and next_in_container(old, old.paragraphs[prev]) is old.paragraphs[run[0]]
        ):
            head = next((h for h, t in plan.tails.items() if t[-1] == prev), None)
            if head is not None:  # the merge takes the run in as more of its tail
                plan.tails[head].extend(run)
                continue
            if prev in plan.kept:
                swallow[prev] = (last, len(last.text))
                continue
        plan.trailing = True
        plan.warnings.append(
            f"{last.address} is the last paragraph of its container and cannot be removed: "
            "its text is deleted and an empty paragraph remains"
        )
        if len(run) > 1:
            delete_runs.append(run[:-1])
        if last.text:
            edits.append(ReplaceText(_at(last, 0, len(last.text)), ""))

    for run in delete_runs:
        first, last = old.paragraphs[run[0]], old.paragraphs[run[-1]]
        edits.append(
            DeleteParagraphs(
                str(first.address) + (f"..{last.address}" if first is not last else "")
            )
        )

    for o, n in sorted(plan.kept.items()):
        para, target = old.paragraphs[o], new.paragraphs[n].text
        if o in plan.tails:
            first, s, last, e, text = _merge_edit(old, o, plan.tails[o], target)
            edits.append(ReplaceText(_cross(first, s, last, e), text))
            continue
        changes = word_edits(para.text, target)
        if o in swallow:
            end_para, end = swallow[o]
            if changes and changes[-1][1] == len(para.text):
                s, _, text = changes.pop()
            else:
                s, text = len(para.text), ""
            edits.append(ReplaceText(_cross(para, s, end_para, end), text))
        edits.extend(ReplaceText(_at(para, s, e), text) for s, e, text in changes)

    for j, anchor in anchors.items():
        where: dict[str, str]
        if anchor is None:
            where = {"before": str(old.paragraphs[0].address)}
        else:
            where = {"after": str(old.paragraphs[anchor].address)}
        source = plan.moves.get(j)
        if source is not None and _can_move(plan, source, anchor):
            edits.append(MoveParagraphs(str(old.paragraphs[source].address), **where))
        else:
            edits.append(InsertParagraph(new.paragraphs[j].text, **where))
    return edits


def _carryable(old: DocumentIndex, para: Paragraph) -> bool:
    """False if the paragraph holds a bookmark, comment anchor or field a copy cannot carry."""
    elem = old.spans.parse_paragraph(para.span_index)
    try:
        ParagraphEditor(elem, para, RevisionContext(set(), "x", "d")).copy_runs()
    except DocControlError:
        return False
    return True


def _can_move(plan: _Plan, source: int, anchor: int | None) -> bool:
    """A tracked move needs a target slot in front of a neighbour, away from the source."""
    old = plan.old
    if not _carryable(old, old.paragraphs[source]):
        return False
    if anchor is None:
        return source != 0
    last = plan.tails[anchor][-1] if anchor in plan.tails else anchor
    return (
        next_in_container(old, old.paragraphs[last]) is not None
        and anchor not in (source, source - 1)
        and last not in (source, source - 1)
    )


# -- the result -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CompareResult:
    output: Path
    sha256: str
    summary: dict[str, int]
    revisions: int
    author: str
    date: str
    old: dict[str, Any]
    new: dict[str, Any]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "old": self.old,
            "new": self.new,
            "output": {"path": str(self.output), "sha256": self.sha256},
            "summary": self.summary,
            "revisions": self.revisions,
            "author": self.author,
            "date": self.date,
        }


def accepts_to(got: list[str], want: list[str], *, unremovable: bool, foreign: bool) -> bool:
    """Is ``got`` (accept-all) the new text? Exact, except for the two known differences:
    an unremovable last paragraph leaves one extra empty paragraph, and another author's
    pending paragraph deletion joins paragraphs (the text must still agree)."""
    if got == want:
        return True
    if foreign:
        return "".join(got) == "".join(want)
    if unremovable:
        return [t for t in got if t] == [t for t in want if t] and len(got) >= len(want)
    return False


def _accepted_texts(data: bytes, ids: set[str]) -> tuple[list[str], bool]:
    """Paragraph texts of the redline with this call's revisions accepted, and whether
    another author's tracked paragraph deletion is pending (it joins paragraphs too)."""
    index = build_index(data)
    out: list[str] = []
    carry = ""
    foreign = False
    for para in index.paragraphs:
        elem = index.spans.parse_paragraph(para.span_index)
        text = carry + para.text
        marks = mark_state(elem)
        foreign = foreign or any(kind == "del" and rid not in ids for kind, rid in marks)
        if any(kind == "del" and rid in ids for kind, rid in marks):
            carry = text
        else:
            out.append(text)
            carry = ""
    if carry:
        out.append(carry)
    return out, foreign


def _check_output(output: Path, inputs: list[Path]) -> None:
    refuse_write(output)
    for source in inputs:
        same = output.exists() and source.exists() and output.samefile(source)
        if same or output.resolve() == source.resolve():
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                f"output is one of the inputs: {output}",
                path=str(output),
            )
    if output.exists():
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"output already exists, nothing was written: {output}",
            path=str(output),
        )
    if not output.parent.is_dir():
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"the folder of the output does not exist: {output.parent}",
            path=str(output),
        )


def compare_documents(
    old: Path,
    new: Path,
    output: Path,
    *,
    author: str = DEFAULT_AUTHOR,
    tag: str | None = None,
    now: datetime | None = None,
    stage: Callable[[str], None] | None = None,
) -> CompareResult:
    """Write the redline of ``old`` -> ``new`` to ``output`` (see the module docstring).

    Raises ``DOC_NOT_FOUND`` / ``NOT_A_DOCX`` for an input, ``SCHEMA_INVALID`` for an
    unusable output path, ``UNSUPPORTED_CONTENT`` for content the tracked-text
    primitives refuse, ``INTERNAL`` if the redline would not accept to the new text or
    reject to the old one (nothing is written) and ``VERIFY_FAILED_RESTORED`` if the
    written file fails verification (it is removed). Both inputs are only read.
    """
    _check_output(output, [old, new])
    pkg_old, pkg_new = Package(old), Package(new)
    index_old = DocumentIndex.from_package(pkg_old)
    index_new = DocumentIndex.from_package(pkg_new)
    alignment = align(index_old.paragraphs, index_new.paragraphs)
    info_old = {"path": str(old), "sha256": pkg_old.sha256, "paragraphs": len(index_old)}
    if pkg_old.text_source is not None:
        info_old["source"] = pkg_old.text_source
    info_new = {"path": str(new), "sha256": pkg_new.sha256, "paragraphs": len(index_new)}
    if pkg_new.text_source is not None:
        info_new["source"] = pkg_new.text_source
    plan = _plan(index_old, index_new, alignment)
    edits = _edits(plan)

    if not edits:  # identical texts: the redline is a copy
        payload = build_package(pkg_old, {})
        revisions: list[int] = []
        author_out, date_out = format_author(author, tag), format_date(now)
        footprint = None
    else:
        prepared = prepare_edits(
            pkg_old, edits, author=author, tag=tag, now=now, layered=True, index=index_old
        )
        result = prepared.result
        revisions, author_out, date_out = result.revision_ids, result.author, result.date
        footprint = prepared.footprint
        got, foreign = _accepted_texts(prepared.new_data, {str(r) for r in revisions})
        want = [p.text for p in index_new.paragraphs]
        if not accepts_to(got, want, unremovable=plan.trailing, foreign=foreign):
            bad = next((n for n, (a, b) in enumerate(zip(got, want, strict=False), 1) if a != b), 0)
            raise DocControlError(
                ErrorCode.INTERNAL,
                f"self-check: accepting the redline does not give the new text (paragraph {bad})",
                paragraph=bad,
            )
        if foreign:
            plan.warnings.append(
                "the old document has tracked paragraph deletions by other authors: "
                "accepting everything also joins those paragraphs"
            )
        payload = build_package(pkg_old, {DOCUMENT_PART: prepared.new_data})

    try:
        commit_bytes(payload, output, stage, exclusive=True)
    except FileExistsError as exc:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            f"output already exists, nothing was written: {output}",
            path=str(output),
        ) from exc
    written = Package(output)
    if footprint is not None:
        problems = verify_footprint(pkg_old, written, footprint)
        if problems or written.read(DOCUMENT_PART) != prepared.new_data:
            output.unlink(missing_ok=True)
            raise DocControlError(
                ErrorCode.VERIFY_FAILED_RESTORED,
                "the redline failed verification and was removed: " + "; ".join(problems),
                path=str(output),
                problems=problems,
            )
    return CompareResult(
        output=output,
        sha256=written.sha256,
        summary=alignment.counts(),
        revisions=len(revisions),
        author=author_out,
        date=date_out,
        old=info_old,
        new=info_new,
        warnings=plan.warnings,
    )


def _parse_date(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f"date is not ISO 8601: {value!r}", date=value
        ) from exc
    if moment.tzinfo is None:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            "date needs a time zone, e.g. 2026-10-12T09:00:00Z",
            date=value,
        )
    return moment


@operation(
    "compare",
    "read",
    "Write a new redline .docx from two versions: every difference a tracked change; "
    "the inputs are never modified.",
    {
        "type": "object",
        "properties": {
            "old": {"type": "string", "description": "Path of the earlier version (.docx)."},
            "new": {"type": "string", "description": "Path of the later version (.docx)."},
            "output": {
                "type": "string",
                "description": "Path of the redline to create (must not exist).",
            },
            "author": {"type": "string", "description": "Revision author (default Claude)."},
            "tag": {"type": "string", "description": "Appended to the author as `author (tag)`."},
            "date": {"type": "string", "description": "Revision date, ISO 8601 with time zone."},
        },
        "required": ["old", "new", "output"],
        "additionalProperties": False,
    },
)
def compare(request: dict[str, Any]) -> Reply:
    done = compare_documents(
        Path(request["old"]),
        Path(request["new"]),
        Path(request["output"]),
        author=request.get("author", DEFAULT_AUTHOR),
        tag=request.get("tag"),
        now=_parse_date(request["date"]) if "date" in request else None,
    )
    return Reply(done.to_dict(), doc=done.old, warnings=done.warnings)
