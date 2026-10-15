---
id: t-338eb5dd
title: "history.adopt: re-attach a moved document's store"
label: "history.adopt moved documents"
priority: 5
status: merged
---

## Acceptance gate
history_adopt_mismatches == 0 (D-22) — decisions in `docs/decisions.md`.

```bash
uv run pytest -q tests/safety/test_history_adopt.py
```

## Tests
In `tests/safety/test_history_adopt.py`:
- `test_adopt_after_move` — a document written twice, then moved alone to another folder: `open` reports one `adoptable` candidate; `history.adopt` moves the store to the new `.doc-control/<name>/`, `undo` then takes back the last batch byte for byte;
- `test_adopt_after_rename_in_place` — same for `Old.docx` → `New.docx`;
- `test_adopt_needs_force_when_sha_differs` — after an edit outside doc-control the match is refused without `force`;
- `test_adopt_dry_run_moves_nothing`;
- `test_adopt_refuses_when_target_store_exists` — a document that already has a non-empty store is not overwritten;
- `test_adopt_appdata_mode` — the same with `DOC_CONTROL_STORE=appdata` (path-hash keyed stores).

## Location
src/doc_control/safety (history.py, store.py), src/doc_control/ops/history.py, docs/operations.md

## Description
`history.adopt {doc, store?, force?, dry_run?}` (D-22): re-attach a moved or renamed document to its orphan store. Without `store`, candidates come from `find_orphans` over the document's folder, the old folders named in orphan logs, app-data and the custom store root, matched by the document's sha256 equal to the store's last `sha256_after`; with one match it is used, with several the reply lists them. The store folder is moved (same volume: rename; else copy then remove) to `store_dir(doc)`, keeping `id`, backups and log, and an `adopt` entry logs the old path and store. Refuse when the target store already holds entries. `open` on a document without a store adds `adoptable: [...]` (cheap: only stores whose last sha matches). Docs: the move guidance ("move `.doc-control/<name>/` along") stays as the manual route.
