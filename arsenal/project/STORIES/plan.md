# Plan: every story — footnotes, endnotes, headers, footers and tables

**Date**: 2026-10-14
**Specification**: `arsenal/project/STORIES/spec.md` (approved r2; contracts §5, risks §6)
**Author**: Claude (doc-control session), for the owner
**Revision**: 1
**Status**: approved (2026-10-14, revision 1) — without annotations
**Revision log**:
- r1 — first draft

> Review record, kept the way the spec keeps its own: each round of notes bumps
> **Revision** with a log line naming the `<repo>-plan-v<N>-<date>-notes.md`
> export it applied, committed beside this file in the same commit. Approval is
> `approved (YYYY-MM-DD, revision N)`, plus `— without annotations` when there
> were none.

---

## Technical solution

### Architecture overview

A **story** is one text flow with its own paragraph numbering: the body, one footnote, one endnote, or one header or footer part. The work makes the existing body machinery take a story as a parameter instead of assuming `word/document.xml`:

- **Addresses** (`model/address.py`): an optional `<story>/` prefix. Without it the address means the body, exactly as today.
- **Stories** (new `model/stories.py`): resolves each story to its part and to the XML element that holds its paragraphs. Notes are found by `w:id` in `footnotes.xml` or `endnotes.xml`; headers and footers through each section's `w:headerReference` / `w:footerReference` and the document rels.
- **Index** (`model/index.py`, `ooxml/splice.py`): `scan` takes the container element (today hard-wired to `w:body`), so one `DocumentIndex` is built per story, lazily. The body index is built exactly as before.
- **Reads** (`ops/read.py`, `ops/locate.py`): walk the stories listed in `stories`. Notes are interleaved after the body paragraph that holds their reference mark when the read is in document order.
- **Writes** (`ops/tracked.py`, `ops/comments.py`): resolve the address's story, splice that part, and declare it in the footprint. `batch` groups edits per part and writes every part together through `write_verified`.
- **Safety** (`safety/verify.py`, `safety/history.py`): the footprint and the history entry carry part names. Body verification stays byte-identical in what it checks.
- **New ops**: `note.add`, `sections.list`, `table.get`, `table.row.insert`, `table.row.delete`, all in `OPERATION_MODULES`. The docs are regenerated.

### Data flow

1. A request carries a story address.
2. `parse_range` splits it into story and range.
3. `stories.resolve(doc, story)` gives the part and its container.
4. The index for that story resolves the range.
5. The tracked splice runs on that part's bytes.
6. `write_verified` gets `{part: new_bytes, …}` and a footprint naming each part.
7. History logs one entry with every part, and undo restores them together.

### State changes

| Service | Database | Change | Description |
|---------|----------|--------|-------------|
| `.docx` on disk | — | UPDATE | Only on an explicit write. Each write touches the parts it names: a story part, the comment parts, a notes part for `note.add`. |
| doc-control store | history log | UPDATE | Entries gain the parts touched. Old entries, which hold body changes only, are read as before. |

### Technology choices

| Choice | Justification |
|--------|--------------|
| Standard library only | The core's rule: no new dependency. |
| Reuse `scan`/`splice` with a container parameter instead of a second parser | One code path keeps the byte-exact splicing and its tests; the body is the default argument. |
| Note address by stored `w:id` (D-S2) | Stable while notes are added; the displayed number is returned as a field. |

### Out of scope

- The live engine in notes and headers (D-S4, I-42).
- Table columns, merges and widths (D-S5, I-41).
- Text boxes (I-40).
- Changing sections, page numbering, layout, TOC, indexes, captions or citations: S-9, the next batch. `sections.list` only reads.
- Comments inside comments, and glossary documents.

---

## Implementation tasks

