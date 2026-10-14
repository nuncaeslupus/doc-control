# Plan: page layout and long-document structure

**Date**: 2026-10-14
**Specification**: `arsenal/project/LAYOUT/spec.md` (approved r2; contracts §5, risks §6)
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

The work has three layers, each on top of STORIES (story index, `sections.list`, per-part safety):

1. **Page-label checker** (tests only): LibreOffice exports a scenario to PDF, `pdftotext` gives each page's text, and the printed header/footer number is compared with the plan. Every later gate uses it.
2. **Page plan** (`ops/layout.py`):
   - `layout.get` builds the plan from `sections.list` and the header/footer stories.
   - `layout.apply` diffs the requested plan against the current one into a minimal list of changes:
     - insert or remove a section break, the `w:sectPr` in the last paragraph of a section;
     - set section properties with `w:sectPrChange`;
     - set header/footer text through the STORIES tracked edits;
     - create header/footer parts, which Word cannot track, so this is refused without `untracked: true` (D-L2);
     - set `settings.xml` flags.
   - All changes are written through one `write_verified` call with a layout footprint.
3. **Fields** (`ops/fields.py`): `fields.list` over every story; `toc.insert`, `caption.add`, `xref.add`, `index.mark`, `index.insert` as tracked insertions of field code plus a placeholder result; `fields.update`, which converts a copy with headless LibreOffice, reads the new results and splices only the result runs back as tracked changes.

### Data flow

- **`layout.apply`**:
  1. Validate the plan.
  2. Resolve each `from` address.
  3. Diff the plan against `layout.get`.
  4. Turn the diff into changes per part.
  5. Write them with `write_verified`.
  6. Log one history entry.
  7. Check that `layout.get` equals the plan.
- **`fields.update`**:
  1. Copy the document to a temp directory and convert it with `soffice --convert-to docx`.
  2. Index both files and pair the fields by order and code.
  3. Write the changed results into the original as tracked replacements.

### State changes

| Service | Database | Change | Description |
|---------|----------|--------|-------------|
| `.docx` on disk | — | UPDATE | Only on explicit writes: section properties, settings flags, header/footer parts, field runs |
| doc-control store | history log | UPDATE | One entry per write with every part it touched |

### Technology choices

| Choice | Justification |
|--------|--------------|
| Standard library only in `doc_control` | The core's rule. LibreOffice is an optional external program for `fields.update`, as for the live engine. |
| `pdftotext` (poppler-utils) in tests only | Reads per-page text from LibreOffice's PDF for the page-label gate; never imported by the package |
| Plan diffing instead of imperative section ops | Hides Word's inheritance rules in one tested place (D-L1) |

### Out of scope

- Generating citations (D-L4).
- Front-matter templates (D-L5, in consumers).
- Page breaking by text flow.
- Columns, line numbering and page borders.
- The live engine.

---

## Implementation tasks

