# Plan: doc-control — core and file engine (v1)

**Date**: 2026-10-12
**Specification**: `status/specification.md` (approved, revision 6)
**Author**: Claude
**Revision**: 3
**Status**: approved (2026-10-12, revision 3) — without annotations
**Revision log**:
- r1 — first draft
- r2 — owner decision D-18 (2026-10-12): tasks T31–T36 for literary-critic indexing support; T29 also depends on them
- r3 — owner decision D-19 (2026-10-12): T37 `compare` (redline document from two versions); T29 depends on it

---

## Technical solution

### Architecture overview

A single stdlib-only package, `doc_control`, in four layers. Each layer only calls the one below it.

```text
cli.py / Python API          JSON envelope, schema validation, capabilities, help --json
        │
ops/                         operation registry (one JSON Schema per op), batch engine, recipes, stylesets
        │
model/                       document index: paragraphs, visible text + offsets, runs, addresses,
        │                    anchors, segmentation (sentences, dialogue, scene breaks, chapters)
ooxml/  +  safety/           zip I/O (raw member copy), paragraph span tokenizer, splice writer,
                             revision/comment/style part writers | lock + change detection, store,
                             backups, undo log, footprint verifier, auto-restore
```

### Data flow

1. **Read**: open zip → read `[Content_Types].xml`, `document.xml` and the parts the operation needs → index paragraphs with byte spans → answer, capped to the token budget with a `next` cursor.
2. **Write (batch)**:
   1. validate the request against its schema;
   2. check the `~$` lock file and `expect.sha256`;
   3. re-resolve every address and fingerprint (`STALE_ADDRESS` on mismatch);
   4. plan edits and compute the expected footprint;
   5. if `dry_run`, return the preview;
   6. otherwise back up, build the new XML for touched paragraphs and entries, splice them into the original part bytes, and write a temp zip copying untouched members raw;
   7. verify: every part parses, the footprint matches, and reject-all text is unchanged;
   8. `os.replace` the temp file over the document, append to the undo log, and return `op_id`, `address_map` and the new sha256;
   9. on any failure, leave the original untouched (or restore it from the backup) and return `VERIFY_FAILED_RESTORED`.

### State changes

| Service | Database | Change | Description |
|---------|----------|--------|-------------|
| Document | the `.docx` | UPDATE | Only through verified batches; tracked by default |
| Document store | `.doc-control/<file name>/backups/` | CREATE/DELETE | Backup on open-for-write and before every write; pruned to 20 or 30 days, whichever keeps more. Keyed by the full file name, not the stem, to avoid same-stem collisions (`Novela.docx` / `Novela.dotx`) |
| Document store | `.doc-control/<file name>/log.jsonl` | CREATE (append) | One line per applied batch; removed only by `history.purge` (tombstone line) |
| User store | `recipes/`, `stylesets/` | CREATE/UPDATE | Written only when the agent saves a user recipe or style set |

### Technology choices

| Choice | Justification |
|--------|--------------|
| Python ≥ 3.11, stdlib only (`zipfile`, `xml.etree`, `hashlib`, `json`, `re`, `unicodedata`) | Runs in the chat sandbox (D-7); no packaging burden |
| Read with ElementTree, write by splicing paragraph bytes | Avoids the `mc:Ignorable` namespace-loss trap; byte-identity outside the footprint (Option B) |
| uv, ruff, strict mypy, standard Makefile (arsenal defaults, floor 3.11) | Owner decision 2026-10-12 |
| pytest for tests (dev dependency only) | Standard; never imported by the package |
| .NET OOXML validator and LibreOffice headless in CI only | Independent check that output is valid; not runtime dependencies |

### Out of scope

- Live engine (Windows COM, Mac), MCP server, `.mcpb`/plugin packaging, Skill packaging, Google Docs/`.odt`, review page, contract test suite for downstream repos (later specs).
- Editing headers, footers, footnotes and endnotes (read-only listing in v1).
- Editing across fields, hyperlinks, content controls (refused with `UNSUPPORTED_CONTENT`).
- Word move-revision markup (moves are a tracked delete plus a tracked insert in v1).

---

## Implementation tasks

