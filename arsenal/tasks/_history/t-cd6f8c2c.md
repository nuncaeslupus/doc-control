---
id: t-cd6f8c2c
title: "T6: Document index: paragraphs, visible text with offsets to runs, addresses and fingerprints, range parsing"
label: "document index"
priority: 5
deps: [t-e5d218b7, t-85e83731]
status: merged
---

# T6: Document index: paragraphs, visible text with offsets to runs, addresses and fingerprints, range parsing

## Acceptance gate
`address_resolution_errors == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/model/test_address.py::test_address_roundtrip_every_paragraph_resolves
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T6
```

## Tests
`test_address_roundtrip_every_paragraph_resolves` in `tests/model/test_address.py` — every paragraph's address resolves back to the same paragraph

## Location
model

Spec: `status/specification.md` · Plan: `status/plan.md` (T6)


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
