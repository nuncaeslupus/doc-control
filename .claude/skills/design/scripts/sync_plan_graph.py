#!/usr/bin/env python3
"""sync_plan_graph.py — draw a plan's dependency graph from its task table.

The graph is a ```drawspec fence (nodes = tasks, edges = the Depends column)
under the plan's `### Dependency graph` heading. It is generated, not drawn by
hand, so it cannot drift from the table: `validate_plan.py` compares the two and
names every edge one has and the other lacks.

    sync_plan_graph.py --input status/plan.md           print the drawspec block
    sync_plan_graph.py --input status/plan.md --apply   replace the block in the plan
                                                        (adds the section after the
                                                        task table when it is missing)

Imported by validate_plan.py for `task_edges` / `graph_problems`.

Exit 0 = printed or written, 1 = no task table with T# and Depends columns,
2 = usage error (file missing).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TASK_ID_RE = re.compile(r"\bT\d+\b")
DELIM_CELL = re.compile(r"^:?-+:?$")
GRAPH_HEADING_RE = re.compile(r"^(#{2,4})\s+.*dependency graph", re.IGNORECASE)
FENCE_OPEN_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})[ \t]*(?P<lang>[\w-]*)[ \t]*$")
# drawspec's readability budget for one flow; past it the document is refused.
FLOW_NODE_BUDGET, FLOW_EDGE_BUDGET = 15, 16
DETAIL_CHARS = 36


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def task_table(text: str) -> list[dict[str, str]]:
    """Rows of the first table whose header has a T# and a Depends column."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        header = [c.strip("`").lower() for c in _cells(line)]
        if "t#" not in header or not any(h.startswith("depend") for h in header):
            continue
        rows = []
        for row_line in lines[i + 1 :]:
            if "|" not in row_line:
                break
            cells = _cells(row_line)
            if all(DELIM_CELL.match(c) for c in cells if c):
                continue
            rows.append(dict(zip(header, cells, strict=False)))
        return rows
    return []


def task_edges(text: str) -> tuple[list[tuple[str, str]], set[tuple[str, str]]] | None:
    """([(task id, description)] in table order, {(dependency, task)}), or None without a table."""
    rows = task_table(text)
    if not rows:
        return None
    depends_col = next(k for k in rows[0] if k.startswith("depend"))
    tasks: list[tuple[str, str]] = []
    edges: set[tuple[str, str]] = set()
    for row in rows:
        m = TASK_ID_RE.search(row.get("t#", ""))
        if not m:
            continue
        tid = m.group(0)
        tasks.append((tid, row.get("description", "")))
        for dep in TASK_ID_RE.findall(row.get(depends_col, "")):
            edges.add((dep, tid))
    return tasks, edges


def _detail(description: str) -> str:
    plain = re.sub(r"[`*_]", "", description).strip()
    if len(plain) > DETAIL_CHARS:
        plain = plain[: DETAIL_CHARS - 1].rstrip() + "…"
    return plain


def drawspec_block(tasks: list[tuple[str, str]], edges: set[tuple[str, str]]) -> str:
    """The ```drawspec fence for these tasks: one node per task, one edge per Depends entry."""
    order = {tid: n for n, (tid, _) in enumerate(tasks)}
    nodes = []
    for tid, desc in tasks:
        detail = _detail(desc)
        node = {"id": tid, "text": f"{tid}\n{detail}" if detail else tid}
        nodes.append("  " + json.dumps(node, ensure_ascii=False))
    ordered = sorted(edges, key=lambda e: (order.get(e[1], 1e9), order.get(e[0], 1e9), e))
    edge_lines = ["  " + json.dumps({"from": a, "to": b}) for a, b in ordered]
    out = ["```drawspec", '{"version": 1, "kind": "flow", "title": "Task dependencies",']
    out.append(' "nodes": [')
    out.append(",\n".join(nodes))
    if edge_lines:
        out.append(" ],")
        out.append(' "edges": [')
        out.append(",\n".join(edge_lines))
    out.append(" ]}")
    out.append("```")
    return "\n".join(out)