| T# | Description | Service | Size | Depends | Gate | Tests |
|----|-------------|---------|------|---------|------|-------|
| T1 | Bootstrap: `pyproject.toml` (requires-python ≥3.11), uv, ruff, strict mypy, Makefile (`check`, `lint`, `test`, `bench`), CI runs `make check`; hygiene gate kept | repo | S | — | `make_check_exit_code == 0` | `test_package_imports_without_third_party_modules` in `tests/test_package.py` — importing `doc_control` loads no module outside the stdlib |
| T2 | Error codes, response envelope, operation registry with JSON Schemas, CLI skeleton (`capabilities`, `help --json`), no interactive prompts | core/cli | M | T1 | `operations_without_json_schema == 0` | `test_registry_every_op_has_schema_and_appears_in_capabilities` in `tests/test_registry.py` — every registered op has a schema and is listed |
| T3 | Zip I/O: open package, read parts, write temp zip copying untouched members with original `ZipInfo` and order, atomic replace | ooxml | M | T1 | `untouched_parts_byte_identical_ratio == 1.0` | `test_rewrite_one_part_keeps_other_members_byte_identical` in `tests/ooxml/test_zipio.py` — after rewriting `document.xml` every other member's bytes are unchanged |
| T4 | Paragraph span tokenizer and splice writer for `word/document.xml`, with fuzzing | ooxml | L | T3 | `splice_roundtrip_mismatches == 0` | `test_splice_identity_edit_returns_identical_bytes` in `tests/ooxml/test_splice.py` — splicing unchanged paragraphs back yields the original bytes for every corpus document |
| T5 | Synthetic corpus generator (revisions, comments with replies, fields, hyperlinks, tables, footnotes, styles, `mc:Ignorable` namespaces, 100k-word novel) and private-corpus hook via `DOC_CONTROL_PRIVATE_CORPUS` | tests | M | T3 | `synthetic_docs_failing_validator == 0` | `test_corpus_generator_documents_pass_validator` in `tests/corpus/test_generator.py` — every generated document passes the OOXML validator |
| T6 | Document index: paragraphs, visible text with offsets to runs, addresses and fingerprints, range parsing | model | M | T4, T5 | `address_resolution_errors == 0` | `test_address_roundtrip_every_paragraph_resolves` in `tests/model/test_address.py` — every paragraph's address resolves back to the same paragraph |
| T7 | Read operations: `info`, `outline`, `stats`, `para.get`, `styles.list`, `style.get`, `comments.list`, `revisions.list`, token caps and cursors | ops | M | T2, T6 | `default_response_tokens_p95 <= 2000` | `test_read_ops_large_doc_responses_stay_under_budget` in `tests/ops/test_read.py` — on the 100k-word document every default response is ≤ 8,000 bytes and carries `next` when truncated |
| T8 | `find`: literal, regex, case- and accent-insensitive, whole word, capped results with cursors | ops | M | T2, T6 | `find_100k_words_p95_ms <= 1000` | `test_find_accent_insensitive_matches_unaccented_query` in `tests/ops/test_find.py` — `maria` finds `María` when accent-insensitive |
| T9 | Safety basics: `~$` lock detection, sha256 change detection, `.doc-control/` store layout, document id, backups and retention, app-data option | safety | M | T3 | `writes_with_word_lock_present == 0` | `test_write_with_lock_file_present_returns_doc_locked` in `tests/safety/test_lock.py` — a batch with a `~$` file next to the document fails with `DOC_LOCKED` and leaves it untouched |
| T10 | Footprint verifier, auto-restore, fault injection at every write stage | safety | M | T4, T9 | `partial_writes_on_failure == 0` | `test_fault_at_each_stage_leaves_original_unchanged` in `tests/safety/test_verify.py` — injecting a failure at each stage leaves the document byte-identical and returns `VERIFY_FAILED_RESTORED` |
| T11 | Tracked text operations: `text.replace`, `para.insert`, `para.delete`, `para.move` (`w:ins`/`w:del`/`w:delText`, deleted paragraph marks, unique ids, author and tag, date) | ops/ooxml | L | T6, T10 | `text_after_reject_all_mismatches == 0` | `test_reject_all_after_edits_restores_original_text` in `tests/ops/test_tracked_text.py` — rejecting every revision written by the tool restores the original text exactly |
| T12 | Batch engine: all-or-nothing, dry run with preview, overlap detection, address map, stale detection fuzz | ops | L | T11 | `edits_applied_to_wrong_paragraph == 0` | `test_stale_address_after_structural_change_is_rejected` in `tests/ops/test_batch.py` — random inserts between read and write always produce `STALE_ADDRESS`, never an edit elsewhere |
| T13 | Undo log, `undo`, `restore`, `history.list`, `history.search`, `history.purge` (tombstone), orphans listing, backup on open | safety | M | T12 | `undo_roundtrip_mismatches == 0` | `test_undo_each_logged_op_restores_prior_state` in `tests/safety/test_history.py` — undoing any logged batch returns text and paragraph properties to their prior state |
| T14 | Formatting operations: `style.apply`, `format.apply` as tracked `w:pPrChange`/`w:rPrChange` | ops | M | T12 | `content_edits_without_revision_markup == 0` | `test_style_apply_tracked_records_ppr_change` in `tests/ops/test_format.py` — applying a style in tracked mode writes a `w:pPrChange` with the previous style |
| T15 | Comments: `comment.add`, `comment.reply`, `comment.resolve` across comments, commentsExtended, commentsIds, commentsExtensible and people parts | ops/ooxml | L | T12 | `ooxml_validator_errors_introduced == 0` | `test_comment_reply_and_resolve_pass_validator` in `tests/ops/test_comments.py` — adding, replying to and resolving comments on every corpus document introduces no validator errors |
| T16 | `revision.accept`, `revision.reject` for tool-written and existing revisions | ops | M | T12 | `revision_accept_reject_mismatches == 0` | `test_accept_then_text_equals_expected_final_text` in `tests/ops/test_revisions.py` — accepting all revisions yields the expected final text |
| T17 | `term.rename` with whole word, case, preserve-case and inflection options, as one batch; benchmark | ops | M | T8, T12 | `rename_100k_words_p95_s <= 5` | `test_rename_preserves_case_variants` in `tests/ops/test_rename.py` — renaming `Lucía`→`Elena` also turns `LUCÍA` into `ELENA` and skips `Lucíana` with whole word |
| T18 | Segmentation: Spanish and generic sentences, dialogue lines (raya), scene breaks, chapters; gold set of ≥ 500 sentences | model | M | T6 | `boundary_f1 >= 0.98` | `test_spanish_abbreviations_do_not_split_sentences` in `tests/model/test_segment.py` — `Sr. García llegó.` is one sentence |
| T19 | Anchors: `anchor.create`, `anchor.resolve`, re-anchoring with score and ambiguity rules | model | M | T8, T18 | `reanchor_success_ratio >= 0.98` | `test_ambiguous_anchor_returns_candidates_not_guess` in `tests/model/test_anchor.py` — two equally good matches return `ANCHOR_AMBIGUOUS` |
| T20 | `page.estimate` (page breaks, `lastRenderedPageBreak`, word-count model) always flagged as estimated | ops | S | T6 | `page_estimates_without_estimated_flag == 0` | `test_page_estimate_is_flagged_estimated` in `tests/ops/test_page.py` — every response carries `"estimated": true` |
| T21 | Style write operations (`style.create`, `style.update`, `style.rename`, `style.merge`) and JSON style sets (`styleset.export`, `styleset.install`) | ops | M | T12 | `style_definition_diffs_after_roundtrip == 0` | `test_export_then_install_reproduces_style_definitions` in `tests/ops/test_stylesets.py` — installing an exported set into a blank document reproduces the definitions |
| T22 | `.dotx` export and import of style sets | ops | M | T21 | `dotx_validator_errors == 0` | `test_exported_dotx_passes_validator` in `tests/ops/test_dotx.py` — every exported template passes the validator |
| T23 | Declarative recipe engine, user recipe store, `recipe.list`, `recipe.run` (proposal batch, repeat until stable) | ops/recipes | L | T8, T12 | `user_recipe_capabilities_outside_batch == 0` | `test_user_recipe_with_unknown_key_is_rejected` in `tests/recipes/test_engine.py` — a recipe with any key outside the schema fails with `RECIPE_INVALID` |
| T24 | Bundled recipes: generally useful docx-fixer rules, with their round-trip cases | recipes | L | T14, T23 | `docx_fixer_general_cases_failing == 0` | `test_dialogue_dash_recipe_fixes_hyphen_openers` in `tests/recipes/test_es_typography.py` — `- Hola` becomes `—Hola` as a tracked change |
| T25 | Other common bundled recipes (whitespace and empty paragraphs, quotes and dashes per language, ellipsis, fake centring and tabs, leftovers, direct formatting to style, language tags, scene breaks, heading hygiene) | recipes | M | T14, T23 | `bundled_recipes_without_tests == 0` | `test_every_bundled_recipe_has_a_test_module` in `tests/recipes/test_inventory.py` — each bundled recipe has at least one passing test |
| T26 | Owner-rule expressibility: house styles, template and author-note patterns as user recipes and style sets, on synthetic documents | recipes | S | T21, T23 | `owner_rules_not_expressible == 0` | `test_house_rules_expressible_as_user_recipes` in `tests/recipes/test_user_expressibility.py` — each owner-specific docx-fixer rule has a declarative equivalent that passes its case |
| T27 | CI extras: OOXML validator and LibreOffice round trip, no-network smoke on 3.11, benchmark job, private-corpus hygiene check | repo/ci | M | T5, T10 | `private_corpus_identifiers_in_repo == 0` | `test_hygiene_flags_denylisted_name` in `tests/ci/test_hygiene.py` — the hygiene check fails when a denylisted identifier appears in a tracked file |
| T28 | Writer-notes scenario: marker paragraph `========== Move this after chapter X` applied with `find` + chapters + one tracked batch | ops | S | T12, T15, T18 | `writer_note_scenario_failures == 0` | `test_marker_move_after_chapter_applies_as_tracked_batch` in `tests/scenarios/test_writer_notes.py` — the passage moves after chapter X, the marker is deleted, both as revisions |
| T29 | Coverage and documentation: README usage, error-code reference, generated operation reference from schemas | repo | M | T7, T13, T17, T19, T20, T22, T24, T25, T26, T27, T28, T31, T32, T33, T34, T35, T36, T37 | `line_coverage >= 0.90` | `test_docs_list_every_error_code` in `tests/test_docs.py` — every error code in `errors.py` appears in the reference |
| T30 | Manual real-Word check (Windows, Word 2016+ and M365): open without repair, accept/reject/reply/resolve | manual | S | T15, T16 | `real_word_repair_prompts == 0` | Manual checklist in `docs/real-word-check.md` — evidence recorded as `unmeasured` until a Windows machine is available |
| T31 | Layout facts in `para.get` and `outline`: `empty`, `blank_before`, heading level (via `basedOn`), style, centred, page break before | ops | S | T7 | `layout_fact_mismatches == 0` | `test_para_get_reports_blank_before_and_heading_level` in `tests/ops/test_layout.py` — a heading after two empty paragraphs reports `blank_before: 2` and its level |
| T32 | `text.normalized`: NFC, soft hyphens and zero-width removed, whitespace collapsed, with a two-way offset map | model | S | T6 | `offset_map_roundtrip_mismatches == 0` | `test_offset_map_roundtrip_collapsed_to_visible` in `tests/model/test_normalized.py` — every offset maps collapsed → visible → collapsed unchanged |
| T33 | Structure detection: stable chapter and scene ids, detection signals, fingerprint-keyed overrides, profile settings (minimum chapter words) | model | M | T18 | `structure_override_failures == 0` | `test_override_forces_chapter_start_by_fingerprint` in `tests/model/test_structure.py` — an override makes the given paragraph start a chapter and is reported as the signal |
| T34 | `anchor.resolve` exact mode and match kind | model | S | T19 | `exact_mode_fuzzy_matches == 0` | `test_exact_mode_rejects_fuzzy_candidate` in `tests/model/test_anchor_exact.py` — a one-character change returns `ANCHOR_NOT_FOUND` in exact mode and `match: fuzzy` in best mode |
| T35 | `diff`: paragraph alignment between two document versions (unchanged, modified, moved, split, merged) with a cross-version address map | ops | M | T6 | `diff_alignment_errors == 0` | `test_diff_detects_moved_and_split_paragraphs` in `tests/ops/test_diff.py` — a corpus pair with a moved and a split paragraph is aligned exactly |
| T36 | `locate`: chapter, scene, estimated page and shortest unique first-and-last-words phrase per paragraph | ops | S | T8, T20, T33 | `locators_not_unique == 0` | `test_locator_phrase_is_unique_in_document` in `tests/ops/test_locate.py` — every returned phrase matches exactly one place in the document |
| T37 | `compare`: write a new redline `.docx` from two versions, every difference as a tracked change (paragraph alignment from `diff`, word-level changes inside modified paragraphs) | ops | M | T11, T35 | `compare_redline_text_mismatches == 0` | `test_compare_accept_all_equals_new_reject_all_equals_old` in `tests/ops/test_compare.py` — accepting all revisions in the redline yields the new text and rejecting all yields the old text |

