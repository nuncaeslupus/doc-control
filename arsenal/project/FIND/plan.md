# Plan: loose find matching

**Date**: 2026-10-14
**Specification**: `arsenal/project/FIND/spec.md` (approved r3; contracts §5, risks §6)
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

Option B (D-F1): one new module, `model/loosefold.py`, and five ops that call it.

1. **Fold** (`fold`, `to_original`): turns a text into its comparison string at a level (`case`, `default`, `loose`) and keeps the original start and end of every folded character. Cf characters go at every folded level, as in `anchor.resolve`'s normalised tier; `\r` stays a hard separator and is never trimmed.
2. **Word codes** (`translate_codes`): the `model/wordfind.py` set (`^^ ^p ^l ^t ^s ^- ^~ ^uNNN`), left to right. `wordfind.py` itself is not changed.
3. **Levelled search** (`hits`, `ladder`): the hits of a phrase at one level, and the ladder `exact → case → default → loose` for the ops that retry.
4. **Ops**:
   - `live.select` replaces `_found`'s casefold search with the ladder; one hit per level or `ANCHOR_AMBIGUOUS`.
   - `anchor.resolve` adds the folded stages after its normalised stage and before fuzzy. The context score still chooses between verbatim hits; it never raises `ANCHOR_AMBIGUOUS` for a hit count.
   - `find`, `term.rename`, `history.search` fold only when `match_level` is passed, search once, and tag every hit.
5. **Guard**: `locate` output is pinned by a golden file before any of this lands.

### Data flow

- **`live.select` with `find_phrase`**:
  1. Read the paragraph texts from the app (as now).
  2. Try the raw phrase at `exact`; then translate Word codes.
  3. Walk the ladder; at each level fold the joined text (cached per paragraph text) and search.
  4. Stop at the first level with hits: one hit selects, several raise `ANCHOR_AMBIGUOUS`.
  5. Reply with `match_level` and, when not `exact`, `matched_text`.
- **`anchor.resolve`**:
  1. Verbatim spans; when none, normalised spans (both as today).
  2. When none, the `case`, `default` and `loose` spans in turn.
  3. Score, accept and break ties as today; fuzzy only with `mode: "best"` and nothing accepted.
- **`find` with `match_level`**:
  1. Translate codes, fold the query at the level.
  2. Search each paragraph's cached fold; map hits back; tag each with the strictest level it meets.

### State changes

| Service | Database | Change | Description |
|---------|----------|--------|-------------|
| process memory | fold cache | CREATE | LRU of folded text per `(paragraph text, level)`, like `anchor.py`'s `_flat` |
| `.docx` on disk | — | UPDATE | Only through `term.rename`, as today |

### Technology choices

| Choice | Justification |
|--------|--------------|
| Standard library only (`unicodedata`, `re`, `bisect`) | The core's rule; Unicode categories give P*, S*, M*, Cf directly |
| Folding by Unicode category, not a character list | The owner asked for a generic rule; `--`, `¿`, `«` and curly quotes need no special cases |
| One module for every phrase op | The same phrase behaves the same in every op; one place to test and benchmark |

### Out of scope

- `locate`'s generated phrases (`find_phrase`, `find_phrase_word`) and `model/wordfind.py` (direction b).
- Regex search at folded levels.
- Fuzzy matching outside `anchor.resolve`.
- Edit ops that take addresses (`replace`, `insert`, `move`, comments).

---

## Implementation tasks

