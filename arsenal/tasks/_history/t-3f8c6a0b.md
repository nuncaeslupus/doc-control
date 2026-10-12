---
id: t-3f8c6a0b
title: "T31: Layout facts in `para.get` and `outline`: `empty`, `blank_before`, heading level (via `basedOn`), style, centred, page break before"
label: "layout facts in para"
priority: 10
deps: [t-dd07d765]
status: merged
---

# T31: Layout facts in `para.get` and `outline`: `empty`, `blank_before`, heading level (via `basedOn`), style, centred, page break before

## Acceptance gate
`layout_fact_mismatches == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/ops/test_layout.py::test_para_get_reports_blank_before_and_heading_level
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T31
```

## Tests
`test_para_get_reports_blank_before_and_heading_level` in `tests/ops/test_layout.py` — a heading after two empty paragraphs reports `blank_before: 2` and its level

## Location
ops

Spec: `status/specification.md` · Plan: `status/plan.md` (T31)


## Acceptance gate

<!-- Replace this with a fenced bash block. A gate that is only prose runs
     nothing, and a gate that runs nothing passes everything — `task_select.py`
     reports gate: false for a task with no block, so an unenforced gate is
     visible rather than quietly inert. -->

```bash
# arsenal:gate-placeholder — replace with the real check; it may land in this task's own PR
# e.g. bash tests/surface_probe_test.sh
false
```
