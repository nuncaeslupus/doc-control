---
id: t-bbe2aa24
title: "LAYOUT T8: captions, cross-references, index"
label: "captions and xrefs"
priority: 5
deps: [t-420bff3f]
workspace: LAYOUT
---

## Acceptance gate
xref_page_mismatches == 0 — from the LAYOUT plan (`arsenal/project/LAYOUT/plan.md`), spec `arsenal/project/LAYOUT/spec.md` §1 and §5.

```bash
uv run pytest -q tests/layout/test_xrefs.py -k test_captions_xrefs_and_index_resolve_after_update
```

## Tests
`test_captions_xrefs_and_index_resolve_after_update` in `tests/layout/test_xrefs.py` — after `fields.update`, every cross-reference shows its target's number and page, and every index entry its page

## Location
ops

## Description
`caption.add`, `xref.add` (REF/PAGEREF), `index.mark`/`index.insert` (surface: cloud)
