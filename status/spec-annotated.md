# Word Control — Specification (annotated edition)

> Generated 2026-10-12. This is the document with a **note slot** after every section. Read it in any Markdown app. To annotate, replace the `_(your notes…)_` placeholder under any section. When done, send the file back — notes are acted on.

---

# Specification

## Preamble & scope

**Date**: 2026-10-12
**Ticket / PR**: nuncaeslupus/word-control#2
**Author**: Claude (with the owner, via `explore-idea`)
**Revision**: 3
**Status**: draft
**Revision log**:
- r1 — first draft, from the explore-idea decisions log D-1…D-16 and `docs/research/01-tooling-and-risks.md`, `docs/research/02-docx-fixer-inventory.md`
- r2 — CodeRabbit review on PR #2: per-operation allowlist of supporting parts in the safety gate; undo-log retention and purge rules
- r3 — applied `word-control-spec-notes-2026-10-12-r2.md` and the owner's answers: bundle only generally useful recipes (owner-specific docx-fixer rules become user recipes/style sets on the owner's side, D-17); private test corpus never referenced in the repo; real-Word check left unmeasured for the first release; Python 3.11 floor with the other arsenal defaults; `move` primitive so marker-driven edits (writer notes in the text) can be applied with primitives

<!-- -->

> **✎ Notes** · `SPEC · intro`
> _(your notes here — replace this line)_

## §1 Problem statement

