---
id: t-ebf12f11
title: "LAYOUT T4: layout.apply numbering, breaks, recto starts"
label: "numbering apply"
priority: 1
deps: [t-3efc4aae, t-9c29142e, t-c3ed6610]
workspace: LAYOUT
---

## Acceptance gate
page_label_mismatches == 0 — from the LAYOUT plan (`arsenal/project/LAYOUT/plan.md`), spec `arsenal/project/LAYOUT/spec.md` §1 and §5.

```bash
uv run pytest -q tests/layout/test_layout_apply.py -k test_apply_numbering_scenarios_print_planned_labels
```

## Tests
`test_apply_numbering_scenarios_print_planned_labels` in `tests/layout/test_layout_apply.py` — applying each numbering scenario to a plain document prints the planned labels in LibreOffice's PDF and `layout.get` returns the plan

## Location
ops

## Description
`layout.apply` for breaks and numbering (start, format, shown), recto starts, `dry_run`, all or nothing (surface: cloud)
