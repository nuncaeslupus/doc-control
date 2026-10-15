"""Run a validated recipe over paragraph texts and produce a batch proposal.

The engine reads nothing but the texts it is given and returns operation dicts for
``doc.batch``; it writes nothing. Every address in the proposal refers to the document as
read, so the result is a valid batch. With ``repeat_until_stable`` the recipe is run again
on the text its own replacements would leave, until a pass changes nothing or
``max_passes`` is reached. Every ``text.replace`` is narrowed to the characters that change
and the engine follows each character from the text as read to the final text, so a reviewer
sees one small tracked change per edit (changes that touch are one change) however many
passes it took.

A rule with ``match.where`` is run only on the paragraphs whose own style, heading status
and emptiness, and whose neighbours, meet its conditions, all judged on the document as
read. Neighbour text conditions are regexes and run isolated like ``text.find``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from doc_control.model.address import Address
from doc_control.model.search import IsolatedScan, Match, Matcher, SearchOptions
from doc_control.ops.tracked import boundary_ok
from doc_control.recipes.schema import DEFAULT_PASSES, SUBSTITUTION, search_options

MAX_OPS = 2000
TIME_BUDGET_SECONDS = 10.0


@dataclass(frozen=True)
class Context:
    """What a ``where`` condition can see of a paragraph besides its text."""

    styles: frozenset[str] = frozenset()  # its style id and name, lower-case
    heading: bool = False
    content: bool = False  # no text, but an image or a break: not an empty paragraph


NO_CONTEXT = Context()


@dataclass
class Proposal:
    ops: list[dict[str, Any]]
    passes: int
    stable: bool
    matches: int
    warnings: list[str] = field(default_factory=list)


def _substitute(text: str, match: str, para: str, groups: Sequence[str]) -> str:
    def one(m: Any) -> str:
        ref = m.group(1)
        if ref == "$":
            return "$"
        if ref == "match":
            return match
        if ref == "para":
            return para
        return groups[int(ref) - 1]

    return SUBSTITUTION.sub(one, text)


def _smallest_edit(before: str, after: str) -> tuple[int, int, str]:
    """``(start, end, new)``: replace ``before[start:end]`` with ``new``."""
    limit = min(len(before), len(after))
    head = 0
    while head < limit and before[head] == after[head]:
        head += 1
    while head and not (boundary_ok(before, head) and boundary_ok(after, head)):
        head -= 1
    tail = 0
    while tail < limit - head and before[-1 - tail] == after[-1 - tail]:
        tail += 1
    while tail and not (
        boundary_ok(before, len(before) - tail) and boundary_ok(after, len(after) - tail)
    ):
        tail -= 1
    return head, len(before) - tail, after[head : len(after) - tail]


def _trim(text: str, lo: int, hi: int, new: str) -> tuple[int, int, str]:
    """Narrow replacing ``text[lo:hi]`` by ``new`` to the characters that really change."""
    old = text[lo:hi]
    limit = min(len(old), len(new))
    head = 0
    while head < limit and old[head] == new[head]:
        head += 1
    while head and not (boundary_ok(text, lo + head) and boundary_ok(new, head)):
        head -= 1
    tail = 0
    while tail < limit - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    while tail and not (boundary_ok(text, hi - tail) and boundary_ok(new, len(new) - tail)):
        tail -= 1
    return lo + head, hi - tail, new[head : len(new) - tail]


def _runs(original: str, final: str, origin: list[int]) -> list[tuple[int, int, str]]:
    """The separate changes that turn ``original`` into ``final`` (each ``(start, end, new)``).

    ``origin[k]`` is the offset in ``original`` of ``final[k]``, or -1 for a character a
    replacement typed. The characters between two kept ones form one change, so changes that
    touch are merged and none overlaps another. Falls back to the single smallest edit when a
    change would split a character.
    """
    gaps: list[tuple[int, int, int, int, str]] = []  # (start, end, new start, new end, new)
    prev = -1
    pending = 0  # offset in ``final`` where the typed characters began
    typed: list[str] = []
    for k, (char, at) in enumerate(zip(final, origin, strict=True)):
        if at < 0:
            typed.append(char)
            continue
        new = "".join(typed)
        if prev + 1 < at or new:
            gaps.append((prev + 1, at, pending, k, new))
        typed = []
        prev = at
        pending = k + 1
    new = "".join(typed)
    if prev + 1 < len(original) or new:
        gaps.append((prev + 1, len(original), pending, len(final), new))
    gaps = [g for g in gaps if original[g[0] : g[1]] != g[4]]
    if all(
        boundary_ok(original, s)
        and boundary_ok(original, e)
        and boundary_ok(final, j1)
        and boundary_ok(final, j2)
        for s, e, j1, j2, _ in gaps
    ):
        return [(s, e, new) for s, e, _, _, new in gaps]
    edit = _smallest_edit(original, final)
    return [] if edit == (0, 0, "") else [edit]


def _scan(
    options: SearchOptions, matcher: Matcher, items: list[tuple[int, str]], deadline: float
) -> tuple[list[tuple[int, list[Match]]], int | None]:
    """Matches per paragraph, and the paragraph the time budget stopped at (or ``None``).

    A regex runs in a child process that is killed at the deadline (Python's regex engine
    cannot be interrupted, and the static screen cannot catch every pattern that
    backtracks badly). A literal cannot backtrack and is searched here.
    """
    if options.regex:
        scan = IsolatedScan(options, items, deadline, detail=True)
        with contextlib.closing(scan.__iter__()) as found:
            return [(number, list(spans)) for number, spans in found], scan.stopped_at
    out: list[tuple[int, list[Match]]] = []
    for done, (number, text) in enumerate(items):
        if done and time.monotonic() > deadline:
            return out, number
        spans = list(matcher.matches(text))
        if spans:
            out.append((number, spans))
    return out, None


def _empty(text: str, ctx: Context) -> bool:
    return text == "" and not ctx.content


def _meets(cond: dict[str, Any], text: str, ctx: Context) -> bool:
    """``cond``'s own-paragraph conditions (``text`` and ``missing`` are checked elsewhere)."""
    if "empty" in cond and cond["empty"] != _empty(text, ctx):
        return False
    if "heading" in cond and cond["heading"] != ctx.heading:
        return False
    if "style" in cond and not ctx.styles & {s.lower() for s in cond["style"]}:
        return False
    return not ("not_style" in cond and ctx.styles & {s.lower() for s in cond["not_style"]})


