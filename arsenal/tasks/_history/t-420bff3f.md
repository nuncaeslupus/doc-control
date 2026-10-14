---
id: t-420bff3f
title: "LAYOUT T7: toc.insert and fields.update"
label: "toc and fields.update"
priority: 1
deps: [t-a2f30637]
workspace: LAYOUT
status: merged
---

## Acceptance gate
toc_page_mismatches == 0 — from the LAYOUT plan (`arsenal/project/LAYOUT/plan.md`), spec `arsenal/project/LAYOUT/spec.md` §1 and §5.

```bash
uv run pytest -q tests/layout/test_toc.py -k test_toc_pages_match_rendered_pages
```

## Tests
`test_toc_pages_match_rendered_pages` in `tests/layout/test_toc.py` — after `fields.update`, each TOC entry's page number equals the page LibreOffice renders its heading on

## Location
ops

## Description
`toc.insert` and `fields.update` through LibreOffice; stale fields reported without it (surface: cloud)