| T# | Description | Service | Size | Depends | Gate | Tests |
|----|-------------|---------|------|---------|------|-------|
| T1 | Corpus kinds `notes` (footnotes, endnotes, restarts, a custom mark) and `headers` (default/first/even, an inherited header, a page-numbering restart); the LibreOffice validator compares note, header and footer text and section setup (surface: cloud) | tests/corpus | M | — | `story_fixture_failures == 0` | `test_story_kinds_round_trip_through_libreoffice` in `tests/corpus/test_validator.py` — each new kind converts and the validator reports no missing note, header or footer text |
| T2 | Story prefix in the address grammar (`fn<id>/`, `en<id>/`, `hdr<s>.<kind>/`, `ftr<s>.<kind>/`); cross-story ranges refused (surface: cloud) | model | S | — | `body_address_drift == 0` | `test_story_prefix_round_trips_and_body_unchanged` in `tests/model/test_address.py` — every story form parses and prints back unchanged, every 0.6.0 corpus body address is identical, and a cross-story range is `ADDRESS_INVALID` |
| T3 | `model/stories.py`: resolve stories to parts and containers; `scan` and `build_index` take a container; lazy per-story index (surface: cloud) | model, ooxml | M | T2 | `story_index_failures == 0` | `test_story_index_numbers_each_note_and_header` in `tests/model/test_stories.py` — every note, header and footer of the `notes` and `headers` kinds is indexed with its own numbering, and the body index equals 0.6.0's |
| T4 | Reads take `stories`: `paragraphs.list`, `find`, `locate`, `para.get`, `outline`, `stats` give `story`, `note`, `section`, `kind`, `used_by_sections`; body rows in tables get `cell` (surface: cloud) | ops | M | T1, T3 | `story_read_mismatches == 0` | `test_story_reads_match_libreoffice` in `tests/ops/test_story_reads.py` — every note, header and footer paragraph's text equals LibreOffice's, and each note's `of` is the body address holding its mark |
| T5 | Footprint and verification per part; history logs every part of a write; undo restores them together (surface: cloud) | safety | M | T3 | `unintended_changes == 0` | `test_story_part_footprint_rejects_outside_change` in `tests/safety/test_verify_parts.py` — a write that changes a byte outside the declared paragraphs of a story part is refused and restored, and the body checks are unchanged |
| T6 | Tracked `text.replace`, `para.insert`, `para.delete`, `comment.add`, `revision.accept`/`reject` in any story; `batch` across parts all or nothing; live returns `ENGINE_UNAVAILABLE` for story addresses (surface: cloud) | ops | L | T4, T5 | `story_edit_failures == 0` | `test_story_edits_round_trip_through_libreoffice` in `tests/ops/test_story_edits.py` — each edit kind in a footnote, an endnote, a header and a footer appears in LibreOffice as a revision or comment in that story, and accept, reject and undo all work |
| T7 | `note.add` (footnote or endnote at a body offset; mark and text as tracked insertions; creates the notes part when missing) (surface: cloud) | ops | M | T6 | `note_add_failures == 0` | `test_note_add_tracked_and_rejectable` in `tests/ops/test_note_add.py` — a new note appears in LibreOffice as a tracked insertion, rejecting removes the mark and the note, and undo restores the file byte for byte |
| T8 | `sections.list`: break type, title page, even/odd, page-number start and format, header/footer references per kind (read-only, groundwork for S-9) (surface: cloud) | ops | S | T1, T3 | `section_read_mismatches == 0` | `test_sections_list_matches_libreoffice` in `tests/ops/test_sections.py` — each section of the `headers` and `novel` kinds reports the setup LibreOffice reads |
| T9 | `table.get`: the grid with `gridSpan`/`vMerge` and paragraph addresses per cell (surface: cloud) | ops | M | T3 | `table_grid_mismatches == 0` | `test_table_grid_matches_libreoffice` in `tests/ops/test_tables.py` — every corpus table's grid equals LibreOffice's rows × cells, merges included |
| T10 | Tracked `table.row.insert` and `table.row.delete` (refused through a vertical merge) (surface: cloud) | ops | M | T5, T9 | `table_row_failures == 0` | `test_table_rows_tracked_round_trip` in `tests/ops/test_table_rows.py` — an inserted and a deleted row show in LibreOffice as tracked row changes, and accept, reject and undo all work |
| T11 | Performance check, README and docs regeneration, `scripts/real_word_stories.py` for the owner's Word check (surface: cloud) | docs, scripts | S | T6, T7, T8, T10 | `paragraphs_list_ratio <= 1.10` | `test_body_read_time_within_ratio` in `tests/bench/test_story_bench.py` — `paragraphs.list` on the corpus novel takes at most 1.10 × its 0.6.0 time |

