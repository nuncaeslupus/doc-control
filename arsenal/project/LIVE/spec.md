# Specification: doc-control live engine (Word and LibreOffice)

**Date**: 2026-10-13
**Ticket / PR**: — (workspace `LIVE`)
**Author**: Claude (doc-control session), for the owner
**Revision**: 1
**Status**: draft
**Revision log**:
- r1 — first draft
- r1 (pre-review) — adversarial-review risks folded in: text-level `unintended_text_changes`, fingerprints vs tracked deletions, `uno` not on PyPI, LibreOffice attach limits, lock-file kinds, global author name, Word Find limits, index access for latency, COM retry, build order vs D-2

> Review record. Each round of reviewer notes makes a new revision: bump
> **Revision**, add a log line naming the export it applied, and commit that
> export beside this file in the same commit. On approval set **Status** to
> `approved (YYYY-MM-DD, revision N)` and, when the reviewer sent no notes for
> that revision, add `— without annotations`.

---

## 1. Problem statement

doc-control's file engine only edits a closed `.docx`: while the writer has the manuscript open in Word or LibreOffice, an agent can read the file on disk but cannot point at a passage on screen, add a comment the writer sees at once, or propose a tracked edit without asking the writer to save, close and reopen. literary-critic (`lc word`) has retired its own Word-control interface in favour of doc-control and falls back to computer-use clicks for this today. The core spec reserved room for a live engine (D-2: Windows first, Mac later; same operation names, engine chosen per call, `ENGINE_UNAVAILABLE` otherwise). This spec covers that engine: the same Python API and operation names, driving an already-open document in Microsoft Word on Windows (COM) and in LibreOffice Writer on any OS (UNO), so an agent can select a passage by Find string or address, read the selection, add comments and tracked insertions/replacements, and save, close and reopen — never losing the writer's unsaved work.

**Success criteria (measurable)**:

- [ ] `live_roundtrip_failures == 0` with LibreOffice (headless, in CI on Linux) over every corpus kind: open a corpus `.docx`; for sampled paragraphs, select by `find_phrase` (+ `paragraphs_after`) and by address, and the selected text equals the file engine's paragraph text; `comment.add` lands on that range; a tracked `text.replace` and `para.insert` appear as revisions; save, close, reopen keeps them; the file engine then reads the saved file and `comments.list` / `revisions.list` report exactly the live changes, and every other paragraph's text, style and order equal the original (`unintended_text_changes == 0`; byte identity cannot hold, since the app rewrites the whole package on save).
- [ ] `live_word_failures == 0`: the same scripted round-trip run by the owner on Windows with Word 2016+ and Microsoft 365 (`scripts/live_word_check.py`, results recorded in the plan's evidence log).
- [ ] `unsaved_work_lost == 0`: `close` with unsaved changes and neither `save: true` nor `discard: true` is refused (`UNSAVED_CHANGES`) and the document stays open and dirty; tested in CI for LibreOffice and in `scripts/live_word_check.py` for Word.
- [ ] `capabilities` reports `engines: ["file", "live"]` with `live.apps` listing each app found and whether it is reachable (e.g. Word running, LibreOffice listening), and every live call on a machine without one returns `ENGINE_UNAVAILABLE` with the reason — never a traceback.
- [ ] Core stays stdlib-only: `import doc_control` and every file-engine operation work with neither `pywin32` nor `uno` installed (existing `smoke-no-network` job); live dependencies are an optional extra (`doc-control[live]`) imported lazily.
- [ ] `live_select_ms_p95 <= 500` for select-by-find on the 100k-word corpus novel in LibreOffice headless (CI), and the same budget reported (not gated) for Word.
- [ ] `live_lock_misreports == 0` over a fixture set of lock files (Word `~$name.docx`, LibreOffice `.~lock.name.docx#`, stale locks with no running app) and live sessions: `live.status` reports `open_in` (app, and whether this engine controls that window) correctly, and the file engine's `DOC_LOCKED` refusal recognises both lock-file kinds and names the app (today it checks Word's only).

