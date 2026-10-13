---
id: t-dee18c2f
title: "T40: Structure: chapter starts from style names, part level above chapters, front matter unit"
label: "parts, style chapters, front"
priority: 5
deps: []
---

# T40: Structure detection for parts, style-marked chapters and front matter

Requested by literary-critic, measured on a real Spanish manuscript (3 parts holding 54
untitled chapters plus front matter; doc-control 0.0.1 found 3 chapters).
- A chapter may start at a body paragraph (no heading text) whose style name holds a chapter
  word (configurable per language, e.g. "Normal inicio capítulo"), signal `chapter-style`;
  profile settings can extend or replace the word list.
- A `part` level above chapters: a heading-titled unit that holds style-marked or lower-level
  chapters is a part and does not swallow them; `segment` gains unit `parts`, chapters carry
  their part, `locate` reports the part.
- A `front` unit: matter before the first part or chapter (title page, dedication).

## Acceptance gate
`structure_part_chapter_errors == 0` — on generated books with parts, style-marked untitled
chapters and front matter, every part, chapter and front boundary is found exactly.
Recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q tests/model/test_structure_parts.py::test_parts_and_style_chapters_found_exactly
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T40
```
