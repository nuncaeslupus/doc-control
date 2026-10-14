# Doc Control — Specification (annotated edition)

> Generated 2026-10-14. This is the document with a **note slot** after every section that has text of its own. Read it in any Markdown app. To annotate, replace the `_(your notes…)_` placeholder under any section. When done, send the file back — notes are acted on.

---

# Spec

## Preamble & scope

**Date**: 2026-10-14
**Ticket / PR**: — (workspace `STORIES`)
**Author**: Claude (doc-control session), for the owner
**Revision**: 1
**Status**: draft
**Revision log**:
- r1 — first draft, from sub-project S-1 of `status/ideas.md`

> Review record. Each round of reviewer notes makes a new revision: bump
> **Revision**, add a log line naming the export it applied, and commit that
> export beside this file in the same commit. On approval set **Status** to
> `approved (YYYY-MM-DD, revision N)` and, when the reviewer sent no notes for
> that revision, add `— without annotations`.

<!-- -->

> **✎ Notes** · `SPEC · intro`
> _(your notes here — replace this line)_

## §1 Problem statement

doc-control reads and edits only the paragraphs of the main story (`word/document.xml`, `w:body`). Five other kinds of text are invisible or out of reach:

- **Footnotes and endnotes**: the text in `footnotes.xml` and `endnotes.xml` is never parsed. `stats` only counts the notes. `find` does not search them (`ops/locate.py` says so). Note marks add no character to the body text, and a write range that holds one is refused with `UNSUPPORTED_CONTENT`.
- **Headers and footers**: `header*.xml` and `footer*.xml` are counted, never read.
- **Tables**: cell paragraphs are numbered in document order with no row, column or table index. Rows cannot be added or removed.

An agent reviewing a thesis, a contract or a novel with notes cannot read, find, comment on or propose a tracked edit to a footnote, a running header or a table row. The writer then has to do it by hand, or the agent falls back to computer use.

