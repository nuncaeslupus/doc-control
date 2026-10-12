"""Run a validated recipe over paragraph texts and produce a batch proposal.

The engine reads nothing but the texts it is given and returns operation dicts for
``batch``; it writes nothing. Every address in the proposal refers to the document as
read, so the result is a valid batch. With ``repeat_until_stable`` the recipe is run again
on the text its own replacements would leave, until a pass changes nothing or
``max_passes`` is reached. A paragraph that needed more than one pass is proposed as one
``text.replace`` over the smallest range that differs between before and after.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from doc_control.model.address import Address
from doc_control.model.search import Matcher
from doc_control.ops.tracked import boundary_ok
from doc_control.recipes.schema import DEFAULT_PASSES, SUBSTITUTION, search_options

MAX_OPS = 2000
TIME_BUDGET_SECONDS = 10.0


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
    pattern = matcher.pattern if options.regex else None

    work = {a.number: t for a, t in paragraphs}
    original = dict(work)
    address = {a.number: a for a, _ in paragraphs}
    direct: dict[int, list[dict[str, Any]]] = {}  # pass-1 text edits, kept when no 2nd pass
    others: list[dict[str, Any]] = []
    seen: set[str] = set()
    redone: set[int] = set()  # paragraphs whose text changed in a pass after the first
    deleted: set[int] = set()
    warnings: list[str] = []
    total = 0
    passes = 0
    stable = False
    capped = False

    for passno in range(1, max_passes + 1):
        passes = passno
        changed = False
        for number in sorted(work):
            if number in deleted or capped:
                continue
            if time.monotonic() > deadline:
                warnings.append(f"TIME_BUDGET: stopped at p{number}; the proposal is partial")
                capped = True
                break
            text = work[number]
            spans = list(matcher.spans(text))
            if not spans:
                continue
            addr = address[number]
            edits: list[tuple[int, int, str]] = []
            for start, end in spans:
                groups: Sequence[str] = ()
                if pattern is not None and pattern.groups:
                    found = pattern.match(text, start)
                    if found is None or found.end() != end:
                        continue
                    groups = [g or "" for g in found.groups()]
                total += 1
                matched = text[start:end]
                for step in steps:
                    op = _instantiate(step, addr, start, end, matched, text, groups)
                    if op["op"] == "para.delete":
                        deleted.add(number)
                        key = json.dumps(op, sort_keys=True)
                        if key not in seen:
                            seen.add(key)
                            others.append(op)
                    elif op["op"] == "text.replace":
                        lo, hi = (0, len(text)) if step["at"] == "$para" else (start, end)
                        if text[lo:hi] == op["text"]:
                            continue  # already what the recipe wants
                        edits.append((lo, hi, op["text"]))
                        if passno == 1:
                            direct.setdefault(number, []).append(op)
                    else:
                        key = json.dumps(op, sort_keys=True)
                        if key not in seen:
                            seen.add(key)
                            others.append(op)
            if number in deleted:
                continue
            new = text
            for start, end, replacement in reversed(edits):
                new = new[:start] + replacement + new[end:]
            if new != text:
                changed = True
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
    if not stable and not capped and repeat:
        warnings.append(f"NOT_STABLE: still changing after {max_passes} passes")
    if capped:
        stable = False

    ops: list[dict[str, Any]] = []
    for number in sorted(set(direct) | redone):
        if number in deleted:
            continue
        if number in redone:
            start, end, new = _smallest_edit(original[number], work[number])
            if (start, end, new) != (0, 0, ""):
                ops.append(
                    {"op": "text.replace", "at": f"{address[number]}:{start}-{end}", "text": new}
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
    out = {k: v for k, v in step.items() if k != "at"}
    if "text" in out:
        out["text"] = _substitute(out["text"], matched, para, groups)
    out["at"] = str(addr) if step["at"] == "$para" else f"{addr}:{start}-{end}"
    return {"op": out.pop("op"), "at": out.pop("at"), **out}
