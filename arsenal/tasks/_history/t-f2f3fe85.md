---
id: t-f2f3fe85
title: "T21: Style write operations (`style.create`, `style.update`, `style.rename`, `style.merge`) and JSON style sets (`styleset.export`, `styleset.install`)"
label: "style write operations style"
priority: 5
deps: [t-7af47039]
status: merged
---

# T21: Style write operations (`style.create`, `style.update`, `style.rename`, `style.merge`) and JSON style sets (`styleset.export`, `styleset.install`)

## Acceptance gate
`style_definition_diffs_after_roundtrip == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/ops/test_stylesets.py::test_export_then_install_reproduces_style_definitions
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T21
```

## Tests
`test_export_then_install_reproduces_style_definitions` in `tests/ops/test_stylesets.py` — installing an exported set into a blank document reproduces the definitions

## Location
ops

Spec: `status/specification.md` · Plan: `status/plan.md` (T21)


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
