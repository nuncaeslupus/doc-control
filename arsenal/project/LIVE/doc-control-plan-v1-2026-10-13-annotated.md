# Doc Control — Plan (annotated edition)

> Generated 2026-10-13. This is the document with a **note slot** after every section that has text of its own. Read it in any Markdown app. To annotate, replace the `_(your notes…)_` placeholder under any section. When done, send the file back — notes are acted on.

---

# Plan

## Preamble & scope

**Date**: 2026-10-13
**Specification**: `arsenal/project/LIVE/spec.md`
**Author**: Claude (doc-control session), for the owner
**Revision**: 1
**Status**: draft
**Revision log**:
- r1 — first draft

> Review record, kept the way the spec keeps its own: each round of notes bumps
> **Revision** with a log line naming the export it applied. Approval is
> `approved (YYYY-MM-DD, revision N)`, plus `— without annotations` when there
> were none.

<!-- -->

> **✎ Notes** · `PLAN · intro`
> _(your notes here — replace this line)_

## Technical solution

### Architecture overview

A new package, `doc_control.live`, contains these modules:

- `protocol.py`: the `LiveAdapter` protocol (attach, paragraphs, select, selection, find_select, comment, tracked_replace, tracked_insert, save, close, reopen, status), plus a fake in-memory adapter for unit tests.
- `lo_uno.py`: the LibreOffice adapter, over UNO.
- `word_com.py`: the Word adapter, over COM.
- `dispatch.py`: routes an operation that carries `engine: "live"` to the chosen adapter and maps adapter failures to `DocControlError`.

The other changes are small hooks into existing code:

- **Registry:** `registry.py` routes an operation to the live engine when its request carries `engine: "live"`.
- **Lock check:** `safety/lock.py` learns LibreOffice's lock file.
- **Front doors:** `api.open(engine="live")` and the CLI/MCP flags are the new entry points.

<!-- -->

> **✎ Notes** · `PLAN › Architecture overview`
> _(your notes here — replace this line)_

### Data flow

1. The agent makes a call, e.g. `{"op": "comment.add", "engine": "live"}`.
2. `api` / `registry` passes it to `live.dispatch`, which picks an adapter.
3. The adapter attaches to the app (COM or UNO) and resolves the address by paragraph index. It checks the paragraph's fingerprint, reading the text with tracked deletions excluded.
4. The adapter makes the edit with tracking on and the configured author set. It then restores the app's settings.
5. The edit is recorded in history (`engine: live`), and the call returns a reply in the file engine's shape.

<!-- -->

> **✎ Notes** · `PLAN › Data flow`
> _(your notes here — replace this line)_

### State changes

| Component | Store | Change | Description |
|---------|----------|--------|-------------|
| History log | `.doc-control/` store | CREATE | One row per live edit, `engine: live`, no byte backup |
| Writer's open document | App memory | UPDATE | Tracked edits and comments; saved only on `live.save` / `live.close(save)` |

<!-- -->

> **✎ Notes** · `PLAN › State changes`
> _(your notes here — replace this line)_

### Technology choices

