---
id: t-e7f75df9
title: "T55: .txt text_mode (auto/blocks/lines) and hard-wrap detection"
label: "txt text mode"
priority: 10
status: merged
---

Regression reported by literary-critic on 0.4.0 ("any empty line → blocks"): a writer's export with one paragraph per line and empty lines only around headings merged every chapter into one paragraph; verse stanzas merged. `auto` now picks `blocks` only when the text looks hard-wrapped (lines inside multi-line blocks mostly end within 10 % of one width from 50 to 120, or the median block has 2+ lines and no line passes 120 characters), else `lines` (every line a paragraph, every empty line an empty paragraph). `api.open(path, text_mode="auto"|"blocks"|"lines")` and `DOC_CONTROL_TEXT_MODE` choose explicitly; `doc.source.mode` reports the mode used.

## Acceptance gate
`text_mode_mismatches == 0`, recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q -s tests/ooxml/test_text_mode.py::test_text_mode_gate
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T55
```
