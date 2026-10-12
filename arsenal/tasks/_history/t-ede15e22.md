---
id: t-ede15e22
status: merged
title: "T13: Undo log, `undo`, `restore`, `history.list`, `history.search`, `history.purge` (tombstone), orphans listing, backup on open"
label: "undo log undo restore"
priority: 5
deps: [t-7af47039]
---

# T13: Undo log, `undo`, `restore`, `history.list`, `history.search`, `history.purge` (tombstone), orphans listing, backup on open

## Acceptance gate
`undo_roundtrip_mismatches == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/safety/test_history.py::test_undo_each_logged_op_restores_prior_state
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T13
```

## Tests
`test_undo_each_logged_op_restores_prior_state` in `tests/safety/test_history.py` — undoing any logged batch returns text and paragraph properties to their prior state

## Location
safety

Spec: `status/specification.md` · Plan: `status/plan.md` (T13)


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
