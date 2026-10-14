---
id: t-75ca0a7d
title: "T8: sections.list: read each section's page setup"
label: "sections.list"
priority: 10
deps: [t-766f260d, t-0a709c5e]
workspace: STORIES
---

## Acceptance gate
section_read_mismatches == 0 — from the STORIES plan (`arsenal/project/STORIES/plan.md`), spec `arsenal/project/STORIES/spec.md` §1 and §5.

```bash
uv run pytest -q tests/ops/test_sections.py -k test_sections_list_matches_libreoffice
```

## Tests
`test_sections_list_matches_libreoffice` in `tests/ops/test_sections.py` — each section of the `headers` and `novel` kinds reports the setup LibreOffice reads

## Location
ops

## Description
`sections.list`: break type, title page, even/odd, page-number start and format, header/footer references per kind (read-only, groundwork for S-9) (surface: cloud)