**Status legend**: ☐ not started · ◐ in progress · ☑ merged

**Merge order**:
1. T1 and T2, in parallel.
2. T3.
3. T4, T5, T8 and T9, in parallel.
4. T6, then T7; T10 can start once T5 and T9 are in.
5. T11 last.

**Branch pattern**: `stories-T<N>-description` from the default branch.

### Dependency graph

```drawspec
{"version": 1, "kind": "flow", "title": "Task dependencies",
 "nodes": [
  {"id": "T1", "text": "T1\nCorpus kinds"},
  {"id": "T2", "text": "T2\nAddress prefix"},
  {"id": "T3", "text": "T3\nStory index"},
  {"id": "T4", "text": "T4\nStory reads"},
  {"id": "T5", "text": "T5\nPer-part safety"},
  {"id": "T6", "text": "T6\nStory edits"},
  {"id": "T7", "text": "T7\nnote.add"},
  {"id": "T8", "text": "T8\nsections.list"},
  {"id": "T9", "text": "T9\ntable.get"},
  {"id": "T10", "text": "T10\nRow ops"},
  {"id": "T11", "text": "T11\nPerf + docs"}
 ],
 "edges": [
  {"from": "T2", "to": "T3"},
  {"from": "T1", "to": "T4"},
  {"from": "T3", "to": "T4"},
  {"from": "T3", "to": "T5"},
  {"from": "T4", "to": "T6"},
  {"from": "T5", "to": "T6"},
  {"from": "T6", "to": "T7"},
  {"from": "T1", "to": "T8"},
  {"from": "T3", "to": "T8"},
  {"from": "T3", "to": "T9"},
  {"from": "T5", "to": "T10"},
  {"from": "T9", "to": "T10"},
  {"from": "T6", "to": "T11"},
  {"from": "T7", "to": "T11"},
  {"from": "T8", "to": "T11"},
  {"from": "T10", "to": "T11"}
 ]}
```

## Evidence log

| T# | Gate | Measured | Command | SHA | Env | Date |
|----|------|----------|---------|-----|-----|------|
| T1 | `story_fixture_failures == 0` | 0 | `uv run pytest -q tests/corpus/test_validator.py -k test_story_kinds_round_trip_through_libreoffice` (text of notes, headers and footers; section setup is compared in T8) | (this PR) | cloud container, LibreOffice 7.x headless | 2026-10-14 |
| T2 | `body_address_drift == 0` | 0 | `uv run pytest -q tests/model/test_address.py -k test_story_prefix_round_trips_and_body_unchanged` | (this PR) | cloud container, Python 3.13 | 2026-10-14 |
| T3 | `story_index_failures == 0` | 0 | `uv run pytest -q tests/model/test_stories.py -k test_story_index_numbers_each_note_and_header` | (this PR) | cloud container, Python 3.13 | 2026-10-14 |
| T4 | `story_read_mismatches == 0` | 0 | `uv run pytest -q tests/ops/test_story_reads.py -k test_story_reads_match_libreoffice` (`paragraphs.list`, `find`, `para.get` take `stories`; `locate`, `outline`, `stats` stay body-only: chapters, pages and counts are the body's) | (this PR) | cloud container, LibreOffice headless | 2026-10-14 |

---

## Sign-off

- [ ] Plan reviewed by the owner
- [ ] Contracts agreed with literary-critic (body rows unchanged; `stories` opt-in)
- [ ] Ready for execution
