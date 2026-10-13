---
id: t-7a888bcb
title: "T52: read operations accept UTF-8 .txt/.md directly"
label: "text sources"
priority: 10
status: merged
---

Requested by literary-critic (drops a .txt/.md → .docx re-conversion per run). A `.txt`, `.md` or `.markdown` path is read as a minimal in-memory document: blank-line blocks are paragraphs (wrapped lines joined), each further empty line in a run is an empty paragraph, a text with no empty line has one paragraph per line; Markdown `#`..`######` lines are Heading 1-6, thematic breaks keep their characters, emphasis markers and escapes are dropped. Write operations refuse it with `UNSUPPORTED_CONTENT` before any store is made.

## Acceptance gate
`text_source_mismatches == 0`, recorded in the plan's Evidence log (`status/plan.md`).

```bash
uv run pytest -q -s tests/ooxml/test_plaintext.py::test_text_source_gate
python3 .claude/skills/gate-check/scripts/run_gate.py --input status/plan.md --id T52
```
