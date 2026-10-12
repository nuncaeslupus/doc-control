---
id: t-dc3c106b
title: "T33: Structure detection: stable chapter and scene ids, detection signals, fingerprint-keyed overrides, profile settings (minimum chapter words)"
label: "structure detection"
priority: 5
deps: [t-206b1b9a]
status: merged
---

# T33: Structure detection: stable chapter and scene ids, detection signals, fingerprint-keyed overrides, profile settings (minimum chapter words)

## Acceptance gate
`structure_override_failures == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/model/test_structure.py::test_override_forces_chapter_start_by_fingerprint
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T33
```

## Tests
`test_override_forces_chapter_start_by_fingerprint` in `tests/model/test_structure.py` — an override makes the given paragraph start a chapter and is reported as the signal

## Location
model

Spec: `status/specification.md` · Plan: `status/plan.md` (T33)


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
