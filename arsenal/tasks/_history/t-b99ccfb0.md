---
id: t-b99ccfb0
title: "T22: `.dotx` export and import of style sets"
label: "dotx export and import"
priority: 5
deps: [t-f2f3fe85]
---

# T22: `.dotx` export and import of style sets

## Acceptance gate
`dotx_validator_errors == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/ops/test_dotx.py::test_exported_dotx_passes_validator
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T22
```

## Tests
`test_exported_dotx_passes_validator` in `tests/ops/test_dotx.py` — every exported template passes the validator

## Location
ops

Spec: `status/specification.md` · Plan: `status/plan.md` (T22)


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
