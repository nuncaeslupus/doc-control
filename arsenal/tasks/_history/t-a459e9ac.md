---
id: t-a459e9ac
title: "T24: Bundled recipes: generally useful docx-fixer rules, with their round-trip cases"
label: "bundled recipes"
priority: 1
deps: [t-333cfaa3, t-c028d6b7]
status: merged
---

# T24: Bundled recipes: generally useful docx-fixer rules, with their round-trip cases

## Acceptance gate
`docx_fixer_general_cases_failing == 0` — measured value recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/recipes/test_es_typography.py::test_dialogue_dash_recipe_fixes_hyphen_openers
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T24
```

## Tests
`test_dialogue_dash_recipe_fixes_hyphen_openers` in `tests/recipes/test_es_typography.py` — `- Hola` becomes `—Hola` as a tracked change

## Location
recipes

Spec: `status/specification.md` · Plan: `status/plan.md` (T24)


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