**Status legend**: ☐ not started · ◐ in progress · ☑ merged

**Merge order**: T1 → T2, T3 → T4, T5, T9 → T6, T10 → T7, T8, T11, T18, T20 → T12 → T13–T17, T21, T23, T19 → T22, T24–T28, T31–T37 → T29; T30 when a Windows machine is available.
**Branch pattern**: `doc-control-T<N>-<description>` from the default branch

## Evidence log

| T# | Gate | Measured | Command | SHA | Env | Date |
|----|------|----------|---------|-----|-----|------|
| T1 | `make_check_exit_code == 0` | 0 | `make check` | 50b04fb | local (Linux, CPython 3.11 via uv) | 2026-10-12 |
| T2 | `operations_without_json_schema == 0` | 0 | `uv run pytest tests/test_registry.py` (counts registered schemas failing `check_schema` plus ops missing from `capabilities`) | 0633dc9 | local (Linux, CPython 3.13 via uv) | 2026-10-12 |
| T3 | `untouched_parts_byte_identical_ratio == 1.0` | 1.0 | `uv run pytest tests/ooxml/test_zipio.py (ratio of untouched members whose raw compressed bytes and header fields are identical)` | 0188f16 | local (Linux, CPython 3.13 via uv) | 2026-10-12 |
| T9 | `writes_with_word_lock_present == 0` | 0 | `uv run pytest tests/safety` | bffa774 | local (Linux, CPython 3.11 via uv) | 2026-10-12 |
| T5 | `synthetic_docs_failing_validator == 0` | 0 (of 28) | `DOC_CONTROL_REQUIRE_SOFFICE=1 uv run pytest tests/corpus` (validator: structural lint + LibreOffice 24.2.7.2 headless round trip; 40 passed, 1 skipped: private corpus unset) | 645979c | local (Linux, CPython 3.13.16 via uv) | 2026-10-12 |
| T4 | `splice_roundtrip_mismatches == 0` | 0 (400 generated documents) | `uv run pytest tests/ooxml/test_splice.py` | 41065c5 | local (Linux, CPython 3.13.16 via uv; requires-python >=3.11) | 2026-10-12 |
| T6 | `address_resolution_errors == 0` | 0 (440 generated documents, 12679 paragraphs) | `uv run pytest tests/model/test_address.py` | 284e850 | local (Linux, CPython 3.13.16 via uv; requires-python >=3.11) | 2026-10-12 |
| T7 | `default_response_tokens_p95 <= 2000` | 1993 (bytes/4 over every default and followed page of 8 read ops on the 100k-word document with 80 extra annotated sections) | `uv run pytest -s tests/ops/test_read.py::test_read_ops_large_doc_responses_stay_under_budget` | e6399f6 | local (Linux, CPython 3.13.16 via uv; requires-python >=3.11) | 2026-10-12 |
| T18 | `boundary_f1 >= 0.98` | 0.9973 (gold set: 667 sentences, 368 internal boundaries; es + generic; own and public-domain text) | `uv run pytest -q -s tests/model/test_segment.py` (micro F1 of internal sentence boundaries) | 8c64351 | local (Linux, CPython 3.13.16 via uv; requires-python >=3.11) | 2026-10-12 |
| T10 | `partial_writes_on_failure == 0` | 0 (of 22 injected faults) | `uv run pytest -s tests/safety/test_verify.py` (prints `partial_writes_on_failure=<n>`: document byte-identical, no stray temp file, restorable backup after every fault) | b4704e6 | local (Linux, CPython 3.13.16 via uv) | 2026-10-12 |
| T35 | `diff_alignment_errors == 0` | 0 (20 document pairs, 7 change kinds incl. moved block, split, merge; 100k-word novel diffed in 0.27 s, 100k unrelated words in 2.0 s) | `uv run pytest -q -s tests/ops/test_diff.py` (prints `diff_alignment_errors=<n>`: symmetric difference between expected and found (kind, old, new) changes) | 89bc8a8 | local (Linux, CPython 3.13.16 via uv; requires-python >=3.11) | 2026-10-12 |

### Dependency graph

```text
T1 ─┬─> T2 ──────────────┬─> T7
    └─> T3 ─┬─> T4 ─┬─> T6 ─┼─> T8 ─┬─> T17
            │       │       ├─> T18 ─┼─> T19
            ├─> T5 ─┘       ├─> T20  │
            └─> T9 ─> T10 ──┴─> T11 ─> T12 ─┬─> T13, T14, T15, T16, T21 ─> T22
                                            └─> T23 ─> T24, T25, T26
T5, T10 ─> T27     T12, T15, T18 ─> T28     all tasks except T30 (through T29's Depends list) ─> T29     T15, T16 ─> T30
T7 ─> T31   T6 ─> T32, T35   T18 ─> T33   T19 ─> T34   T8, T20, T33 ─> T36   T11, T35 ─> T37
```

---

## Sign-off

- [ ] Design reviewed by the owner
- [ ] Contracts agreed with literary-critic (adapter targets API 1.x)
- [ ] Store layout (`.doc-control/`) validated
- [ ] Ready for execution
