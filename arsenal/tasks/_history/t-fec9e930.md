---
id: t-fec9e930
title: "T9: table.get: table grid with merges"
label: "table.get"
priority: 5
deps: [t-0a709c5e]
workspace: STORIES
status: merged
---

## Acceptance gate
table_grid_mismatches == 0 — from the STORIES plan (`arsenal/project/STORIES/plan.md`), spec `arsenal/project/STORIES/spec.md` §1 and §5.

```bash
uv run pytest -q tests/ops/test_tables.py -k test_table_grid_matches_libreoffice
```

## Tests
`test_table_grid_matches_libreoffice` in `tests/ops/test_tables.py` — every corpus table's grid equals LibreOffice's rows × cells, merges included

## Location
ops

## Description
`table.get`: the grid with `gridSpan`/`vMerge` and paragraph addresses per cell (surface: cloud)
