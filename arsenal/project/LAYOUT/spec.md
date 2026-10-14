# Specification: page layout and long-document structure

**Date**: 2026-10-14
**Ticket / PR**: — (workspace `LAYOUT`)
**Author**: Claude (doc-control session), for the owner
**Revision**: 2
**Status**: approved (2026-10-14, revision 2) — without annotations
**Revision log**:
- r1 — first draft, from sub-project S-9 of `status/ideas.md` and the owner's notes on the STORIES spec
- r2 — the owner approved Option B and answered Q1–Q5 in chat (no notes export): Q1–Q4 as recommended, Q5 front matter stays in consumers (D-L1..D-L5)

> Review record. Each round of reviewer notes makes a new revision: bump
> **Revision**, add a log line naming the export it applied, and commit that
> export beside this file in the same commit. On approval set **Status** to
> `approved (YYYY-MM-DD, revision N)` and, when the reviewer sent no notes for
> that revision, add `— without annotations`.

---

## 1. Problem statement

Word hides the structure of a long document across many places:

- section breaks, each with its own page setup (`w:sectPr`);
- header and footer references per section and kind (default, first, even), where a missing reference means the section inherits the previous one;
- page-number restarts and formats (`w:pgNumType`);
- the document-wide "different odd and even" and "mirror margins" settings;
- fields: `PAGE`, `TOC`, `INDEX`, `XE`, `SEQ`, `REF`/`PAGEREF`, and citation fields.

Getting this right by hand is notoriously hard. Typical goals:

- No visible number on the front matter, then page 1 at the first chapter.
- A part-title page counted but unnumbered.
- Chapters starting on a right-hand page.
- Mirrored headers when printing on both sides.
- A table of contents whose page numbers are true.

Agents and the tools built on doc-control can't do any of this today. literary-critic, for example, wants to build a title page, an epigraph page and a dedication, then start numbering at the text. Once STORIES is done, doc-control reads each section's setup (`sections.list`) but cannot change it, cannot insert or refresh a TOC or an index, and cannot add captions or cross-references.

This spec covers that layer, as generic primitives that consumers combine (D-19 in the core spec). The judgment ("this is front matter") stays with the agent or the consumer.

**Ideas covered**: I-34, I-35, I-36, I-37, I-38, I-39 (sub-project S-9). Builds on STORIES (`sections.list`, header and footer stories).

**Success criteria (measurable)**:

- [ ] `page_label_mismatches == 0`. For each scripted layout scenario, LibreOffice exports a PDF and the page number printed in each page's header or footer equals the plan. Scenarios:
  - unnumbered front matter, then 1;
  - roman then arabic;
  - a counted but unnumbered part page;
  - restarts per part;
  - recto chapter starts with blank versos;
  - two-sided mirrored headers.
- [ ] `layout_roundtrip_mismatches == 0`: after `layout.apply`, `sections.list` and LibreOffice both report the requested section breaks, numbering starts and formats, header/footer kinds, odd/even and mirror settings.
- [ ] `toc_page_mismatches == 0`: after `fields.update` (through LibreOffice), every TOC entry's page number equals the page LibreOffice renders that heading on. The same holds for `PAGEREF` cross-references and index entries.
- [ ] `unintended_changes == 0` for every layout write: only `w:sectPr`, `settings.xml`, the header/footer parts named and the field paragraphs touched may change.
- [ ] Every layout change is visible to the writer:
  - section property changes go in as tracked property changes (`w:sectPrChange`);
  - inserted header/footer text, TOC and captions go in as tracked insertions;
  - `revisions.list` lists them, and rejecting them restores the previous layout. `layout_untracked_changes == 0`.
- [ ] Judged, not measured: the owner opens each scenario in Word on Windows. No repair prompt, page numbers as planned, and "Update field" in Word gives the same TOC.

## 2. Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| New `ops/layout.py` (`layout.get`, `layout.apply`) | Primary | One JSON "page plan" read and written as a whole | Yes (new) | New ops; reads extend `sections.list` | High |
| `ooxml` section and settings writers | Primary | `w:sectPr` (with `w:sectPrChange`), `w:pgNumType`, `w:titlePg`, `w:type`, `settings.xml` (`evenAndOddHeaders`, `mirrorMargins`, `updateFields`) | Yes | Writes touch the body's section paragraphs and `settings.xml` | High |
| Header/footer stories (STORIES) | Dependent | Content of each kind; `PAGE` field insertion | Uses | Creating a new header part adds a rel and a content-type entry | Med |
| New `ops/fields.py` (`toc.insert`, `index.mark`, `index.insert`, `caption.add`, `xref.add`, `fields.list`, `fields.update`) | Primary | Fields with tracked result text | Yes (new) | `fields.update` needs LibreOffice installed; without it, `updateFields` is set so Word refreshes on open | Med |
| LibreOffice (headless) | Shared resource | Renders pages to refresh field results and to verify page labels | Optional | Already used in CI and by the live engine | Med |
| `safety/verify.py` | Primary | Footprint over section paragraphs and `settings.xml` | Yes | New footprint family | Med |
| literary-critic | Consumer | Builds front matter and numbering from these primitives | Validation | Its own templates; no doc-control templates | Low |

