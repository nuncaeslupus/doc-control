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

## 5. Contracts

All changes are additive registry operations, so CLI, MCP and Python get them the same way. The plan JSON carries `"version": 1`; a later version adds fields, never renames them.

### Page plan (`layout.get` result, `layout.apply` request)

```json
{"version": 1, "two_sided": false, "mirror_margins": false,
 "sections": [
  {"from": "p1#…", "break": "nextPage", "numbering": {"shown": false, "start": null, "format": "lowerRoman"},
   "header": {"default": "Title", "first": "", "even": null},
   "footer": {"default": "{PAGE}", "first": null, "even": null},
   "first_page_different": true},
  {"from": "p9#…", "break": "oddPage", "numbering": {"shown": true, "start": 1, "format": "decimal"},
   "header": {"default": "{chapter}", "even": "Author Name"}, "footer": {"default": "{PAGE}"}}
 ]}
```

**Fields**:
- `from`: the body address where the section starts (the first section always starts at `p1`).
- `break`: one of `nextPage`, `oddPage`, `evenPage`, `continuous`.
- `header` and `footer` values:
  - a string sets that kind's text;
  - `""` sets an empty header;
  - `null` inherits from the previous section;
  - placeholders are `{PAGE}`, `{NUMPAGES}` and `{chapter}` (a `STYLEREF "Heading 1"` field), D-L5.
- `numbering.shown: false` keeps the count but shows no number: the plan's header/footer text has no `{PAGE}` for that section.

**Requests and results**:
- `layout.get` returns the plan the document has now; its `sections[].header` values read the header stories (STORIES T3).
- `layout.apply` takes `{"doc", "plan", "dry_run"?, "untracked"?}`. It returns the changes it made, or would make, as `[{"kind": "section.break" | "section.props" | "header.text" | "settings", "at": …, "tracked": true}]` and the new plan.

### Field operations

| Op | Kind | Request | Notes |
|----|------|---------|-------|
| `fields.list` | read | `{"doc", "stories"?}` | `[{"at", "code": "TOC \\o \"1-3\" \\h", "type": "TOC", "result": "…", "stale": bool}]`; citations show `type: "CITATION"` or `ADDIN ZOTERO_ITEM` and are never changed by other ops (D-L4) |
| `toc.insert` | write (batchable) | `{"after": "p3#…", "levels": "1-3", "title": "Contents"}` | Inserted as a tracked insertion; result text is filled by `fields.update` |
| `caption.add` | write (batchable) | `{"at": "p20#…", "label": "Table" \| "Figure" \| "Equation", "text": "…", "position": "above" \| "below"}` | `SEQ` field plus a bookmark |
| `xref.add` | write (batchable) | `{"at": "p30#…:12", "to": "<caption or heading address>", "show": "number" \| "page" \| "text"}` | `REF`/`PAGEREF` field |
| `index.mark` / `index.insert` | write (batchable) | `{"at": range, "entry": "Term:Sub"}` / `{"after": address}` | `XE` / `INDEX` fields |
| `fields.update` | write | `{"doc"}` | Recomputes field results through headless LibreOffice and writes only those result runs, as tracked changes. Without LibreOffice: `ENGINE_UNAVAILABLE` naming the stale fields (D-L3). `{"set_update_on_open": true}` sets `w:updateFields` instead. |

### Errors (existing codes)

- `SCHEMA_INVALID`: a bad plan.
- `STALE_ADDRESS`: a `from` address that no longer matches.
- `UNSUPPORTED_CONTENT`: a section break inside a table, or a change Word cannot track without `untracked: true` (D-L2).
- `ENGINE_UNAVAILABLE`: `fields.update` without LibreOffice.

### Safety contracts

- A layout footprint family allows changes only to:
  - the `w:sectPr` of the sections touched (and the paragraphs that gain or lose a section break);
  - `settings.xml` (`evenAndOddHeaders`, `mirrorMargins`, `updateFields`);
  - the header/footer parts named;
  - when a part is created, `[Content_Types].xml` and `document.xml.rels`.
- Section property changes carry `w:sectPrChange` with the old properties.
- History logs every part, so undo restores all of them together (STORIES T5).

### Inter-component contracts

| Caller | Callee | Protocol | Contract | Failure handling |
|--------|--------|----------|----------|-----------------|
| literary-critic | `layout.get`/`layout.apply` | JSON | Builds front matter by composing a plan (D-L5) | `dry_run` first; errors as above |
| `fields.update`, page-label checker | LibreOffice headless | subprocess (`soffice --convert-to`) | Same discovery as the validator (`soffice_path`) | Absent: `ENGINE_UNAVAILABLE` / test skip, failure under `DOC_CONTROL_REQUIRE_SOFFICE` |

### Database migrations

| Service | Database | Change | Reversible | Forward-compatible |
|---------|----------|--------|------------|-------------------|
| — | none | — | yes | yes |

## 6. Risks & Validation

| Risk | Likelihood | Impact | Mitigation | Validation |
|------|-----------|--------|------------|------------|
| Word's header inheritance (an unset `first`/`even` kind, `titlePg`, `evenAndOddHeaders` being document-wide) makes the applied plan differ from what Word shows | High | High | One tested resolver (STORIES `used_by_sections`); `layout.get` after `apply` must equal the plan; real-Word check | integration + manual |
| Page numbers in the PDF depend on LibreOffice's layout, not Word's | Med | Med | The gate checks labels (numbering), not where a page breaks; scenarios use page breaks, not text flow | integration |
| `w:sectPrChange` is rejected or ignored by some Word versions | Med | Med | Follow ECMA-376 order; owner's Word check; D-L2 refusal for what cannot be tracked | manual (Word) |
| `fields.update` rewrites more than field results | Med | High | Read results from the LibreOffice-converted copy and splice only result runs into the original; footprint check | unit + integration |
| Citation fields broken by other edits (Zotero JSON in `instrText`) | Low | High | Treated as opaque; `field_loss == 0` over a fields corpus after every write op | integration |
| The CI runner has no `pdftotext` | Low | Low | Install `poppler-utils` in the `libreoffice` job | CI |
