---
id: t-1c0bdf9c
title: "T47: Cold bulk read: one shared parse per paragraph, C-accelerated reads, faster normalised text"
label: "cold bulk read speed"
priority: 5
status: merged
---

Requested by literary-critic: the first `paragraphs(normalized=True, pages=True)` on a real
74k-word manuscript took about 2.1 s. Readers share one parse per paragraph (index build,
layout facts, page model, paragraph properties), parsed with ElementTree's C parser
(writers keep the bookkeeping parser), and `normalized_text` uses `str.translate` and
regexes for the common case.

## Acceptance gate
`cold_bulk_read_regressions == 0`: the bulk read gives the same rows as before and the
synthetic 100k-word novel's cold `paragraphs(normalized=True, pages=True)` stays within its
budget, recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q -s tests/bench/test_cold_read.py::test_cold_bulk_read_same_rows_within_budget
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T47
```
