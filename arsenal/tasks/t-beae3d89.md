---
id: t-beae3d89
title: "T41: Anchors: exact match on normalised text (soft hyphens, zero-width, tabs, double spaces)"
label: "exact anchors on normalised"
priority: 5
---

`anchor.resolve` exact mode matches the quote against the normalised text (`text.normalized` rules: soft hyphens and zero-width removed, whitespace collapsed, NFC) and maps the hit back to original offsets through the offset map, so a quote an agent copied from normalised text is found across a soft hyphen, zero-width character, tab or double space. Fuzzy and best modes keep their behaviour. Requested by literary-critic (its edit path runs on doc-control).

## Acceptance gate
`exact_quotes_not_found_after_normalisation == 0`, recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q -s tests/model/test_anchor_normalised.py::test_exact_quotes_match_on_normalised_text
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T41
```