**Impact**:
- **API**: additive.
- **User**: the writer sees each layout change as a tracked change.
- **Performance**: `fields.update` costs one LibreOffice conversion, seconds on a novel; it runs only when asked.
- **Risk of inaction**: high for theses and publishing, where the table of contents and numbering are mandatory and are the step writers most often get wrong.

## 3. Options

### Option A: Low-level primitives only (Conservative)

- **Description**: `section.set` (one section's properties), `section.insert_break` at an address, `header.link`/`unlink`, `settings.set`, and raw `field.insert` (a field code at an address). The agent composes everything itself.
- **Scope**: `ops/layout.py`, `ops/fields.py`, verification.
- **Effort**: Medium.
- **Tradeoffs**: a small, safe surface. But the agent must know Word's inheritance rules (an unlinked first-page header, inherited references, `titlePg`) to reach a goal. That is exactly the expertise the owner asks doc-control to hold, and agents get it wrong the way people do.
- **Compatibility**: additive.

### Option B: A declarative page plan, plus field primitives (Recommended)

- **Description**:
  - `layout.get` returns the document's page plan. `layout.apply` takes a plan and computes the minimal tracked changes to reach it: section breaks, numbering starts and formats, visible or hidden numbers, header/footer content per kind including `PAGE` fields, odd/even, mirror margins, and recto starts (`w:type="oddPage"`). It previews them with `dry_run` and applies them all or nothing.
  - Inheritance stays inside doc-control. The plan says what each page range looks like, not how Word links the parts.
  - Fields come as primitives: `toc.insert`, `index.mark`/`index.insert`, `caption.add`, `xref.add`, `fields.list`, plus `fields.update`, which refreshes results through headless LibreOffice and otherwise sets `updateFields`.
  - Citation fields (Word sources, Zotero, Mendeley) are listed and kept intact on every edit; they are not generated.
- **Scope**: everything in the §2 table.
- **Effort**: Large.
- **Tradeoffs**: one call reaches a layout goal and Word's inheritance rules are handled in one tested place. It has more code than A, and the plan format becomes a contract to version.
- **Compatibility**: additive. The plan JSON carries a `version`.

Example plan:

```json
{"version": 1, "two_sided": true, "sections": [
  {"from": "p1#…",  "numbering": {"shown": false}, "kind": "front"},
  {"from": "p9#…",  "break": "oddPage", "numbering": {"start": 1, "format": "decimal"},
   "header": {"odd": "{chapter}", "even": "Author Name"}, "footer": {"odd": "{PAGE}", "even": "{PAGE}"},
   "first_page": {"header": "", "footer": "{PAGE}"}}
]}
```

### Option C: Render-driven layout (LibreOffice as the layout engine)

- **Description**: compute everything by driving LibreOffice. Open the document, set styles and sections through UNO, then save.
- **Scope**: the live engine's LibreOffice adapter.
- **Effort**: Large.
- **Tradeoffs**: exact page numbers come for free. But LibreOffice becomes required (the core is stdlib-only), LibreOffice rewrites the whole package on save (byte identity outside the change is lost), and its tracked-change behaviour for section properties is weak.
- **Compatibility**: breaks the stdlib-only core promise for these ops.

### Comparison

| | Option A | Option B | Option C |
|---|---|---|---|
| Effort | Medium | Large | Large |
| Risk | Low | Medium (plan diffing) | High (byte identity, tracking) |
| Completeness | Partial (the agent must master Word's rules) | Full | Full, but untracked |
| Compatibility | Full | Full | Requires LibreOffice |
| Maintenance | Low | Medium (versioned plan) | High (two engines) |

## 4. Recommendation

**Recommended option**: Option B. It puts Word's section and header-inheritance rules in one tested place, gives consumers like literary-critic a single call per layout goal, and keeps the stdlib-only core. LibreOffice is used only to refresh field results and to verify, as today.

**Immediate next action**:
1. Finish STORIES T3 and T8 (story index, `sections.list`); this spec builds on them.
2. After approval, `design` writes the plan, starting with the page-label checker: a LibreOffice PDF export plus page text, so every later task has its gate.

**Open questions**: none. The owner answered Q1–Q5 in r2; see D-L1..D-L5.

**Decisions log** (from `status/ideas.md`):

| ID | Decision | Status | Date |
|----|----------|--------|------|
| D-2 | STORIES reads section page setup; changing it is this sub-project (S-9) | agreed | 2026-10-14 |
| D-L1 | Option B: a declarative page plan (`layout.get`/`layout.apply`) plus field primitives | agreed | 2026-10-14 |
| D-L2 | A layout change Word cannot track is refused unless `untracked: true`, and is always logged for undo | agreed | 2026-10-14 |
| D-L3 | Without LibreOffice, stale fields are reported; `updateFields` is set only on request | agreed | 2026-10-14 |
| D-L4 | Citations are listed and kept intact on edit, not generated | agreed | 2026-10-14 |
| D-L5 | Header text placeholders: `{chapter}` (STYLEREF), `{PAGE}`, `{NUMPAGES}` only. Front matter (title page, epigraph, dedication) is built by consumers such as literary-critic; doc-control provides the sections, numbering and headers it needs | agreed | 2026-10-14 |
