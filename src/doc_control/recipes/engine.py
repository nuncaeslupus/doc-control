"""Run a validated recipe over paragraph texts and produce a batch proposal.

The engine reads nothing but the texts it is given and returns operation dicts for
``batch``; it writes nothing. Every address in the proposal refers to the document as
read, so the result is a valid batch. With ``repeat_until_stable`` the recipe is run again
on the text its own replacements would leave, until a pass changes nothing or
``max_passes`` is reached. A paragraph that needed more than one pass is proposed as one
``text.replace`` per run of characters that differ between before and after. Every
``text.replace`` is narrowed to the characters that change, so a reviewer sees one small
tracked change per edit rather than the whole matched text deleted and typed again.
"""

from __future__ import annotations

import contextlib
import difflib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from doc_control.model.address import Address
from doc_control.model.search import IsolatedScan, Match, Matcher, SearchOptions
from doc_control.ops.tracked import boundary_ok
from doc_control.recipes.schema import DEFAULT_PASSES, SUBSTITUTION, search_options

MAX_OPS = 2000
TIME_BUDGET_SECONDS = 10.0
MAX_DIFF_CHARS = 4000  # longer paragraphs are compared as one smallest edit


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


def _edits(before: str, after: str) -> list[tuple[int, int, str]]:
    """The separate runs that differ between ``before`` and ``after`` (each ``(start, end, new)``).

    Falls back to the single smallest edit when a split would fall inside a character or the
    paragraph is too long to compare cheaply.
    """
    if len(before) <= MAX_DIFF_CHARS and len(after) <= MAX_DIFF_CHARS:
        ops = [
            op
            for op in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
            if op[0] != "equal"
        ]
        if all(
            boundary_ok(before, i1)
            and boundary_ok(before, i2)
            and boundary_ok(after, j1)
            and boundary_ok(after, j2)
            for _, i1, i2, j1, j2 in ops
        ):
            return [(i1, i2, after[j1:j2]) for _, i1, i2, j1, j2 in ops]
    edit = _smallest_edit(before, after)
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


def propose(
    recipe: dict[str, Any],
    paragraphs: Sequence[tuple[Address, str]],
    *,
    deadline: float | None = None,
) -> Proposal:
    """The batch proposal of ``recipe`` (already validated) over ``paragraphs``."""
    options = search_options(recipe)
    matcher = Matcher(options)
    steps: list[dict[str, Any]] = recipe["propose"]
    repeat = recipe.get("repeat_until_stable", False)
    max_passes = recipe.get("max_passes", DEFAULT_PASSES) if repeat else 1
    deadline = time.monotonic() + TIME_BUDGET_SECONDS if deadline is None else deadline

    work = {a.number: t for a, t in paragraphs}
    original = dict(work)
    address = {a.number: a for a, _ in paragraphs}
    direct: dict[int, list[dict[str, Any]]] = {}  # pass-1 text edits, kept when no 2nd pass
    others: list[dict[str, Any]] = []  # every other operation, from the original text only
    seen: set[tuple[str, str]] = set()
    redone: set[int] = set()  # paragraphs whose text changed in a pass after the first
    deleted: set[int] = set()
    warnings: list[str] = []
    total = 0
    passes = 0
    stable = False
    capped = False
    to_scan = sorted(n for n, t in work.items() if t)

    for passno in range(1, max_passes + 1):
        passes = passno
        found, stopped = _scan(options, matcher, [(n, work[n]) for n in to_scan], deadline)
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
                        edits.append((lo, hi, replacement))
                        if passno == 1:
                            op["at"] = f"{addr}:{lo}-{hi}"
                            op["text"] = replacement
                            direct.setdefault(number, []).append(op)
                        continue
                    if kind == "para.delete":
                        deleted.add(number)
                    if (kind, op["at"]) not in seen:
                        seen.add((kind, op["at"]))
                        others.append(op)
            if number in deleted:
                continue
            new = text
            for start, end, replacement in reversed(edits):
                new = new[:start] + replacement + new[end:]
            if new != text:
                changed.append(number)
                work[number] = new
                if passno > 1:
                    redone.add(number)
        if len(others) + sum(map(len, direct.values())) > MAX_OPS:
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
    for number in sorted(set(direct) | redone):
        if number in deleted:
            continue
        if number in redone:
            ops.extend(
                {"op": "text.replace", "at": f"{address[number]}:{start}-{end}", "text": new}
                for start, end, new in _edits(original[number], work[number])
            )
        else:
            ops.extend(direct[number])
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
