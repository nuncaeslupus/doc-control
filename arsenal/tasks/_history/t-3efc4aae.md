---
id: t-3efc4aae
title: "LAYOUT T1: page-label checker (LibreOffice PDF)"
label: "page-label checker"
priority: 5
workspace: LAYOUT
status: merged
---

## Acceptance gate
page_label_checker_failures == 0 — from the LAYOUT plan (`arsenal/project/LAYOUT/plan.md`), spec `arsenal/project/LAYOUT/spec.md` §1 and §5.

```bash
uv run pytest -q tests/layout/test_page_labels.py -k test_page_labels_of_handmade_scenarios
```

## Tests
`test_page_labels_of_handmade_scenarios` in `tests/layout/test_page_labels.py` — each hand-written scenario's printed page labels equal its expected list, and a mislabelled fixture is reported

## Location
tests

## Description
Page-label checker: scenario fixtures (front matter unnumbered then 1, roman then arabic, counted unnumbered part page, restart per part, recto chapter starts, two-sided mirrored headers) built by a small fixture writer; LibreOffice PDF export plus `pdftotext`; `poppler-utils` in the CI `libreoffice` job (surface: cloud)