## 2. Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| `doc_control.registry` / operation dispatch | Primary | Routes each call to the file or the live engine (`engine: "file" \| "live"`, default file) | Yes | New optional request field on the operations the live engine supports; others reject `engine: "live"` with `ENGINE_UNAVAILABLE` | Med |
| `doc_control.live` (new package) | Primary | Engine-neutral live protocol plus two adapters: `word_com` (pywin32) and `lo_uno` (LibreOffice UNO) | Yes | New code; no change to file-engine behaviour | Med |
| Addresses and anchors (`model/address`, `model/anchor`) | Dependent | Map `p142#a7b2…` and quote anchors onto the live document's paragraphs (index + fingerprint check, else anchor resolve on the live text) | Yes (adapter only) | Live paragraph enumeration must match file-engine numbering (tables, text boxes, footnotes excluded). The apps' paragraph text includes tracked deletions, which file-engine fingerprints exclude: the adapter must read the text without deletions (Word: a revisions-aware range view; LibreOffice: skip redline portions) or every paragraph with a tracked deletion reads as `stale` | High |
| `locate` find phrases (T54) | Dependent | `find_phrase` / `find_phrase_word` / `paragraphs_after` drive select-by-find | No (validation) | Must behave identically in Word and LibreOffice Find (LibreOffice has no `^p`: the adapter searches paragraph by paragraph instead). Word's Find takes 255 characters and also matches text inside tracked deletions: the adapter checks the found range against the expected paragraph and falls back to select by address | Med |
| Safety layer (`safety/lock`, backups, history) | Dependent | Lock detection reused for `open_in`; live writes are logged to history with the app's undo as the rollback | Yes | Live edits cannot be byte-verified like file edits; verification is by re-reading the saved file | Med |
| CLI, MCP server, `api.open` | UI | Same front doors; `api.open(path, engine="live")` attaches to the open window | Yes | Additive | Low |
| literary-critic `lc word` | Dependent consumer | Selects passages, adds comments and tracked edits in the writer's open document | No (validation) | Replaces its computer-use fallback | Med |
| CI | Infrastructure | LibreOffice headless job already exists (`libreoffice`); gains the live round-trip suite. `uno` is not on PyPI: it binds to LibreOffice's own or the system Python (`python3-uno`), so this job runs on the system interpreter, not the version matrix | Yes | +2–4 min per run | Low |
| Owner's Windows + Word machine | Infrastructure | Runs `scripts/live_word_check.py` and T30 | No | Manual evidence only | Low |

**Impact dimensions**: Data — live edits change the writer's open document, so every edit is tracked and every close refuses to drop unsaved work (High, mitigated by design). API — additive (`engine` field, `live.*` operations), no version bump. Performance — per-call IPC latency (COM/UNO) instead of file parsing; budget above. Operational — an optional extra and app setup (LibreOffice must listen on a socket). Risk of inaction — literary-critic keeps scripting computer-use clicks, which are slow and error-prone, and the writer must save/close/reopen to see agent changes.

## 3. Options

### Option A: Save-and-reload bridge (Conservative)

- **Description**: The "live" engine only asks the app to save, runs the existing file engine on the saved file, then asks the app to reload it. Selection is done by the app's own Find with the find phrase.
- **Scope**: `live/` with save/reload/select primitives per app; every edit reuses the file engine unchanged.
- **Effort**: Small.
- **Tradeoffs**: + every edit keeps file-engine guarantees (byte-identity, verification, undo log); + tiny per-app surface. − reload loses the writer's cursor, scroll position and app-level undo stack; − forces a save of the writer's in-progress work on every agent edit; − a reload while the writer types races with their edits.
- **Compatibility**: Fully additive.

### Option B: Native live adapters behind one protocol (Recommended)

- **Description**: A small engine-neutral protocol (`attach`, `paragraphs`, `select(range)`, `selection`, `find_select(phrase, paragraphs_after)`, `comment(range, text)`, `tracked_replace(range, text)`, `tracked_insert(after, text)`, `save`, `close(save|discard)`, `reopen`, `status`) implemented by a Word COM adapter and a LibreOffice UNO adapter. Registry operations (`comment.add`, `text.replace`, `para.insert`, and new `live.select`, `live.selection`, `live.save`, `live.close`, `live.open`, `live.status`) run on the live engine when `engine: "live"`. Track changes are switched on for the edit and restored after; the app's author name is set for the edit and restored after (it is a global setting in both apps); addresses are checked against the live paragraph text (fingerprint, deletions excluded) before any write. Selection uses index access into the document (Word `Paragraphs(n)` / ranges, LibreOffice paragraph enumeration cached per call), not a full walk per call, to meet the latency budget; Word COM calls retry when Word reports it is busy.
- **Scope**: `live/protocol.py`, `live/word_com.py`, `live/lo_uno.py`, dispatch in `registry.py`, schemas of the supported operations, `capabilities`, CLI/MCP flags, tests with LibreOffice headless, `scripts/live_word_check.py`.
- **Effort**: Large (Medium per adapter).
- **Tradeoffs**: + the writer keeps cursor, undo and unsaved state; edits appear instantly and are rejectable in the app; + the same operation names for agents; + LibreOffice adapter is testable in CI, Word adapter by script. − two IPC APIs to maintain; − no byte-level verification while the document is open (verified after save by re-reading); − paragraph numbering must be kept in step with the file engine's.
- **Compatibility**: Additive; `engine` defaults to `file`.