| T# | Description | Service | Size | Depends | Gate | Tests |
|----|-------------|---------|------|---------|------|-------|
| T1 | Page-label checker: scenario fixtures (front matter unnumbered then 1, roman then arabic, counted unnumbered part page, restart per part, recto chapter starts, two-sided mirrored headers) built by a small fixture writer; LibreOffice PDF export plus `pdftotext`; `poppler-utils` in the CI `libreoffice` job (surface: cloud) | tests | M | — | `page_label_checker_failures == 0` | `test_page_labels_of_handmade_scenarios` in `tests/layout/test_page_labels.py` — each hand-written scenario's printed page labels equal its expected list, and a mislabelled fixture is reported |
| T2 | `layout.get`: the page plan from `sections.list` and header/footer stories, placeholders recognised (`PAGE`, `NUMPAGES`, `STYLEREF`) (surface: cloud) | ops | M | — | `layout_get_mismatches == 0` | `test_layout_get_reads_every_scenario` in `tests/layout/test_layout_get.py` — the plan read from each scenario equals the plan it was written from |
| T3 | Section and settings writers: `w:sectPr` changes with `w:sectPrChange`, section break insert/remove, `settings.xml` flags; layout footprint family (surface: cloud) | ooxml, safety | M | — | `layout_untracked_changes == 0` | `test_section_props_change_is_tracked_and_rejectable` in `tests/layout/test_section_writer.py` — every property change appears in `revisions.list` and rejecting it restores the original bytes of the section |
| T4 | `layout.apply` for breaks and numbering (start, format, shown), recto starts, `dry_run`, all or nothing (surface: cloud) | ops | L | T1, T2, T3 | `page_label_mismatches == 0` | `test_apply_numbering_scenarios_print_planned_labels` in `tests/layout/test_layout_apply.py` — applying each numbering scenario to a plain document prints the planned labels in LibreOffice's PDF and `layout.get` returns the plan |
| T5 | `layout.apply` for headers and footers per kind, first page, two-sided (`evenAndOddHeaders`, `mirrorMargins`), placeholders; new parts refused unless `untracked: true` (surface: cloud) | ops | L | T4 | `page_label_mismatches == 0` | `test_apply_two_sided_headers_print_planned_text` in `tests/layout/test_layout_apply.py` — odd and even pages print their planned header and footer text and numbers |
| T6 | `fields.list` over all stories; citation fields kept intact by every write op (surface: cloud) | ops | M | — | `field_loss == 0` | `test_write_ops_never_change_citation_fields` in `tests/layout/test_fields.py` — after every batchable op on a document with Zotero and Word citations, their field codes and results are byte-identical |
| T7 | `toc.insert` and `fields.update` through LibreOffice; stale fields reported without it (surface: cloud) | ops | L | T6 | `toc_page_mismatches == 0` | `test_toc_pages_match_rendered_pages` in `tests/layout/test_toc.py` — after `fields.update`, each TOC entry's page number equals the page LibreOffice renders its heading on |
| T8 | `caption.add`, `xref.add` (REF/PAGEREF), `index.mark`/`index.insert` (surface: cloud) | ops | M | T7 | `xref_page_mismatches == 0` | `test_captions_xrefs_and_index_resolve_after_update` in `tests/layout/test_xrefs.py` — after `fields.update`, every cross-reference shows its target's number and page, and every index entry its page |
| T9 | Docs, README example of a front-matter plan, `scripts/real_word_layout.py` for the owner's Word check (surface: cloud) | docs, scripts | S | T5, T8 | `layout_docs_examples_failing == 0` | `test_readme_layout_example_runs` in `tests/test_docs.py` — the README's plan example applies cleanly to a corpus document and `layout.get` returns it |

**Status legend**: ☐ not started · ◐ in progress · ☑ merged

**Merge order**:
1. STORIES T8 (`sections.list`) first.
2. Then T1, T2, T3 and T6, in parallel.
3. T4, then T5.
4. T7, then T8.
5. T9 last.

**Branch pattern**: `layout-T<N>-description` from the default branch.

### Dependency graph

```drawspec
{"version": 1, "kind": "flow", "title": "Task dependencies",
 "nodes": [
  {"id": "T1", "text": "T1\nPage-label checker"},
  {"id": "T2", "text": "T2\nlayout.get"},
  {"id": "T3", "text": "T3\nSection writer"},
  {"id": "T4", "text": "T4\nNumbering apply"},
  {"id": "T5", "text": "T5\nHeaders apply"},
  {"id": "T6", "text": "T6\nfields.list"},
  {"id": "T7", "text": "T7\nTOC + update"},
  {"id": "T8", "text": "T8\nCaptions, xrefs"},
  {"id": "T9", "text": "T9\nDocs"}
 ],
 "edges": [
  {"from": "T1", "to": "T4"},
  {"from": "T2", "to": "T4"},
  {"from": "T3", "to": "T4"},
  {"from": "T4", "to": "T5"},
  {"from": "T6", "to": "T7"},
  {"from": "T7", "to": "T8"},
  {"from": "T5", "to": "T9"},
  {"from": "T8", "to": "T9"}
 ]}
```

## Evidence log

| T# | Gate | Measured | Command | SHA | Env | Date |
|----|------|----------|---------|-----|-----|------|

---

## Sign-off

- [ ] Plan reviewed by the owner
- [ ] Contracts agreed with literary-critic (page plan as the front-matter primitive)
- [ ] Ready for execution
