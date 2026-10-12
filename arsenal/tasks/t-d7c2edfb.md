---
id: t-d7c2edfb
title: "T10: Footprint verifier, auto-restore, fault injection at every write stage"
label: "footprint verifier auto restore"
priority: 5
deps: [t-e5d218b7, t-d9e2aeb3]
---

# T10: Footprint verifier, auto-restore, fault injection at every write stage

## Acceptance gate
`partial_writes_on_failure == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/safety/test_verify.py::test_fault_at_each_stage_leaves_original_unchanged
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T10
```

## Tests
`test_fault_at_each_stage_leaves_original_unchanged` in `tests/safety/test_verify.py` — injecting a failure at each stage leaves the document byte-identical and returns `VERIFY_FAILED_RESTORED`

## Location
safety

Spec: `status/specification.md` · Plan: `status/plan.md` (T10)


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
