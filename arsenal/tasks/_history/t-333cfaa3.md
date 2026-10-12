---
id: t-333cfaa3
title: "T14: Formatting operations: `style.apply`, `format.apply` as tracked `w:pPrChange`/`w:rPrChange`"
label: "formatting operations"
status: merged
priority: 5
deps: [t-7af47039]
---

# T14: Formatting operations: `style.apply`, `format.apply` as tracked `w:pPrChange`/`w:rPrChange`

## Acceptance gate
`content_edits_without_revision_markup == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/ops/test_format.py::test_style_apply_tracked_records_ppr_change
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T14
```

## Tests
`test_style_apply_tracked_records_ppr_change` in `tests/ops/test_format.py` — applying a style in tracked mode writes a `w:pPrChange` with the previous style

## Location
ops

Spec: `status/specification.md` · Plan: `status/plan.md` (T14)


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
