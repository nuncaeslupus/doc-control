---
id: t-d9e2aeb3
title: "T9: Safety basics: `~$` lock detection, sha256 change detection, `.doc-control/` store layout, document id, backups and retention, app-data option"
label: "safety basics"
priority: 5
deps: [t-129fb2b5]
---

# T9: Safety basics: `~$` lock detection, sha256 change detection, `.doc-control/` store layout, document id, backups and retention, app-data option

## Acceptance gate
`writes_with_word_lock_present == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/safety/test_lock.py::test_write_with_lock_file_present_returns_doc_locked
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T9
```

## Tests
`test_write_with_lock_file_present_returns_doc_locked` in `tests/safety/test_lock.py` — a batch with a `~$` file next to the document fails with `DOC_LOCKED` and leaves it untouched

## Location
safety

Spec: `status/specification.md` · Plan: `status/plan.md` (T9)


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