def find_graph(text: str) -> tuple[int, int, str, str] | None:
    """(start line, end line, fence language, fence body) of the dependency graph's fence.

    The fence is the first one after the `Dependency graph` heading and before
    the next heading; lines [start, end) are the fence itself. A heading with no
    fence under it gives start == end == where its section ends, an empty
    language and an empty body. None when the plan has no such heading.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not GRAPH_HEADING_RE.match(line):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].startswith("#"):
            m = FENCE_OPEN_RE.match(lines[j].strip())
            if m:
                fence = m.group("fence")
                k = j + 1
                while k < len(lines) and not (
                    lines[k].strip()
                    and set(lines[k].strip()) == {fence[0]}
                    and len(lines[k].strip()) >= len(fence)
                ):
                    k += 1
                return j, k + 1, m.group("lang").lower() or "plain", "\n".join(lines[j + 1 : k])
            j += 1
        while j > i + 1 and not lines[j - 1].strip():
            j -= 1
        return j, j, "", ""
    return None


FIX_HINT = "regenerate it: sync_plan_graph.py --input <plan> --apply"


def graph_warnings(text: str) -> list[str]:
    """Advisories about the plan's dependency graph that should not fail validation.

    A graph that is not a drawspec fence (the ASCII art earlier templates seeded)
    cannot be checked against the table, but a plan written before the drawspec
    graph is still a valid plan: it is flagged with the fix, not refused.
    """
    found = find_graph(text)
    if found is None or task_edges(text) is None:
        return []
    lang = found[2]
    if lang == "drawspec":
        return []
    what = f"a ```{lang} block" if lang and lang != "plain" else "no ```drawspec block"
    return [f"dependency graph is {what}, not a ```drawspec fence — {FIX_HINT}"]


def graph_problems(text: str) -> list[str]:
    """Where a drawspec dependency graph disagrees with the Depends column; [] when it agrees.

    A plan without a dependency-graph section has nothing to disagree with, and
    one whose graph is not a drawspec fence is a warning (graph_warnings), not a
    problem. Only a real drawspec graph is held to the table.
    """
    found = find_graph(text)
    table = task_edges(text)
    if found is None or table is None:
        return []
    fix = FIX_HINT
    _, _, lang, body = found
    if lang != "drawspec":
        return []
    try:
        doc = json.loads(body)
    except ValueError as exc:
        return [f"dependency graph is not valid JSON ({exc}) — {fix}"]
    if not isinstance(doc, dict):
        return [f"dependency graph is not a drawspec document — {fix}"]
    tasks, want = table
    have = {
        (str(e.get("from")), str(e.get("to")))
        for e in doc.get("edges") or []
        if isinstance(e, dict)
    }
    nodes = {str(n.get("id")) for n in doc.get("nodes") or [] if isinstance(n, dict)}
    problems = []
    for a, b in sorted(want - have):
        problems.append(f"dependency graph is missing edge {a} → {b} (Depends of {b}) — {fix}")
    for a, b in sorted(have - want):
        problems.append(
            f"dependency graph has edge {a} → {b}, which no Depends cell declares — {fix}"
        )
    for tid in [t for t, _ in tasks if t not in nodes]:
        problems.append(f"dependency graph has no node for task {tid} — {fix}")
    return problems


def write_graph(text: str, block: str) -> str:
    """The plan with its dependency-graph fence replaced by `block` (or the section added)."""
    lines = text.splitlines()
    found = find_graph(text)
    if found is not None:
        start, end, _, _ = found
        new = [*lines[:start], *([""] if start == end else []), block, *lines[end:]]
    else:
        # After the task table and whatever directly follows it, before the next ## section.
        anchor = len(lines)
        seen_table = False
        for i, line in enumerate(lines):
            if not seen_table and "|" in line and "t#" in line.lower():
                seen_table = True
            elif seen_table and line.startswith("## "):
                anchor = i
                break
        new = [*lines[:anchor], "### Dependency graph", "", block, "", *lines[anchor:]]
    return "\n".join(new) + ("\n" if text.endswith("\n") else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input", default="status/plan.md", help="plan file (default: status/plan.md)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="replace the plan's dependency-graph block in place"
    )
    args = parser.parse_args()
    path = Path(args.input)
    if not path.is_file():
        print(f"✗ {path} not found — pass --input <plan>", file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8")
    table = task_edges(text)
    if table is None:
        print(f"✗ {path}: no task table with T# and Depends columns", file=sys.stderr)
        return 1
    tasks, edges = table
    if len(tasks) > FLOW_NODE_BUDGET or len(edges) > FLOW_EDGE_BUDGET:
        print(
            f"⚠  {len(tasks)} tasks / {len(edges)} edges is past what drawspec draws in one flow "
            f"({FLOW_NODE_BUDGET} nodes, {FLOW_EDGE_BUDGET} edges) — split the plan into phases",
            file=sys.stderr,
        )
    block = drawspec_block(tasks, edges)
    if not args.apply:
        print(block)
        return 0
    path.write_text(write_graph(text, block), encoding="utf-8")
    print(f"✓ {path}: dependency graph drawn from {len(tasks)} task(s), {len(edges)} edge(s)")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
