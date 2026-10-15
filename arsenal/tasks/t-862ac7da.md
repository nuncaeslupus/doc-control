---
id: t-862ac7da
title: "Plain writes in the live engines"
label: "plain writes, live engines"
priority: 10
deps: [t-f3d581d0]
---

## Acceptance gate
live_plain_write_failures == 0 (D-21) — decisions in `docs/decisions.md`.

```bash
uv run pytest -q tests/live/test_plain_live.py
```

## Tests
In `tests/live/test_plain_live.py` (skipped without Word / LibreOffice):
- `test_word_plain_replace_leaves_no_revision` — `batch {engine: live, tracked: false}` on a hidden `DispatchEx` Word leaves `doc.Revisions.Count` unchanged and restores `TrackRevisions` to its prior value;
- `test_lo_plain_replace_leaves_no_redline` — the same on LibreOffice;
- an extra scenario in `tests/live/word_suite.py` so `scripts/live_word_check.py` covers a plain write.

## Location
src/doc_control/live (protocol.py, word_com.py, lo_uno.py), src/doc_control/ops/live.py

## Description
Plain writes (D-21) in the live engines: the adapter protocol's `tracked_replace` / `tracked_insert` gain a `tracked` flag (or plain twins); Word sets `TrackRevisions = False` around the edit and restores it in `finally`; LibreOffice turns `RecordChanges` off likewise. Same overlap refusal as the file engine. Never touch the owner's Word in tests.