def _eligible(
    where: dict[str, Any],
    paragraphs: Sequence[tuple[Address, str]],
    context: Mapping[int, Context],
    deadline: float,
) -> tuple[list[int], int | None]:
    """The paragraphs ``where`` admits, and where a neighbour regex ran out of time."""
    order = [a.number for a, _ in paragraphs]
    text = {a.number: t for a, t in paragraphs}
    own_empty = bool(where.get("empty"))
    patterns = {c["text"] for side in ("prev", "next") for c in where.get(side, []) if "text" in c}
    hits: dict[str, set[int]] = {}
    items = [(n, text[n]) for n in order if text[n]]
    for pattern in sorted(patterns):
        options = SearchOptions(pattern, regex=True)
        found, stopped = _scan(options, Matcher(options), items, deadline)
        if stopped is not None:
            return [], stopped
        hits[pattern] = {n for n, _ in found}
    skip = bool(where.get("skip_empty"))

    def neighbours(i: int, step: int, count: int) -> list[int | None]:
        out: list[int | None] = []
        j = i + step
        while len(out) < count:
            if not 0 <= j < len(order):
                out.append(None)
                continue
            if not skip or not _empty(text[order[j]], context.get(order[j], NO_CONTEXT)):
                out.append(order[j])
            j += step
        return out

    admitted: list[int] = []
    for i, number in enumerate(order):
        if own_empty and not _empty(text[number], context.get(number, NO_CONTEXT)):
            continue
        if not own_empty and not text[number]:
            continue
        if not _meets(where, text[number], context.get(number, NO_CONTEXT)):
            continue
        ok = True
        for side, step in (("prev", -1), ("next", 1)):
            conds = where.get(side, [])
            for cond, other in zip(conds, neighbours(i, step, len(conds)), strict=True):
                if other is None:
                    ok = cond.get("missing", False)
                elif cond.get("missing") or not _meets(
                    cond, text[other], context.get(other, NO_CONTEXT)
                ):
                    ok = False
                elif "text" in cond:
                    ok = other in hits[cond["text"]]
                if not ok:
                    break
            if not ok:
                break
        if ok:
            admitted.append(number)
    return admitted, None


