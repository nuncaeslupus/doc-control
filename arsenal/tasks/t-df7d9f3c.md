---
id: t-df7d9f3c
title: "FIND T10: docs for match levels"
label: "match level docs"
priority: 10
deps: [t-aeb67121, t-fa877f39, t-09b3d9f7]
workspace: FIND
---

## Acceptance gate
operations_doc_drift == 0 — from the FIND plan (`arsenal/project/FIND/plan.md`), spec `arsenal/project/FIND/spec.md` §1 and §5.

```bash
uv run pytest -q tests/test_docs.py -k test_operations_md_is_generated
```

## Tests
`test_operations_md_is_generated` in `tests/test_docs.py` — `docs/operations.md` equals `operations_markdown()` with the new `match_level` fields

## Location
docs

## Description
Docs: `docs/operations.md` regenerated from the registry, `match_level` and the two meanings of "exact" in the op descriptions, the owner's `--` dialogue case for `scripts/real_word_check` notes (surface: cloud)