| T# | Description | Service | Size | Depends | Gate | Tests |
|----|-------------|---------|------|---------|------|-------|
| T1 | Golden file of `locate` output (`find_phrase`, `find_phrase_word`, `find_phrase_across`) over the corpus kinds, taken on the code of today, and a test that compares against it (surface: cloud) | tests | S | — | `locate_phrase_changes == 0` | `test_locate_output_matches_golden` in `tests/ops/test_locate_golden.py` — `locate` on every corpus kind is byte-identical to `tests/ops/data/locate_golden.json` |
| T2 | `model/loosefold.py`: `fold` at `case`, `default`, `loose` with the offset map; Cf dropped, NFD marks dropped, `casefold`, P* to a boundary, S* dropped at `loose`; whitespace collapse that keeps `\r` and trims spaces only; `to_original` with the punctuation widening rule; empty fold refused (surface: cloud) | model | M | — | `span_mismatches == 0` | `test_fold_spans_cut_the_matched_passage` in `tests/model/test_loosefold.py` — for random accented, punctuated text, every folded hit mapped back is a whole-character span that folds to the phrase; `test_fold_keeps_paragraph_mark_untrimmed` — `^pEstas` and `ahí^p` keep their `\r` at every level; `test_fold_drops_cf_like_normalised_tier` — every `tests/model/test_anchor_normalised.py` quote matches at `case` |
| T3 | `translate_codes`: `^^ ^p ^l ^t ^s ^- ^~ ^uNNN` left to right, other `^x` literal (surface: cloud) | model | S | — | `word_code_failures == 0` | `test_word_codes_table` in `tests/model/test_loosefold_codes.py` — each code becomes its character, `^^p` is a caret and `p`, `^x` stays literal; `test_find_phrase_word_round_trips` — for every corpus paragraph without two-form hyphens, `translate_codes(word_find(t)) == t` (`word_code_roundtrip_failures == 0`) |
| T4 | `hits` and `ladder`: hits of a phrase at one level over a list of texts, each tagged with its strictest level; the retry ladder; the variant fixture table of the spec (surface: cloud) | model | M | T1, T2, T3 | `loose_match_failures == 0` | `test_variant_table_matches` in `tests/model/test_loosefold_levels.py` — every row (`Estas ahi` against `¿Estás ahí?`, `-¿Estás ahí?`, `-Estas ahí?`, `--Estas ahi`, mid-paragraph `¿`, dashes, quotes, whitespace) is found at its expected level; `test_symbols_kept_at_default` — `5$` does not match `5` at `default` and does at `loose` |
| T5 | `live.select`: `match_level` (default `default`), the ladder in `_found` over the folded joined text, raw-phrase-first for carets, `match_level` and `matched_text` in the reply, `ANCHOR_AMBIGUOUS` with level and candidates, `ANCHOR_NOT_FOUND` with `tried` (surface: cloud) | ops | M | T4 | `exact_tiebreak_violations == 0` | `test_select_prefers_verbatim_paragraph` in `tests/live/test_select_loose.py` — with the `FakeAdapter`, one verbatim and two folded matches select the verbatim one and report `exact`; `test_select_same_level_twice_is_ambiguous` — two `default` hits raise `ANCHOR_AMBIGUOUS` listing both (`tier_ambiguity_misses == 0`); `test_select_reports_retry_level` — a phrase found only at `loose` reports `loose` (`fallback_level_misreports == 0`); `test_select_exact_level_is_todays_rule` — `match_level: "exact"` gives `_found`'s results of today on the LIVE fixtures |
| T6 | `anchor.resolve`: folded stages `case → default → loose` after the normalised stage and before fuzzy; context score, tie margin and fingerprint rule unchanged; `match_level` request and reply; `mode` × `match_level` as in spec §5 (surface: cloud) | model, ops | M | T4 | `anchor_regressions == 0` | `test_two_verbatim_hits_pick_by_context` in `tests/model/test_anchor_loose.py` — two verbatim hits with different context resolve to the better-scored one, not `ANCHOR_AMBIGUOUS`; `test_exact_level_reproduces_today` — over every anchor in `tests/model/test_anchor*.py` and the corpus, `match_level: "exact"` returns today's `at`, `score` and `match`; `test_mode_exact_with_default_level_skips_fuzzy` — a folded hit is accepted whatever its context score and fuzzy never runs; `test_folded_hit_reports_normalized_match` — `match` stays `normalized`, `match_level` says `default` |
| T7 | `find`: `match_level` for literal queries (implies `ignore_case`/`ignore_accents`, contradictions and `regex` refused), codes translated only then, per-hit `match_level`, `query_word` null at folded levels, cursor token unchanged without `match_level`, `whole_word` on the original text (surface: cloud) | model, ops | M | T4 | `symbol_fold_violations == 0` | `test_default_level_keeps_symbols` in `tests/ops/test_find_loose.py` — `5$` finds no `5` at `default` and finds it at `loose`, each hit tagged; `test_find_without_match_level_unchanged` — replies (cursor and `query_word` included) equal today's on the find fixtures (`regression_failures == 0`); `test_regex_with_match_level_refused` — `SCHEMA_INVALID` at `$.match_level` |
| T8 | `term.rename` and `history.search`: `match_level` through the T7 matcher and the fold; per-change and per-hit `match_level`; `matches_by_level`; exact offsets for folded history hits (surface: cloud) | ops, safety | M | T7 | `loose_report_missing == 0` | `test_rename_default_level_tags_every_change` in `tests/ops/test_rename_loose.py` — each `preview` change at a folded level carries its `match_level` and `matches_by_level` adds up to `counts.matches`; `test_rename_without_match_level_unchanged` — the plan equals today's on the rename fixtures; `test_history_search_folded_hit_context` in `tests/safety/test_history_search_loose.py` — a folded hit's `text` shows the hit's own context |
| T9 | Benchmark on the 100k-word novel (`generate("novel", 1)`): folded `find` against literal `find`; `live.select`'s `_found` at `default` against `exact` on the same texts, without LibreOffice (surface: cloud) | tests | S | T5, T7 | `find_loose_overhead_ratio <= 1.5` | `test_find_loose_overhead_on_novel` in `tests/bench/test_find_loose.py` — median time of `find` with `match_level: "default"` over 20 queries is at most 1.5 × literal `find`; `test_live_select_loose_extra_ms` — p95 of `_found` at `default` minus at `exact` is at most 50 ms (`live_select_loose_extra_ms <= 50`) |
| T10 | Docs: `docs/operations.md` regenerated from the registry, `match_level` and the two meanings of "exact" in the op descriptions, the owner's `--` dialogue case for `scripts/real_word_check` notes (surface: cloud) | docs | S | T5, T6, T8 | `operations_doc_drift == 0` | `test_operations_md_is_generated` in `tests/test_docs.py` — `docs/operations.md` equals `operations_markdown()` with the new `match_level` fields |

