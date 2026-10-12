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
| D-14 | docx-fixer frozen now; archived at parity (rules ported as bundled recipes, its round-trip cases pass); reviewing tracked changes in Word with the agent replaces its review page | agreed | 2026-10-12 |
| D-15 | Undo log is permanent and append-only (before/after of every changed paragraph); `restore` brings any logged paragraph back as a tracked change; backup on every open; history searchable by quote | agreed | 2026-10-12 |
| D-16 | Styles and templates in the core: list/read/create/update/apply/rename/merge styles; style sets as JSON (source of truth) with `.dotx` export/import; style application tracked, definition changes covered by dry run + backup + undo log | agreed | 2026-10-12 |

Direction approved by the owner on 2026-10-12; handed to `specify`.

## Owner to-dos

- Switch the GitHub default branch to `main` (UI error "Could not change default branch"; no rulesets exist). Remind when the owner is at a PC.
- Reserve `doc-control` on PyPI.
- Find a Windows machine with Word for live-engine testing.
