---
id: t-7a888bcb
title: "T52: read operations accept UTF-8 .txt/.md directly"
label: "text sources"
priority: 10
status: merged
---

Requested by literary-critic (drops a .txt/.md → .docx re-conversion per run). A `.txt`, `.md` or `.markdown` path is read as a minimal in-memory document. Text: UTF-32/UTF-16 with BOM, else UTF-8 (BOM or not) with non-UTF-8 lines read as cp1252; BOM-less UTF-16 refused. `.txt` mode: `blocks` when the text holds an empty line (blank-line blocks joined; each extra empty line an empty paragraph), else `lines` (every line a paragraph). Markdown: front matter skipped, `#`..`######` → Heading 1-6, break lines kept as text, emphasis/escapes handled, code fences literal. The reply's `doc.source` names format, encoding and mode. Write operations (and a text `compare` output) are refused with `UNSUPPORTED_CONTENT` before anything is written.

## Acceptance gate
`text_source_mismatches == 0`, recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q -s tests/ooxml/test_plaintext.py::test_text_source_gate
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T52
```