def propose(
    recipe: dict[str, Any],
    paragraphs: Sequence[tuple[Address, str]],
    *,
    deadline: float | None = None,
    context: Mapping[int, Context] | None = None,
) -> Proposal:
    """The batch proposal of ``recipe`` (already validated) over ``paragraphs``.

    ``paragraphs`` are in document order and include the empty ones when the recipe has
    neighbour conditions; ``context`` gives their styles and heading status.
    """
    options = search_options(recipe)
    matcher = Matcher(options) if options is not None else None
    steps: list[dict[str, Any]] = recipe["propose"]
    repeat = recipe.get("repeat_until_stable", False)
    max_passes = recipe.get("max_passes", DEFAULT_PASSES) if repeat else 1
    deadline = time.monotonic() + TIME_BUDGET_SECONDS if deadline is None else deadline

    work = {a.number: t for a, t in paragraphs}
    original = dict(work)
    address = {a.number: a for a, _ in paragraphs}
    origin: dict[int, list[int]] = {}  # per changed paragraph: original offset of each character
    n_edits = 0
    others: list[dict[str, Any]] = []  # every other operation, from the original text only
    seen: set[tuple[str, str]] = set()
    deleted: set[int] = set()
    warnings: list[str] = []
    total = 0
    passes = 0
    stable = False
    capped = False
    where = recipe["match"].get("where")
    if where is None:
        to_scan = sorted(n for n, t in work.items() if t)
    else:
        to_scan, stopped = _eligible(where, paragraphs, context or {}, deadline)
        if stopped is not None:
            warnings.append(f"TIME_BUDGET: stopped at p{stopped}; the proposal is empty")
            return Proposal([], 0, False, 0, warnings)

    for passno in range(1, max_passes + 1):
        passes = passno
        if options is None or matcher is None:
            found: list[tuple[int, list[Match]]] = [(n, [(0, len(work[n]), ())]) for n in to_scan]
            stopped = None
        else:
            items = [(n, work[n]) for n in to_scan]
            found, stopped = _scan(options, matcher, items, deadline)
        if stopped is not None:
            warnings.append(f"TIME_BUDGET: stopped at p{stopped}; the proposal is partial")
            capped = True
        changed: list[int] = []
        for number, spans in found:
            if number in deleted:
                continue
            text = work[number]
            addr = address[number]
            edits: list[tuple[int, int, str]] = []
            for start, end, groups in spans:
                if passno == 1:
                    total += 1
                matched = text[start:end]
                for step in steps:
                    kind = step["op"]
                    if kind != "text.replace" and passno > 1:
                        continue  # other operations are decided on the text as read
                    op = _instantiate(step, addr, start, end, matched, text, groups)
                    if kind == "text.replace":
                        lo, hi = (0, len(text)) if step["at"] == "$para" else (start, end)
                        if text[lo:hi] == op["text"]:
                            continue  # already what the recipe wants
                        lo, hi, replacement = _trim(text, lo, hi, op["text"])
                        if edits and lo < edits[-1][1]:
                            continue  # overlaps the edit before it (e.g. a repeated $para)
                        edits.append((lo, hi, replacement))
                        continue
                    if kind == "para.delete":
                        deleted.add(number)
                    if (kind, op["at"]) not in seen:
                        seen.add((kind, op["at"]))
                        others.append(op)
            if number in deleted:
                continue
            new = text
            track = origin.setdefault(number, list(range(len(text))))
            for start, end, replacement in reversed(edits):
                new = new[:start] + replacement + new[end:]
                track[start:end] = [-1] * len(replacement)
            n_edits += len(edits)
            if new != text:
                changed.append(number)
                work[number] = new
        if len(others) + n_edits > MAX_OPS:
            warnings.append(f"OPS_CAP: more than {MAX_OPS} operations; narrow the recipe")
            capped = True
        if capped:
            break
        if not changed:
            stable = True
            break
        to_scan = [n for n in changed if work[n]]
    if not stable and not capped and repeat:
        warnings.append(f"NOT_STABLE: still changing after {max_passes} passes")
    if capped:
        stable = False

    ops: list[dict[str, Any]] = []
    for number in sorted(origin):
        if number in deleted or work[number] == original[number]:
            continue
        ops.extend(
            {"op": "text.replace", "at": f"{address[number]}:{start}-{end}", "text": new}
            for start, end, new in _runs(original[number], work[number], origin[number])
        )
    ops.extend(others)
    return Proposal(ops, passes, stable, total, warnings)


def _instantiate(
    step: dict[str, Any],
    addr: Address,
    start: int,
    end: int,
    matched: str,
    para: str,
    groups: Sequence[str],
) -> dict[str, Any]:
    out = {k: v for k, v in step.items() if k not in ("op", "at")}
    if "text" in out:
        out["text"] = _substitute(out["text"], matched, para, groups)
    at = str(addr) if step["at"] == "$para" else f"{addr}:{start}-{end}"
    return {"op": step["op"], "at": at, **out}