**Status legend**: ☐ not started · ◐ in progress · ☑ merged

**Merge order**:
1. T1, T2 and T3, in parallel (T1 must land before any op changes).
2. T4.
3. T5, T6 and T7, in parallel.
4. T8.
5. T9 and T10.

**Branch pattern**: `find-T<N>-description` from the default branch.

### Dependency graph

```drawspec
{"version": 1, "kind": "flow", "title": "Task dependencies",
 "nodes": [
  {"id": "T1", "text": "T1\nLocate golden"},
  {"id": "T2", "text": "T2\nFold + offsets"},
  {"id": "T3", "text": "T3\nWord codes"},
  {"id": "T4", "text": "T4\nHits, ladder"},
  {"id": "T5", "text": "T5\nlive.select"},
  {"id": "T6", "text": "T6\nanchor.resolve"},
  {"id": "T7", "text": "T7\nfind"},
  {"id": "T8", "text": "T8\nrename, history"},
  {"id": "T9", "text": "T9\nBenchmark"},
  {"id": "T10", "text": "T10\nDocs"}
 ],
 "edges": [
  {"from": "T1", "to": "T4"},
  {"from": "T2", "to": "T4"},
  {"from": "T3", "to": "T4"},
  {"from": "T4", "to": "T5"},
  {"from": "T4", "to": "T6"},
  {"from": "T4", "to": "T7"},
  {"from": "T7", "to": "T8"},
  {"from": "T5", "to": "T9"},
  {"from": "T7", "to": "T9"},
  {"from": "T5", "to": "T10"},
  {"from": "T6", "to": "T10"},
  {"from": "T8", "to": "T10"}
 ]}
```

## Evidence log

| T# | Gate | Measured | Command | SHA | Env | Date |
|----|------|----------|---------|-----|-----|------|

---

## Sign-off

- [ ] Plan reviewed by the owner
- [ ] Contracts agreed with literary-critic (`match_level`, `matched_text`)
- [ ] Ready for execution