The gap survey (`status/ideas.md`) ranks footnotes, and headers and footers, among the most-requested docx features (python-docx #1; the headers issue). literary-critic needs notes to judge a manuscript.

This spec covers reading, finding and tracked editing in those stories, plus a table structure, in the file engine. The safety guarantees stay as they are: verified writes, undo, and `unintended_changes == 0`.

**Ideas covered**: I-1, I-2, I-3 (sub-project S-1).

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
- [ ] `table_row_failures == 0`: a tracked `table.row.insert` and `table.row.delete` on corpus tables appear in LibreOffice as a tracked row insertion or deletion, and accept, reject and undo all work.
- [ ] `paragraphs_list_ms` on the corpus novel (body only, the default) is at most 1.10 × its 0.6.0 value. Reading every story is opt-in and is reported, not gated.
- [ ] Judged, not measured: the owner opens one output of each kind in Word on Windows (`scripts/live_word_check.py`, or a new `scripts/real_word_stories.py`). There is no repair prompt, and each revision and comment can be accepted, rejected and resolved there.

<!-- -->

> **✎ Notes** · `SPEC §1`
> _(your notes here — replace this line)_

## §2 Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| `model/address.py` | Primary | Address grammar and fingerprints | Yes | A story prefix is added (see options). Body addresses keep their exact form. | High |
| `model/index.py`, `ooxml/splice.py` | Primary | Paragraph index over `w:body` | Yes | Indexes any story part's paragraphs. Table cells get `(table, row, col)`. | High |
| `model/document.py` | Primary | Opens the package; `DOCUMENT_PART` is hard-coded | Yes | Parts are resolved through relationships (footnotes, endnotes, each header and footer). | Med |
| `ops/tracked.py`, `ops/comments.py` | Primary | Tracked edits and comments | Yes | The target part comes from the address, no longer the constant `DOCUMENT_PART`. Comments get anchors in note and header parts. | High |
| `ooxml/revisions.py` | Primary | Revision markup and ids | Yes | Ids are already unique across `STORY_PART`. A note mark inside a range is still refused for a body edit; editing inside the note itself becomes possible. | Med |
| `safety/verify.py` | Primary | Footprint verification | Yes | `_document_problems` takes the touched part as an argument and drops the hard-coded `DOCUMENT_PART`. | High |
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

<!-- -->

> **✎ Notes** · `SPEC §2`
> _(your notes here — replace this line)_

## §3 Options

### Option A: Separate operations per story (Conservative)

- **Description**: leave every existing op body-only. Add `notes.list`, `note.get`, `note.replace`, `note.comment`, `headers.list`, `header.replace`, and so on, each with its own small address form (`n3` for footnote 3, `h1` for header 1), and add `table.get`.
- **Scope**: new ops only; the index and verification change only as far as the new ops need.
- **Effort**: Medium.
- **Tradeoffs**: zero risk to body callers. But it doubles the surface: every new feature (recipes, find, anchors, batch, undo) would need a per-story twin, and `batch` could not mix a body edit with a note edit, all or nothing.
- **Compatibility**: fully backwards compatible.

<!-- -->

> **✎ Notes** · `SPEC › Option A: Separate operations per story (Conservative)`
> _(your notes here — replace this line)_

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

<!-- -->

> **✎ Notes** · `SPEC › Option B: Story-prefixed addresses through the existing operations (Recommended)`
> _(your notes here — replace this line)_

### Option C: One global paragraph numbering over all stories

- **Description**: number every paragraph of every story in a single sequence, in reading order (the body with each note after its reference).
- **Scope**: the same as B, plus every consumer.
- **Effort**: Large.
- **Tradeoffs**: one simple number. But body addresses shift whenever a note is added, which breaks the stable-address promise and every stored address in literary-critic.
- **Compatibility**: breaking; rejected for that reason.

<!-- -->

> **✎ Notes** · `SPEC › Option C: One global paragraph numbering over all stories`
> _(your notes here — replace this line)_

### Comparison

| | Option A | Option B | Option C |
|---|---|---|---|
| Effort | Medium | Large | Large |
| Risk | Low | Medium (index and verify changes) | High (address break) |
| Completeness | Partial: no batch across stories, no recipes or anchors in notes | Full | Full |
| Compatibility | Full | Full | Breaking |
| Maintenance | High (per-story twin ops) | Low (one path) | Medium |

<!-- -->

> **✎ Notes** · `SPEC › Comparison`
> _(your notes here — replace this line)_

## §4 Recommendation

**Recommended option**: Option B. One address grammar with a story prefix keeps every existing op, recipe and `batch` working in notes and headers, without breaking any stored body address. Option A would need a twin of every future feature, and Option C breaks the stable-address promise.

**Immediate next action**: after approval, `design` writes the contracts and a plan. Suggested order:
1. Corpus `notes` and `headers` kinds, and the validator comparing their text (the gates need them first).
2. Part-aware index and reads with `stories`.
3. Story-prefixed writes with the generalised footprint.
4. Undo in any part.
5. `table.get`, then the tracked row ops.

**Open questions**:
- [ ] Q1. Address prefix shape. Proposed: `fn3/p1#fp`, `en2/p1#fp`, `hdr1.default/p1#fp`. Note numbers could be Word's stored id (stable) or the displayed number (what the writer sees, but it shifts when a note is added). Recommendation: the stored id in the address, with the displayed number returned as a field.
- [ ] Q2. Should adding a new footnote or endnote at a body address (`note.add`, a tracked insertion of the mark plus the note) be in this spec, or stay as I-12? Recommendation: in scope, because it is the commonest academic ask and reuses the same part machinery.
- [ ] Q3. Live engine. Keep it body-only here (story addresses answer `ENGINE_UNAVAILABLE`), and make a follow-up task for reading and editing notes in LibreOffice and Word? Recommendation: yes, keep it out of this spec.
- [ ] Q4. Table scope beyond rows: column insert/delete, cell merge and widths. Recommendation: out of scope. Rows cover the common contract and report edits, and column changes are not tracked reliably across Word versions.
- [ ] Q5. Text boxes (`depth > 0` paragraphs, skipped today). Recommendation: out of scope; add an idea to the backlog instead.

**Decisions log** (from `status/ideas.md`; none agreed for S-1 yet):

| ID | Decision | Status | Date |
|----|----------|--------|------|
| D-1 | Which sub-project to explore first | open (the owner asked for this spec, which implies S-1, but has not confirmed it) | 2026-10-14 |


> Sections 5–6 (contracts, risks) are appended by `design`.

<!-- -->

> **✎ Notes** · `SPEC §4`
> _(your notes here — replace this line)_