### Option C: Hybrid — native navigation, file-engine edits

- **Description**: Native adapters for select/read/save/close/status (Option B's navigation half); every edit goes through Option A's save → file engine → reload.
- **Scope**: Option B minus native edit primitives, plus Option A's reload path.
- **Effort**: Medium.
- **Tradeoffs**: + edits keep full file-engine guarantees; + navigation is live and cheap. − every edit still saves and reloads (cursor/undo lost, races with typing); − two code paths for one feature.
- **Compatibility**: Additive.

### Comparison

| | Option A | Option B | Option C |
|---|---|---|---|
| Effort | Small | Large | Medium |
| Risk to writer's work | Med (forced saves, reload races) | Low (tracked, no reload) | Med |
| Completeness (literary-critic asks) | Partial (no live selection state, reloads) | Full | Mostly |
| Compatibility | Additive | Additive | Additive |
| Maintenance | Low | Med–High (two IPC adapters) | Med |

## 4. Recommendation

**Recommended option**: Option B — it is the only option that leaves the writer's cursor, undo history and unsaved work untouched while showing agent edits as rejectable tracked changes at once, which is the point of a live engine; the LibreOffice adapter makes most of it testable in CI on Linux, and the Word adapter shares the protocol and the test script.

**Immediate next action**: on owner approval, `design` the protocol and the LibreOffice adapter first (testable here and in CI), starting with `live.status` / `live.open` / `live.select` / `live.selection` / `live.close`; the Word adapter follows on the owner's Windows session using the same protocol tests run through `scripts/live_word_check.py`.

**Phasing** (proposed for `design`; LibreOffice before Word departs from D-2 "Windows first" only in build order, because the LibreOffice adapter is testable here and in CI while Word needs the owner's machine):
1. Protocol, dispatch, `capabilities`, `live.status` (lock/`open_in`), LibreOffice attach/open/close/save with the unsaved-work guard.
2. LibreOffice select by address and by find phrase + `paragraphs_after`, read selection / paragraph at cursor.
3. LibreOffice `comment.add`, tracked `text.replace` / `para.insert`; round-trip gate in CI.
4. Word COM adapter for 1–3 (owner's Windows session), `live_word_failures == 0`.
5. Mac Word (AppleScript/JXA) — separate later spec (D-2).

**Open questions**:
- [ ] LibreOffice attach: LibreOffice accepts connections only when started with a listening socket (`--accept`), so a window the writer opened normally cannot be attached to (Word has no such limit). May doc-control start LibreOffice itself that way when asked to open a file, or must the writer always start it with a launcher (script / desktop shortcut)? Recommended: doc-control starts a listening instance when asked to open a file, and `live.status` explains how to relaunch for an already-open window.
- [ ] Which Word versions must pass: 2016, 2019, 2021, Microsoft 365 (current channel)? Recommended: 2016+ and M365, as for T30.
- [ ] Should live edits be recorded in doc-control's history log (with the app's own undo as rollback), or only in the app? Recommended: logged, marked `engine: live`, no byte backup until the next save.
- [ ] Author name on live comments and tracked changes: the app's user name, or a configured agent name (e.g. "Claude")? Recommended: configurable, default "Claude", as in the file engine.

**Decisions log** (from the core spec, carried unchanged):

| ID | Decision | Status | Date |
|----|----------|--------|------|
| D-2 | Live engine: Windows first, Mac later | agreed | 2026-10-12 |
| D-L1 | LibreOffice Writer (UNO) is a supported live app alongside Word, so the engine is testable on Linux CI | proposed | 2026-10-13 |
| D-L2 | Live dependencies are imported lazily and the core stays stdlib-only: `pywin32` as an optional extra (`doc-control[live]`); `uno` comes with LibreOffice (not installable from PyPI), so the LibreOffice adapter runs on a Python that can import it and reports `ENGINE_UNAVAILABLE` otherwise | proposed | 2026-10-13 |

---

> Sections 5–6 (contracts, risks) are appended by `design`.