| Choice | Justification |
|--------|--------------|
| UNO (LibreOffice's API) | The only programmatic way to control a running LibreOffice. It ships with LibreOffice, and this container can import it. |
| pywin32 COM | The standard way to control Word on Windows, offered as an optional extra (`doc-control[live]`). |
| Fake adapter | Lets the unit tests for dispatch, errors and history run without either app. |

<!-- -->

> **✎ Notes** · `PLAN › Technology choices`
> _(your notes here — replace this line)_

### Out of scope

- Mac Word (AppleScript/JXA): a later spec (D-2).
- Live versions of operations other than `comment.add`, `text.replace`, `para.insert` and `live.*`.
- Byte-level verification while a document is open: edits are verified by re-reading the file after it is saved.

<!-- -->

> **✎ Notes** · `PLAN › Out of scope`
> _(your notes here — replace this line)_

## Implementation tasks

Surface: **cloud** runs in a Linux cloud session (LibreOffice installed); **windows-word** needs the owner's Windows session with Word.

| T# | Description | Service | Size | Depends | Gate | Tests |
|----|-------------|---------|------|---------|------|-------|
| T1 | Live protocol, fake adapter, `engine` field, dispatch, `capabilities.engines`/`live.apps`, `ENGINE_UNAVAILABLE` with reasons, lazy imports (surface: cloud) | live, registry | M | — | `live_unavailable_tracebacks == 0` | `test_live_call_without_app_returns_engine_unavailable` in `tests/live/test_dispatch.py` — every live op on a machine without an app gives `ENGINE_UNAVAILABLE` with a reason, never a traceback |
| T2 | Lock awareness: `DOC_LOCKED` recognises Word and LibreOffice lock files and names the app; `live.status.open_in` (surface: cloud) | safety | S | T1 | `live_lock_misreports == 0` | `test_lock_kinds_report_app` in `tests/live/test_lock.py` — each lock-file fixture (Word, LibreOffice, stale) reports the right app and staleness |
| T3 | LibreOffice adapter: start a listening instance or attach to one (D-L3), `live.open`/`live.save`/`live.close` with the unsaved-work guard, reopen (surface: cloud, libreoffice) | live | M | T1 | `unsaved_work_lost == 0` | `test_close_dirty_without_flag_refused` in `tests/live/test_lo_session.py` — closing a dirty document without `save`/`discard` gives `UNSAVED_CHANGES` and the document stays open and dirty |
| T4 | LibreOffice select by address and by find phrase + `paragraphs_after`; `live.selection`; paragraph enumeration matching the file engine, tracked deletions excluded (surface: cloud, libreoffice) | live | M | T3 | `live_select_mismatches == 0` | `test_select_matches_file_engine_text` in `tests/live/test_lo_select.py` — selected text equals the file engine's paragraph text for sampled paragraphs of every corpus kind |
| T5 | LibreOffice `comment.add`, tracked `text.replace`/`para.insert`; configurable author (D-L6); history rows `engine: live` (D-L5); settings restored on error; CI job on the system Python (surface: cloud, libreoffice) | live, CI | L | T4, T2 | `live_roundtrip_failures == 0` | `test_live_roundtrip_matches_file_engine` in `tests/live/test_lo_roundtrip.py` — after save and reopen the file engine reports exactly the live comments and revisions, and `unintended_text_changes == 0` |
| T6 | Select latency on the 100k-word novel in LibreOffice headless (surface: cloud, libreoffice) | live | S | T4 | `live_select_ms_p95 <= 500` | `test_live_select_latency` in `tests/bench/test_live_select.py` — p95 select-by-find latency is within budget |
| T7 | CLI and MCP front doors (`--engine live`, `live.*` tools) and docs (surface: cloud) | cli, mcp | S | T5 | `live_frontdoor_mismatches == 0` | `test_cli_and_mcp_route_live` in `tests/live/test_frontdoor.py` — CLI and MCP give the same reply as `api` for each live op on the fake adapter |
| T8 | Word COM adapter for T3–T5 plus `scripts/live_word_check.py`, run on Word 2016+ and Microsoft 365 (D-L4) (surface: windows-word) | live | L | T5 | `live_word_failures == 0` | `test_word_adapter_protocol_conformance` in `tests/live/test_word_com.py` (skipped off Windows) — the protocol suite that LibreOffice passes also passes against Word |

**Status legend**: ☐ not started · ◐ in progress · ☑ merged

**Merge order**: T1, then T2 and T3 in parallel, then T4, then T5 and T6, then T7 and T8.
**Branch pattern**: one PR per task from `main` (see `claude-arsenal/AGENTS.md`).

<!-- -->

> **✎ Notes** · `PLAN › Implementation tasks`
> _(your notes here — replace this line)_

### Dependency graph

```drawspec
{"version": 1, "kind": "flow", "title": "Task dependencies",
 "nodes": [
  {"id": "T1", "text": "T1\nLive protocol, fake adapter, engine…"},
  {"id": "T2", "text": "T2\nLock awareness: DOCLOCKED recognise…"},
  {"id": "T3", "text": "T3\nLibreOffice adapter: start a listen…"},
  {"id": "T4", "text": "T4\nLibreOffice select by address and b…"},
  {"id": "T5", "text": "T5\nLibreOffice comment.add, tracked te…"},
  {"id": "T6", "text": "T6\nSelect latency on the 100k-word nov…"},
  {"id": "T7", "text": "T7\nCLI and MCP front doors (--engine l…"},
  {"id": "T8", "text": "T8\nWord COM adapter for T3–T5 plus scr…"}
 ],
 "edges": [
  {"from": "T1", "to": "T2"},
  {"from": "T1", "to": "T3"},
  {"from": "T3", "to": "T4"},
  {"from": "T2", "to": "T5"},
  {"from": "T4", "to": "T5"},
  {"from": "T4", "to": "T6"},
  {"from": "T5", "to": "T7"},
  {"from": "T5", "to": "T8"}
 ]}
```

<!-- -->

> **✎ Notes** · `PLAN › Dependency graph`
> _(your notes here — replace this line)_

## Evidence log

| T# | Gate | Measured | Command | SHA | Env | Date |
|----|------|----------|---------|-----|-----|------|

<!-- -->

> **✎ Notes** · `PLAN › Evidence log`
> _(your notes here — replace this line)_

## Sign-off

- [ ] Plan approved by the owner
- [ ] Ready for execution

<!-- -->

> **✎ Notes** · `PLAN › Sign-off`
> _(your notes here — replace this line)_

