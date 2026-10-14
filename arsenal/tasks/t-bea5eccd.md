---
id: t-bea5eccd
title: "T7: note.add: new footnote or endnote, tracked"
label: "note.add"
priority: 5
deps: [t-db820394]
workspace: STORIES
---

## Acceptance gate
note_add_failures == 0 — from the STORIES plan (`arsenal/project/STORIES/plan.md`), spec `arsenal/project/STORIES/spec.md` §1 and §5.

```bash
uv run pytest -q tests/ops/test_note_add.py -k test_note_add_tracked_and_rejectable
```

## Tests
`test_note_add_tracked_and_rejectable` in `tests/ops/test_note_add.py` — a new note appears in LibreOffice as a tracked insertion, rejecting removes the mark and the note, and undo restores the file byte for byte

## Location
ops

## Description
`note.add` (footnote or endnote at a body offset; mark and text as tracked insertions; creates the notes part when missing) (surface: cloud)