AI agents (Claude in Cowork, Claude Code, a chat's code sandbox) and downstream tools such as literary-critic need to read and change Word documents quickly, precisely and safely, while the writer — a non-technical person — keeps full control: every content change must be visible and rejectable in any Word version, and work must never be lost. Today this is done with computer use, ad-hoc scripts or docx-fixer, which only handles Spanish-novel typography and rewrites whole XML parts. Generic libraries do not fit: python-docx cannot write tracked changes, comment replies or the resolved state, and naive `xml.etree` serialisation silently drops namespace declarations listed in `mc:Ignorable`, which makes Word report the file as corrupt. This spec covers the first deliverable of **doc-control**: a deterministic, stdlib-only Python core with a file engine that edits a closed `.docx`, exposed through a JSON CLI, with stable addresses and anchors, all-or-nothing tracked-change batches, styles and templates, recipes, and a safety layer (lock and change detection, backups, verification, automatic restore, permanent undo log). The API is designed so that a live engine (Word on Windows via COM, later Mac) and an MCP front door can be added without breaking it; those are separate specs.

**Success criteria (measurable)** — each seeds a task Gate in the plan:

Safety and correctness
- [ ] `unintended_changes == 0` — over the test corpus, after every operation, everything outside the operation's declared footprint is identical to the input. The footprint is the targeted paragraphs in `word/document.xml` plus a fixed per-operation allowlist of supporting parts; every other zip member is byte-identical, and inside allowed parts only the entries the operation owns may change:

  | Operation family | Supporting parts that may change |
  |---|---|
  | Text, paragraph, direct-formatting and revision accept/reject edits | none beyond the targeted paragraphs |
  | Comments (add, reply, resolve) | `word/comments.xml`, `commentsExtended.xml`, `commentsIds.xml`, `commentsExtensible.xml`, `people.xml` (only the entries for the touched comments/authors); `[Content_Types].xml` and `word/_rels/document.xml.rels` only when a comment part is created |
  | Styles and style sets (create, update, rename, merge, install) | `word/styles.xml` (only the touched style entries; for rename/merge also the style references in affected paragraphs); `word/stylesWithEffects.xml` when present; `[Content_Types].xml` and `document.xml.rels` only when `styles.xml` is created |
  | Batches | the union of their operations' footprints |

  The verifier computes the expected footprint before writing and fails (then auto-restores) on any change outside it.
- [ ] `untouched_parts_byte_identical_ratio == 1.0`
- [ ] `xml_parse_errors == 0` and `ooxml_validator_errors_introduced == 0` (the .NET OOXML validator and a LibreOffice round trip run in CI; errors already present in the input do not count).
- [ ] `content_edits_without_revision_markup == 0` when tracked mode is on (the default).
- [ ] `text_after_reject_all == original_text` — rejecting every revision the tool wrote restores the input text exactly.
- [ ] `partial_writes_on_failure == 0` — fault injection at every stage of a batch leaves the original file unchanged (byte-identical) and a restorable backup.
- [ ] `writes_with_word_lock_present == 0` and `writes_after_external_change_without_force == 0` (`~$` lock file and on-disk hash/mtime checked before every write).
- [ ] `edits_applied_to_wrong_paragraph == 0` — fuzz test: random structural edits between read and write always yield `stale`, never a misplaced edit.
- [ ] `backup_before_write_ratio == 1.0`; every failed verification restores automatically (`auto_restore_success_ratio == 1.0`).
- [ ] `undo_roundtrip_mismatches == 0` — undoing any logged operation returns the document text and paragraph properties to their prior state; `restore` brings back any logged deleted paragraph as a tracked insertion.
- [ ] Recipes: `applied_without_dry_run_or_batch == 0`; user (declarative) recipes cannot read or write anything except through batch proposals (`user_recipe_capabilities_outside_batch == 0`, checked by test).

Performance (reference machine: GitHub-hosted `ubuntu-latest`, CPython 3.11, synthetic 100k-word novel)
- [ ] `rename_100k_words_p95_s <= 5` (find + atomic batch + write + verify).
- [ ] `find_100k_words_p95_ms <= 1000`; `outline_100k_words_p95_ms <= 1000`.
- [ ] `open_and_index_100k_words_p95_s <= 2`.

LLM ergonomics
- [ ] `default_response_tokens_p95 <= 2000` (estimated as bytes/4) and every list is capped with a `next` cursor.
- [ ] `operations_without_json_schema == 0`; `help --json` and `capabilities` list every operation.
- [ ] `responses_without_api_version == 0`; every error carries a code from the published list and a one-line hint (`errors_without_code_or_hint == 0`).
- [ ] `interactive_prompts == 0` (no command ever waits for stdin unless input is piped explicitly).

Portability and dependencies
- [ ] `third_party_imports_in_package == 0` (stdlib only).
- [ ] Test suite green on Python 3.11 and 3.13 × Linux and Windows in CI; the file engine runs in a Python 3.11 sandbox with no network (smoke test in CI with network disabled).

Anchors and segmentation
- [ ] `reanchor_success_ratio >= 0.98` on an edit corpus where up to 10% of characters around the anchor changed; `wrong_anchor_matches == 0` (ambiguous or weak matches return `ambiguous` / `not_found`, never a guess).
- [ ] Spanish sentence segmentation `boundary_f1 >= 0.98` on a gold set of at least 500 sentences covering abbreviations, `¿¡`, `«»`, ellipses and raya dialogue.

Parity and coverage
- [ ] docx-fixer parity (D-17): every generally useful docx-fixer rule ships as a bundled recipe and its round-trip cases pass (`docx_fixer_general_cases_failing == 0`); every owner-specific rule (house style ids, house template, the owner's author-note patterns) is expressible as a declarative user recipe or user style set, shown by tests on synthetic documents (`owner_rules_not_expressible == 0`). The owner's actual user recipes and style sets live on the owner's side, never in the repo.
- [ ] Bundled recipes beyond docx-fixer cover common needs (whitespace and empty-paragraph cleanup, quotes and dashes per language, ellipsis, fake centring and fake tabs, leftover revisions/comments/highlights/hidden text, direct formatting to style, language tags, scene-break normalisation, heading hygiene); the final list is fixed in `design`, each with tests (`bundled_recipes_without_tests == 0`).
- [ ] Private corpus: real documents are used only from a directory outside the repo given by an environment variable; tests skip when it is absent; no file name, title, path or excerpt of a private document is ever committed or printed in CI logs (`private_corpus_identifiers_in_repo == 0`, checked by the hygiene gate against a local denylist that itself stays outside the repo).
- [ ] Styles: installing an exported style set reproduces the style definitions (`style_definition_diffs_after_roundtrip == 0`); exported `.dotx` passes the validator; applying a style is recorded as a tracked formatting change.
- [ ] `line_coverage >= 0.90` for the package.
- [ ] Judged, not measured: one manual check on real Word (Windows, Word 2016 or later, and M365 when available) that tool output opens without a repair prompt and that every revision and comment can be accepted, rejected, replied to and resolved. Recorded as evidence with screenshots. Left `unmeasured` for the first release by owner decision (2026-10-12); done when a Windows machine is available.

<!-- -->

> **✎ Notes** · `SPEC §1`
> _(your notes here — replace this line)_

## §2 Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| `doc_control` core (new) | Primary | Addresses, anchors, segmentation, operation model, batches, safety layer, errors, schema | Yes (new) | Everything else builds on its contract; API mistakes here are expensive later | High |
| File engine (new) | Primary | Opens a closed `.docx`, indexes paragraphs, writes by splicing changed paragraphs into the original part bytes, verifies | Yes (new) | Must never corrupt a manuscript; correctness over features | High |
| CLI (new) | Primary | JSON in/out front door, `help --json`, `capabilities` | Yes (new) | Agents' main interface in Claude Code and the chat sandbox | Med |
| Recipe engine + bundled recipes (new) | Primary | Declarative user recipes and Python bundled recipes producing batch proposals; Spanish-novel set ported from docx-fixer | Yes (new) | Replaces docx-fixer for its only user | Med |
| Styles and style sets (new) | Primary | Style read/write, JSON style sets, `.dotx` export/import | Yes (new) | literary-critic and writers share house styles across books | Med |
| `.doc-control/` store (new) | Shared resource | Backups, permanent undo log, history search, next to each document | Yes (new) | Disk use bounded by retention (≈20–60 MB for a 1–3 MB manuscript); log ≈1 KB per changed paragraph | Med |
| literary-critic | Dependent | First downstream consumer through one adapter (locate, apply batch, rename, comments, map positions, report) | No (validation only) | Its adapter targets the v1 API; live-engine features (locate in open Word, save/reopen) come with the live engine spec | Med |
| docx-fixer | Dependent | Retired: frozen now, archived at parity (D-14) | No | Its rules, lessons and tests are the porting source | Low |
| CI (GitHub Actions) | Infrastructure | Linux + Windows × 3.11/3.13, validator, LibreOffice round trip, benchmarks, no-network smoke test | Yes | Validator and LibreOffice are test-only dependencies, never runtime | Low |
| Live engine (Windows COM), MCP server, `.mcpb` packaging | Future | Out of scope here; the API reserves room for them | No | Separate specs | Low |

**Impact dimensions**
- **Data**: documents are the writer's work; any corruption or silent change is critical → safety criteria above are gates, not goals. *High.*
- **API contracts**: v1 sets the contract downstream repos depend on (D-12). *High.*
- **Performance**: agents call the tool many times per session; targets above. *Med.*
- **User-facing**: writers see tracked changes and comments authored "Claude" (D-10); nothing changes without a visible revision. *Med.*
- **Operational**: PyPI release, CI matrix, test corpus maintenance. *Low.*
- **Risk of inaction**: agents keep using computer use and ad-hoc scripts — slow, imprecise, and able to corrupt files; docx-fixer stays the only, narrow tool. *High.*

<!-- -->

> **✎ Notes** · `SPEC §2`
> _(your notes here — replace this line)_

## §3 Options



<!-- -->

> **✎ Notes** · `SPEC §3`
> _(your notes here — replace this line)_

### Option A: Extend docx-fixer's engine (Conservative)

- **Description**: Rename and generalise docx-fixer: keep its ElementTree parse-and-reserialise model (with its `mc:Ignorable` pruning workaround), its text model with per-character offsets, and add tracked changes, comments, addresses, batches and a JSON CLI on top.
- **Scope**: Port of `package.py`, `xmlns.py`, `document.py`, `findings.py`, rules and template code; new CLI and safety layer.
- **Effort**: Medium
- **Tradeoffs**: + reuses tested code and known OOXML traps; + fastest to a first release. − Re-serialising whole parts changes bytes outside the edited range (attribute order, prefixes, declarations), so `unintended_changes == 0` and byte-identity can only be approximated; − the pruning workaround hides the namespace problem instead of avoiding it; − its rule/finding model is novel-specific.
- **Compatibility**: New package; no existing API to keep.

<!-- -->

> **✎ Notes** · `SPEC › Option A: Extend docx-fixer's engine (Conservative)`
> _(your notes here — replace this line)_

### Option B: New core with a splice writer (Recommended)

- **Description**: A new stdlib-only package. Parts are read with ElementTree for indexing, but writing never re-serialises a whole part: each changed paragraph (`w:p`) is serialised on its own and spliced into the original part bytes at its recorded byte span, keeping the original root start tag and every untouched byte. Untouched zip members are copied with their original `ZipInfo` and order. On top: position+fingerprint addresses (D-9), quote anchors with re-anchoring, a typed operation model where every operation has a JSON Schema (D-8), all-or-nothing batches with dry run and preview diff, tracked changes by default with configurable author (D-10), the safety layer (D-13, D-15), styles and JSON style sets with `.dotx` export/import (D-16), and recipes in three layers (D-11). docx-fixer's code is used as a reference and its tests as a corpus, not as a base.
- **Scope**: New `doc_control/` package (core, file engine, CLI, recipes, styles), test corpus generator, CI matrix.
- **Effort**: Large
- **Tradeoffs**: + byte-identity outside the edited paragraphs is achievable and testable; + avoids the namespace trap by construction; + engine-neutral operation model ready for the live engine; − more upfront work; − splicing needs exact byte spans per paragraph (a small stdlib tokenizer or position tracking), which must be fuzz-tested; − edits that touch part-level structures (comments, styles, numbering, settings) still need careful per-part writers.
- **Compatibility**: New package; API versioned from the first release (D-12).

<!-- -->

> **✎ Notes** · `SPEC › Option B: New core with a splice writer (Recommended)`
> _(your notes here — replace this line)_

### Option C: Full DOM with lxml

- **Description**: Use lxml (or python-docx plus lxml) for a full, namespace-faithful DOM and re-serialise touched parts.
- **Scope**: Same features as B, built on lxml.
- **Effort**: Medium
- **Tradeoffs**: + mature XML handling, namespaces preserved; + simpler edits on complex structures. − Third-party C extension: breaks the stdlib-only requirement and the chat sandbox target (D-7); − complicates Windows/Mac packaging; − whole-part re-serialisation still changes bytes outside the edit.
- **Compatibility**: New package; conflicts with D-7's portability goal.

<!-- -->

> **✎ Notes** · `SPEC › Option C: Full DOM with lxml`
> _(your notes here — replace this line)_

### Comparison

| | Option A | Option B | Option C |
|---|---|---|---|
| Effort | Medium | Large | Medium |
| Risk | Medium (approximate safety) | Low (safety testable by construction); splice correctness must be fuzzed | Medium (dependency, packaging) |
| Completeness | Partial: novel-specific model to unwind | Full v1 scope, engine-neutral | Full v1 scope |
| Compatibility | Stdlib-only | Stdlib-only, runs in chat sandbox | Breaks stdlib-only / sandbox |
| Maintenance | Carries docx-fixer's shape | Clean, owned design | Depends on lxml releases |

<!-- -->

> **✎ Notes** · `SPEC › Comparison`
> _(your notes here — replace this line)_

## §4 Recommendation

**Recommended option**: Option B — it is the only option that can meet `unintended_changes == 0` with byte-identical untouched content, keeps the package stdlib-only for the chat sandbox (D-7), and gives the live engine an engine-neutral contract. docx-fixer remains the source of OOXML lessons, rules and test cases.

**Scope of v1 (from the decisions log)**
- Read: outline, stats, styles and fonts in use, paragraph with formatting, estimated page of a paragraph (marked estimated), find (literal, regex, case- and accent-insensitive, whole word; capped output with cursors), comments with replies and resolved state, tracked revisions with authors, history search by quote.
- Write primitives: replace a range's text, insert/delete paragraphs, move a range of paragraphs (written as a tracked deletion plus a tracked insertion in v1), apply a style or direct formatting, alignment and indentation, add/reply/resolve comments, accept/reject revisions, rename a term (word boundaries, case options) as one atomic batch, `restore` from the undo log.
- Addresses `p142#a7b2` with `stale` errors and an old→new address map after structural edits; quote+context anchors with re-anchoring; segmentation into paragraphs, Spanish-aware sentences, dialogue lines, scene breaks, chapters (per-profile settings).
- Batches all-or-nothing with dry run and preview diff; tracked changes by default; author default "Claude" plus per-call tag.
- Safety: lock and on-disk change detection, backup before every write and on every open, verification, automatic restore, permanent append-only undo log, retention 20 backups / 30 days, `.doc-control/` next to the document with an app-data option. The retention rule prunes full backups only, never undo-log entries. Undo history is removed only by an explicit `history purge` (whole log or before a date; for privacy), which itself is confirmed with a dry run and logged as a tombstone. In app-data mode, history is keyed by a document id stored in the log; history whose document no longer exists is never deleted automatically and is listed by `history orphans` so the user can restore or purge it.
- Styles: list/read/create/update/apply/rename/merge; JSON style sets as source of truth; `.dotx` export/import; style sets stored on the user's side.
- Recipes: bundled (Python allowed, tested here; only generally useful recipes, including the general docx-fixer rules and other common fixes; owner-specific rules become user recipes, D-17) and user (declarative only, stored on the user's side, created by the agent on the fly or saved); every recipe yields a batch proposal.
- CLI with JSON I/O, `help --json`, `capabilities`, API version in every response, stable error codes with hints.
- Out of scope (later specs): live engine (Windows first, then Mac), MCP server and `.mcpb`/plugin packaging, Skill packaging for the chat sandbox, Google Docs/`.odt`, contract test suite (after literary-critic integrates), visual review page.

**Immediate next action**: owner reviews this spec through the annotated reader; on approval, run `design` to write contracts (operation schemas, error codes, address and anchor formats, `.doc-control/` layout), the risk register and the task split, starting with the splice writer and its fuzz tests, since every other gate depends on it.

**Open questions**:
- [x] Test corpus: both synthetic and real documents; real ones stay outside the repo and are never named in it (answered 2026-10-12).
- [x] Real-Word verification: left `unmeasured` for the first release; done when a Windows machine is available (answered 2026-10-12).
- [x] Python tooling: arsenal defaults (uv, ruff, strict mypy, standard Makefile) with the floor moved to Python 3.11 (answered 2026-10-12).
- [ ] Repository rename `word-control` → `doc-control` (owner, in GitHub Settings; old URLs redirect). References in the repo are updated after the rename.

**Writer notes in the text** (owner request, 2026-10-12): a writer may leave instructions inside the manuscript, e.g. a paragraph `========== Move this after chapter X`. Detecting and interpreting them is downstream work (literary-critic or the agent); the core only has to make applying them possible with primitives: `find` (regex) locates the markers, chapter detection resolves "chapter X", and one batch moves the target range, deletes the marker paragraph, and optionally adds a comment, all as tracked changes. A contract example in `design` covers this scenario end to end.

**Decisions log** (carried unchanged from `explore-idea`):

| ID | Decision | Status | Date |
|----|----------|--------|------|
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
| D-17 | docx-fixer frozen now (owner is its only user); archived at parity: its generally useful rules ship as bundled recipes with passing round-trip cases, owner-specific rules are re-created as user recipes / style sets on the owner's side; other common recipes are bundled too; reviewing tracked changes in Word with the agent replaces its review page | agreed | 2026-10-12 |


> Sections 5–6 (contracts, risks) are appended by `design`.

<!-- -->

> **✎ Notes** · `SPEC §4`
> _(your notes here — replace this line)_

