---
id: t-6f47ddb6
title: "LIVE T6: Select latency on the 100k-word novel in LibreOffice headless"
label: "LibreOffice live select latency"
priority: 10
deps: [t-f131d82b]
requires: [app:libreoffice]
tags: [LIVE]
workspace: LIVE
---
# LIVE T6: Select latency on the 100k-word novel in LibreOffice headless

Plan: `arsenal/project/LIVE/plan.md` (T6); contracts in `arsenal/project/LIVE/spec.md` §5.

## Acceptance gate
`live_select_ms_p95 <= 500`

```bash
uv run pytest -q -m bench tests/bench/test_live_select.py
```

## Tests
`test_live_select_latency` in `tests/bench/test_live_select.py` — p95 select-by-find latency within budget

## Location
tests/bench/
