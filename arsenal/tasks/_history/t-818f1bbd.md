---
id: t-818f1bbd
title: "T54: find phrases across paragraph marks, and in Word's Find syntax"
label: "cross-mark find phrases"
priority: 10
status: merged
---

Requested by literary-critic (T8 asks 1 and 3). When no run of words inside a paragraph is unique (short paragraphs like "-I-" or "Perdido" whose words all recur), `locate` takes the shortest phrase across a paragraph mark that Word's Find meets once: words ending the paragraph before + mark + its first words, its last words + mark + words opening the next, or the whole paragraph between both marks (`find_phrase_across`; the mark is `\r` in `find_phrase`). Every phrase also comes as `find_phrase_word`, escaped for Word's Find box (`^p ^t ^s ^~ ^- ^^`, None past 255 characters); `context` gains `after_word`.

## Acceptance gate
`find_phrase_gaps == 0`, recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q -s tests/ops/test_locate_across.py::test_find_phrase_gate
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T54
```
