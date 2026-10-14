# Specification: every story — footnotes, endnotes, headers, footers and tables

**Date**: 2026-10-14
**Ticket / PR**: — (workspace `STORIES`)
**Author**: Claude (doc-control session), for the owner
**Revision**: 2
**Status**: approved (2026-10-14, revision 2)
**Revision log**:
- r1 — first draft, from sub-project S-1 of `status/ideas.md`
- r2 — applied `doc-control-spec-v1-2026-10-14-notes.md`: Option B; Q1–Q5 answered (D-S1..D-S5); section page setup read in scope (D-S6); the owner's layout ideas (numbering, two-sided printing, front matter, TOC/index, captions, citations) filed as S-9, I-34..I-42

> Review record. Each round of reviewer notes makes a new revision: bump
> **Revision**, add a log line naming the export it applied, and commit that
> export beside this file in the same commit. On approval set **Status** to
> `approved (YYYY-MM-DD, revision N)` and, when the reviewer sent no notes for
> that revision, add `— without annotations`.

---

## 1. Problem statement

doc-control reads and edits only the paragraphs of the main story (`word/document.xml`, `w:body`). Five other kinds of text are invisible or out of reach:

- **Footnotes and endnotes**: the text in `footnotes.xml` and `endnotes.xml` is never parsed. `stats` only counts the notes. `find` does not search them (`ops/locate.py` says so). Note marks add no character to the body text, and a write range that holds one is refused with `UNSUPPORTED_CONTENT`.
- **Headers and footers**: `header*.xml` and `footer*.xml` are counted, never read.
- **Tables**: cell paragraphs are numbered in document order with no row, column or table index. Rows cannot be added or removed.

An agent reviewing a thesis, a contract or a novel with notes cannot read, find, comment on or propose a tracked edit to a footnote, a running header or a table row. The writer then has to do it by hand, or the agent falls back to computer use.

