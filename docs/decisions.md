# Decisions log (explore-idea, input for the spec)

| ID | Decision | Status | Date |
|---|---|---|---|
| D-1 | License: Apache-2.0 | agreed | 2026-10-12 |
| D-2 | Live engine: Windows first, Mac later | agreed | 2026-10-12 |
| D-3 | Workflow: claude-arsenal, gated tasks | agreed | 2026-10-12 |
| D-4 | CI on every PR; merge after CI and review | agreed | 2026-10-12 |
| D-5 | First spec: core + file engine; API designed for both engines | agreed | 2026-10-12 |
| D-6 | Name `doc-control` (import `doc_control`); owner reserves it on PyPI | agreed | 2026-10-12 |
| D-7 | Python >= 3.11 for the whole package (chat sandbox runs 3.11) | agreed | 2026-10-12 |
| D-8 | CLI first (JSON I/O); MCP is a thin wrapper generated from one operations schema | agreed | 2026-10-12 |
| D-9 | Addresses: position + fingerprint (`p142#a7b2`), `stale` on mismatch, old→new address map after structural edits; quote anchors for long-lived references | agreed | 2026-10-12 |
| D-10 | Revision/comment author configurable, default "Claude", optional per-call tag | agreed | 2026-10-12 |
| D-11 | Three layers: core primitives; bundled recipes (may use Python, tested in repo); user recipes on the user's side, written by the agent, declarative only | agreed | 2026-10-12 |
| D-12 | API version in every response + `capabilities` command + deprecation period; contract test suite once literary-critic integrates | agreed | 2026-10-12 |
| D-13 | Backups and undo log in `.doc-control/` next to the document; keep last 20 backups or 30 days (whichever more); setting to use the app-data folder instead | agreed | 2026-10-12 |
| D-14 | docx-fixer frozen now; archived at parity (rules ported as bundled recipes, its round-trip cases pass); reviewing tracked changes in Word with the agent replaces its review page | superseded by D-17 | 2026-10-12 |
| D-15 | Undo log is permanent and append-only (before/after of every changed paragraph); `restore` brings any logged paragraph back as a tracked change; backup on every open; history searchable by quote | agreed | 2026-10-12 |
| D-16 | Styles and templates in the core: list/read/create/update/apply/rename/merge styles; style sets as JSON (source of truth) with `.dotx` export/import; style application tracked, definition changes covered by dry run + backup + undo log | agreed | 2026-10-12 |
| D-17 | docx-fixer frozen now (owner is its only user); archived at parity: general rules bundled with passing round-trip cases, owner-specific rules re-created as user recipes / style sets on the owner's side; other common recipes bundled too; Word review replaces its review page | agreed | 2026-10-12 |
| D-18 | v1 serves literary-critic's indexing needs: layout facts in `para.get`/`outline`, normalised-text view with two-way offset map, structure detection with stable ids, signals and overrides, exact-only anchor mode with match kind, paragraph diff between document versions, unique-phrase locators | agreed | 2026-10-12 |
| D-19 | doc-control owns every generic primitive useful to control document files (e.g. comparing two documents, diffs, redlines), so literary-critic and other tools build on it; literary- or domain-specific logic stays in the consumers; more features can be added later | agreed | 2026-10-12 |
| D-20 | The CLI reads a request from stdin only with `--json -` (never by default), so an agent's open stdin pipe cannot hang it; a plain `< request.json` is not read | agreed | 2026-10-12 |
| D-21 | Plain writes, opt-in per call: `tracked: false` on `batch`, `term.rename` and `restore` (file and live engines) writes a change the writer already approved in the chat without revision marks. Built as the same tracked edits with exactly this call's own revisions accepted in the same verified write; a range overlapping another unresolved tracked change is refused. Backup, `expect.sha256`, verification, the log and `undo` are unchanged (undo works from the logged paragraph bytes). Tracked stays the default. Amends the core spec's "nothing changes without a visible revision" for callers that opt in (owner, 2026-10-15, asked by literary-critic) | agreed | 2026-10-15 |
| D-22 | `history.adopt`: a document moved or renamed without its store can take its orphan store back (moved to the document's current store location, an `adopt` entry logged with the old path); matched when the document's sha256 equals the store's last `sha256_after`, else only with `force`; `open` on a store-less document reports adoptable candidates. Until then, moving the `.doc-control/<name>/` folder along is the guidance (owner, 2026-10-15) | agreed | 2026-10-15 |

Clarification of D-13/D-15 (spec r2): retention prunes full backups only; undo history is removed only by an explicit `history purge`; orphaned app-data history is listed, never auto-deleted.

Direction approved by the owner on 2026-10-12; handed to `specify`.

## Owner to-dos

- Switch the GitHub default branch to `main` (UI error "Could not change default branch"; no rulesets exist). Remind when the owner is at a PC.
- Reserve `doc-control` on PyPI.
- Find a Windows machine with Word for live-engine testing.
