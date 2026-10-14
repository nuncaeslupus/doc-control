---
id: t-c2d58e6d
title: "live: centre the selected passage, a cursor select mode, bring the app to the front"
label: "select centred and in front"
priority: 5
tags: [LIVE]
status: merged
---

Imported from issue #196 (items 1-3; item 4, the `doc-control://` link, waits on the
owner's decision).

1. `live.select` scrolls the passage to the middle of the window.
2. `mode: "cursor"` leaves the cursor at the passage's start instead of selecting it, so
   typing or Delete cannot replace it.
3. `live.select` and `live.open` bring the app's window to the front (`front: false` to
   leave it where it is).

## Acceptance gate
```bash
uv run pytest -q tests/live/test_select_view.py
```