The gap survey (`status/ideas.md`) ranks footnotes, and headers and footers, among the most-requested docx features (python-docx #1; the headers issue). literary-critic needs notes to judge a manuscript.

This spec covers reading, finding and tracked editing in those stories, plus a table structure, in the file engine. The safety guarantees stay as they are: verified writes, undo, and `unintended_changes == 0`.

**Ideas covered**: I-1, I-2, I-3 (sub-project S-1). Groundwork for S-9 (I-34, I-35): each section's page setup is read, not changed.

**Success criteria (measurable)**:

- [ ] `body_address_drift == 0`: every body address (`p<n>#<fp>`) that doc-control 0.6.0 returns for each corpus kind is the same after this change. Existing callers, literary-critic among them, keep working unchanged.
- [ ] `story_read_mismatches == 0` over every corpus kind plus new `notes` and `headers` kinds:
  - the text doc-control reads for each footnote, endnote, header and footer paragraph equals the text LibreOffice exports for it, compared the way the round-trip validator already compares body text;
  - each note is matched to the body paragraph that holds its reference mark.
- [ ] `story_edit_failures == 0`, measured by a round trip (write with doc-control, open in headless LibreOffice, read back with `revisions.list` and `comments.list`) for each of these, in a footnote, an endnote, a header and a footer:
  - a `text.replace`, `para.insert` and `comment.add`;
  - `revision.accept` and `revision.reject`;
  - `undo`.

  Each revision shows up in LibreOffice in the right story, and undo restores the file byte for byte.
- [ ] `unintended_changes == 0` for every new write: the verification footprint names the story part that was touched, and every other zip member, and every byte of that part outside the touched paragraphs, stays identical.
- [ ] `table_grid_mismatches == 0`: for each table in the corpus, `table.get` returns a grid (rows × cells, with each cell's paragraph addresses and merges) equal to LibreOffice's table structure.
- [ ] `note_add_failures == 0`: `note.add` at a body address inserts a tracked reference mark and a new footnote or endnote. LibreOffice shows both as tracked insertions; rejecting the insertion removes both, and undo restores the file byte for byte.
- [ ] `section_read_mismatches == 0`: for each section of the corpus documents, `sections.list` reports the same page setup as LibreOffice:
  - which header and footer kinds are set (default, first, even), and whether "different first page" and "different odd and even" are on;
  - the page-number start and format;
  - the section break type.
- [ ] `table_row_failures == 0`: a tracked `table.row.insert` and `table.row.delete` on corpus tables appear in LibreOffice as a tracked row insertion or deletion, and accept, reject and undo all work.
- [ ] `paragraphs_list_ms` on the corpus novel (body only, the default) is at most 1.10 × its 0.6.0 value. Reading every story is opt-in and is reported, not gated.
- [ ] Judged, not measured: the owner opens one output of each kind in Word on Windows (`scripts/live_word_check.py`, or a new `scripts/real_word_stories.py`). There is no repair prompt, and each revision and comment can be accepted, rejected and resolved there.

## 2. Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| `model/address.py` | Primary | Address grammar and fingerprints | Yes | A story prefix is added (see options). Body addresses keep their exact form. | High |
| `model/index.py`, `ooxml/splice.py` | Primary | Paragraph index over `w:body` | Yes | Indexes any story part's paragraphs. Table cells get `(table, row, col)`. | High |
| `model/document.py` | Primary | Opens the package; `DOCUMENT_PART` is hard-coded | Yes | Parts are resolved through relationships (footnotes, endnotes, each header and footer). | Med |
| `ops/tracked.py`, `ops/comments.py` | Primary | Tracked edits and comments | Yes | The target part comes from the address, no longer the constant `DOCUMENT_PART`. Comments get anchors in note and header parts. | High |
| `ooxml/revisions.py` | Primary | Revision markup and ids | Yes | Ids are already unique across `STORY_PART`. A note mark inside a range is still refused for a body edit; editing inside the note itself becomes possible. | Med |
| `safety/verify.py` | Primary | Footprint verification | Yes | `_document_problems` takes the touched part as an argument and drops the hard-coded `DOCUMENT_PART`. | High |
| New ops `note.add`, `sections.list` | Primary | Add a note; read each section's page setup | Yes (new) | `sections.list` is read-only; its writes come in S-9. | Med |
| `safety/history.py` | Primary | Undo log | Yes | Logs and undoes changes in any story part (today it refuses with "change … is in <part>"). | Med |
| `ops/read.py`, `ops/locate.py` (`paragraphs.list`, `find`, `locate`, `stats`, `outline`) | Dependent | Reads | Yes | An optional `stories` field (default `["body"]`). Rows from other stories carry `story` and, for notes, `note_of` (the body address of the reference). | Med |
| New ops `table.get`, `table.row.insert`, `table.row.delete` | Primary | Table structure | Yes (new) | New registry entries; docs regenerated. | Med |
| `docs/operations.md`, MCP tools | Dependent | Generated from the registry | Regenerate | New fields and ops appear as MCP tools automatically. | Low |
| Live engine (`live/*`) | Dependent | Body-only protocol | No (this spec) | Live calls with a story address answer `ENGINE_UNAVAILABLE` with the reason. | Low |
| `tests/corpus` generator and validator | Shared resource | Corpus kinds and LibreOffice round trip | Yes | New `notes` and `headers` kinds, plus notes and header/footer text in the validator's comparison. | Med |
| literary-critic | Consumer | Reads body addresses | Validation only | Body addresses unchanged; note text becomes available on request. | Low |

**Impact**:
- **Data**: none at rest. Documents are only changed by explicit writes, as today.
- **API**: additive. New optional fields and ops, and a new address prefix that today's callers never send.
- **Performance**: body reads unchanged by default; the other stories are read only when asked for.
- **Risk of inaction**: high for academic, legal and publishing users, who can't be served at all. Footnotes are python-docx's oldest open issue (#1).

## 3. Options

### Option A: Separate operations per story (Conservative)

- **Description**: leave every existing op body-only. Add `notes.list`, `note.get`, `note.replace`, `note.comment`, `headers.list`, `header.replace`, and so on, each with its own small address form (`n3` for footnote 3, `h1` for header 1), and add `table.get`.
- **Scope**: new ops only; the index and verification change only as far as the new ops need.
- **Effort**: Medium.
- **Tradeoffs**: zero risk to body callers. But it doubles the surface: every new feature (recipes, find, anchors, batch, undo) would need a per-story twin, and `batch` could not mix a body edit with a note edit, all or nothing.
- **Compatibility**: fully backwards compatible.

### Option B: Story-prefixed addresses through the existing operations (Recommended)

- **Description**:
  - Keep `p<n>#<fp>` meaning a body paragraph.
  - Add one prefix for the other stories: `fn<k>/p<n>#<fp>` (footnote k, its paragraph n), `en<k>/…` (endnote), and `hdr<s>.<type>/…` or `ftr<s>.<type>/…` (section s; type `default`, `first` or `even`). `<k>` is the note's id as Word stores it, so it stays stable while other notes are added or removed.
  - Every read and write op that takes an address takes these, and `batch` stays all or nothing across parts.
  - Reads gain `stories: ["body", "notes", "headers"]`.
  - Table cells get a `cell: {table, row, col}` field on paragraph rows, plus `table.get` and tracked `table.row.insert` / `table.row.delete`.
- **Scope**: everything in the §2 table.
- **Effort**: Large. Suggested split into tasks: part-aware index, then reads, then writes and verification, then undo, then tables.
- **Tradeoffs**: one surface for every story, so recipes, find, anchors and batch work in notes for free. The index and verification layers need deeper changes, which risks regressions; `body_address_drift == 0` and the existing gates guard against that.
- **Compatibility**: backwards compatible. Body addresses, response shapes and defaults are unchanged.

### Option C: One global paragraph numbering over all stories

- **Description**: number every paragraph of every story in a single sequence, in reading order (the body with each note after its reference).
- **Scope**: the same as B, plus every consumer.
- **Effort**: Large.
- **Tradeoffs**: one simple number. But body addresses shift whenever a note is added, which breaks the stable-address promise and every stored address in literary-critic.
- **Compatibility**: breaking; rejected for that reason.

### Comparison

| | Option A | Option B | Option C |
|---|---|---|---|
| Effort | Medium | Large | Large |
| Risk | Low | Medium (index and verify changes) | High (address break) |
| Completeness | Partial: no batch across stories, no recipes or anchors in notes | Full | Full |
| Compatibility | Full | Full | Breaking |
| Maintenance | High (per-story twin ops) | Low (one path) | Medium |

## 4. Recommendation

**Recommended option**: Option B. One address grammar with a story prefix keeps every existing op, recipe and `batch` working in notes and headers, without breaking any stored body address. Option A would need a twin of every future feature, and Option C breaks the stable-address promise.

**Immediate next action**: after approval, `design` writes the contracts and a plan. Suggested order:
1. Corpus `notes` and `headers` kinds (headers with first/even variants and a numbering restart), and the validator comparing their text and section setup (the gates need them first).
2. Part-aware index and reads with `stories`.
3. Story-prefixed writes with the generalised footprint.
4. Undo in any part.
5. `note.add`.
6. `sections.list`.
7. `table.get`, then the tracked row ops.

**Open questions**: none. The owner answered Q1–Q5 in r2; see D-S1..D-S5.

**Kept for later** (in `status/ideas.md`): notes and headers in the live engine (I-42); table columns, merges and widths (I-41); text boxes (I-40); controlling sections, page numbering, two-sided layout, front matter, TOC, indexes, captions and citations (S-9, I-34..I-39).

**Decisions log** (D-1 and D-2 from `status/ideas.md`; D-S rows from the owner's r2 notes):

| ID | Decision | Status | Date |
|----|----------|--------|------|
| D-1 | Which sub-project to explore first: S-1 | agreed | 2026-10-14 |
| D-2 | S-1 reads section page setup; changing it is S-9 | agreed | 2026-10-14 |
| D-S1 | Option B: story-prefixed addresses through the existing operations | agreed | 2026-10-14 |
| D-S2 | Note addresses use Word's stored note id; the displayed number is a returned field | agreed | 2026-10-14 |
| D-S3 | Adding a new footnote or endnote (`note.add`) is in scope | agreed | 2026-10-14 |
| D-S4 | Live engine stays body-only here; notes and headers live are I-42 | agreed | 2026-10-14 |
| D-S5 | Tables: rows only; columns, merges and widths are I-41. Text boxes are I-40 | agreed | 2026-10-14 |
| D-S6 | `sections.list` (read-only page setup per section) is in scope as groundwork for S-9 | agreed | 2026-10-14 |

---

> Sections 5–6 (contracts, risks) are appended by `design`.

## 5. Contracts

All changes are additive to the operation registry. CLI, MCP and Python get them the same way, through the JSON Schemas generated from the registry. No database or migration is involved.

### Addresses (`model/address.py`)

```text
p12#a7b2a7b2                    body paragraph 12 (unchanged)
fn3/p1#0c9e0c9e:4-10            footnote with w:id 3, its paragraph 1, chars 4-10
en2/p1#…                        endnote with w:id 2
hdr1.default/p1#…               the header section 1 defines for the default pages
ftr2.first/p1#…                 the footer section 2 defines for its first page
fn3/p1#…:2..fn3/p2#…:5          a range stays inside one story
```

- `<story>/` is optional. Its absence means the body, so every 0.6.0 address parses to the same `Address` and prints byte for byte the same (`body_address_drift == 0`).
- `n` counts paragraphs within the story.
- A range whose two ends are in different stories is `ADDRESS_INVALID`.
- A header or footer that a section inherits (a section with no `w:headerReference` of that kind) is addressed through the section that defines it. Reads report `used_by_sections`.

### Read results

`paragraphs.list`, `find`, `locate`, `para.get`, `outline` and `stats` take `stories` (an array of `"body"`, `"notes"`, `"headers"`; default `["body"]`). Rows from other stories add three fields:

```json
{"at": "fn3/p1#0c9e0c9e", "story": "footnote", "note": {"id": 3, "number": "2", "of": "p40#c0ffee00"},
 "text": "Ibid., p. 12.", "style_name": "Footnote Text"}
{"at": "hdr1.default/p1#5e5e5e5e", "story": "header", "section": 1, "kind": "default",
 "used_by_sections": [1, 2], "text": "Chapter title"}
```

`note.number` is the number the writer sees, which shifts as notes are added. `note.of` is the body address of the reference mark. Body rows inside a table add `"cell": {"table": 2, "row": 1, "col": 3}` (1-based, in document order).

### New operations

| Op | Kind | Request | Result |
|----|------|---------|--------|
| `note.add` | write (batchable) | `{"at": "p40#c0ffee00:17", "kind": "footnote" \| "endnote", "text": "…"}` | `{"note": {"id": 7, "number": "3", "at": "fn7/p1#…"}}`. The reference mark and the note's text are both tracked insertions. |
| `sections.list` | read | `{"doc": "…"}` | `[{"section": 1, "break": "nextPage", "title_page": true, "even_and_odd": false, "page_numbers": {"start": 1, "format": "lowerRoman"}, "headers": {"default": "hdr1.default", "first": null, "even": null}, "footers": {…}}]` (`"even_and_odd"` is a document-wide setting, repeated on each row) |
| `table.get` | read | `{"doc": "…", "table": 2}` or `{"at": "<any address in it>"}` | `{"table": 2, "rows": [[{"col": 1, "span": 1, "vmerge": null, "paragraphs": ["p31#…"]}, …], …]}` |
| `table.row.insert` | write (batchable) | `{"table": 2, "after_row": 3, "cells": ["a", "b", "c"]}` | A tracked row insertion (`w:trPr/w:ins`) with tracked runs. |
| `table.row.delete` | write (batchable) | `{"table": 2, "row": 4}` | A tracked row deletion (`w:trPr/w:del`) with tracked deleted runs. |

`revision.accept` and `revision.reject` select by ids, author, range or all, as today; a range may now be a story range. `revisions.list` and `comments.list` rows gain `story` and use story addresses.

### Errors (existing codes, no new ones)

- `ADDRESS_INVALID`: a malformed prefix, or a cross-story range.
- `STALE_ADDRESS`: the story or the paragraph changed.
- `ANCHOR_NOT_FOUND`: no such note id, section or table.
- `UNSUPPORTED_CONTENT`: a body edit range that contains a note mark (unchanged), an edit in a text box, or a row op on a table with a vertical merge crossing that row.
- `ENGINE_UNAVAILABLE`: any story address sent with `engine: "live"` (D-S4), with the reason "notes and headers are file engine only".

### Safety contracts

- `safety/verify.Footprint` gains a `part` (default `word/document.xml`). `_document_problems` checks that part.
- `note.add` and the row ops declare the extra parts they touch: `footnotes.xml` or `endnotes.xml`, plus the rels and `[Content_Types].xml` only when they create the notes part.
- `safety/history` logs the part for each change, and `undo` restores it. The refusal "change … is in <part>" stays only for parts outside `STORY_PART`.

### Inter-component contracts

| Caller | Callee | Protocol | Contract | Failure handling |
|--------|--------|----------|----------|-----------------|
| literary-critic | doc-control CLI/MCP | JSON in/out | Body rows unchanged. `stories` opts in to notes. | A new field it ignores is harmless. |
| `ops/live.py` | `live/*` | Python | Body spans only (unchanged). | A story address answers `ENGINE_UNAVAILABLE`. |

### Database migrations

| Service | Database | Change | Reversible | Forward-compatible |
|---------|----------|--------|------------|-------------------|
| — | none | — | yes | yes |

## 6. Risks & Validation

| Risk | Likelihood | Impact | Mitigation | Validation |
|------|-----------|--------|------------|------------|
| Generalising the index or verify shifts body addresses or loosens the body check | Med | High | The body path keeps its code; the part becomes a parameter defaulting to the body. `body_address_drift` is gated on the whole corpus and the private corpus. | unit + corpus gate |
| Word repairs a file with a tracked note or row insertion that LibreOffice accepts | Med | High | Follow the OOXML rules from `docs/research`. Note `w:id`s stay unique across notes and the separator ids (-1, 0) are kept. Owner's real-Word check. | integration (LibreOffice) + manual (Word) |
| Shared headers: editing a header used by several sections changes all of them, which surprises the writer | Med | Med | Reads report `used_by_sections`; write results echo it so the agent can tell the writer. | unit |
| A note's displayed number is computed wrongly (restarts per section, custom marks, `w:customMarkFollows`) | Med | Low | Mark the number advisory; the stored id is the address (D-S2). Test restarts and custom marks. | unit + LibreOffice compare |
| Table grids with merged cells (`gridSpan`, `vMerge`) disagree with LibreOffice | Med | Med | Compare grids on the corpus. Refuse row ops through a vertical merge (`UNSUPPORTED_CONTENT`). | integration |
| Reading every story slows reads on long documents | Low | Med | Opt-in `stories`; body-only default; `paragraphs_list_ms` ratio gated. | perf |
| Undo across parts restores one part but not another in a multi-part batch | Low | High | History records every touched part of a batch as one entry; undo restores them together or refuses. | unit (byte compare) |
