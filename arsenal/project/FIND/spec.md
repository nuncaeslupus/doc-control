# Specification: loose find matching

**Date**: 2026-10-14
**Ticket / PR**: — (workspace `FIND`)
**Author**: Claude (doc-control session), for the owner
**Revision**: 2
**Status**: approved (2026-10-14, revision 2)
**Revision log**:
- r1 — first draft, from the owner's report that literary-critic's find phrase missed a dialogue paragraph opening with `--`
- r2 — owner's answers of 2026-10-14 applied (chat; no notes export): Option B chosen, Q1–Q5 answered (D-F1..D-F6)

> Review record. Each round of reviewer notes makes a new revision: bump
> **Revision**, add a log line naming the export it applied, and commit that
> export beside this file in the same commit. On approval set **Status** to
> `approved (YYYY-MM-DD, revision N)` and, when the reviewer sent no notes for
> that revision, add `— without annotations`.

---

## 1. Problem statement

A dialogue paragraph in the writer's document began with `--` (two hyphens, not an em dash or a hyphen). literary-critic's find phrase did not match it. Today every phrase an agent sends must match the document's characters exactly, apart from case. The agent cannot know which dash, quote or accent the writer typed.

The owner's rule: make matching generic. Asked for a paragraph that begins with `Estas ahi`, doc-control should also accept `¿Estás ahí?`, `-¿Estás ahí?` and `-Estas ahí?`. The same holds for other punctuation. If two paragraphs match and one is exactly as asked, use that one. If the agent picked wrong, the user will say so and the passage can be found again.

Two directions must stay apart:

- **(a) Matching an agent's phrase.** This goes loose, with an exact-match tiebreak. It is the subject of this spec.
- **(b) Phrases doc-control generates** for Word's own Find box (`locate`: `find_phrase`, `find_phrase_word`). These stay exact. Word's Find is case-insensitive and accent-sensitive, and a generated phrase must be unique under that rule. Loosening them would make them wrong in Word.

Current rules, from the code:

- `locate` (`ops/locate.py`): case-insensitive (`casefold`), accent-sensitive, Word's Find semantics. Whitespace is not collapsed.
- `find` (`ops/find.py`, `model/search.py`): literal or regex. `ignore_case` and `ignore_accents` are opt-in flags, both default false. Offsets map back to the original text.
- `live.select` (`ops/live.py`, `_found`): `casefold()` on the joined text, then a plain substring search. One hit is required (`ANCHOR_NOT_FOUND` for none, `ANCHOR_AMBIGUOUS` for several). No accent or punctuation folding.
- `anchor.resolve` (`model/anchor.py`): exact, then a normalised tier (soft hyphens, zero-width characters, whitespace, NFC), then fuzzy for quotes of more than three words. It already reports the tier as `match`.

Ops that take a text phrase today:

| Op | Parameter | Current matching |
|----|-----------|------------------|
| `live.select` | `find_phrase` | casefold, substring, must be unique |
| `find` | `query` | literal or regex; folding is opt-in |
| `anchor.resolve` | `exact` (+ prefix, suffix) | exact, normalised, fuzzy |
| `rename` | search term | same matcher as `find` |
| `history.search` | `query` | `ignore_case`, `ignore_accents` flags |

Edit ops (`replace`, `insert`, `move`, comments) take addresses, not phrases. An agent reaches them through `find`, `anchor.resolve` or `live.select`. Loose matching in those three covers every edit.

**Success criteria (measurable)**:

- [ ] `loose_match_failures == 0` on a fixture table of variants. Each row is a document paragraph and a phrase that differs by accent, case, punctuation or dash form, whitespace, or a leading `-`, `--`, `—`, `¿` or `«`, at the start or in the middle of the paragraph. The table includes `Estas ahi` against `¿Estás ahí?`, `-¿Estás ahí?`, `-Estas ahí?` and `--Estas ahi`, and a phrase without `¿` against a paragraph that has it mid-text (D-F2).
- [ ] `symbol_fold_violations == 0`: at the default level a symbol (`$`, `+`, `*`) is never folded, so `5$` does not match `5`; at the `loose` level it does. Rows cover both levels (D-F1).
- [ ] `fallback_level_misreports == 0`: when a phrase is not found at the default level, the op retries at looser levels on its own, and every reply names the level that matched (D-F1).
- [ ] `word_code_failures == 0` on a fixture table: `^p` equals `\r`, `^t` a tab, `^l` a manual line break where the op supports it, `^^` a literal caret, and an unknown `^x` stays literal (D-F6).
- [ ] `exact_tiebreak_violations == 0`: when one paragraph matches the phrase exactly and others match loosely, the exact one is returned, in every fixture that mixes tiers.
- [ ] `tier_ambiguity_misses == 0`: two matches in the same level still raise `ANCHOR_AMBIGUOUS`; none is silently chosen.
- [ ] `span_mismatches == 0`: for every loose match, the returned range, cut from the original text, is the matched passage (property test on random text with accents and punctuation).
- [ ] `locate_phrase_changes == 0`: `locate` output on the corpus is byte-identical before and after (direction b is untouched, including no `^` codes in generated phrases).
- [ ] `loose_report_missing == 0`: every response where the level was not exact says so (`match` level field).
- [ ] `find_loose_overhead_ratio <= 1.5` against the current literal `find` on the 100k-paragraph benchmark document in `tests/bench`; `live.select` stays under the current time plus 50 ms on the same document.
- [ ] No existing test changes behaviour: `match: "exact"` reproduces today's results, and plain `find` with no `match` argument is unchanged (`regression_failures == 0`, D-F5).
- [ ] Judged, not measured: the owner re-runs the original `--` dialogue case from literary-critic and it is found.

## 2. Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| `ops/live.py` `_found` | Primary | Resolves `find_phrase` for `live.select` | Yes | Uses the tiered matcher; reports the tier | High |
| `ops/find.py`, `model/search.py` | Primary | Literal and regex search | Option B: Yes | New `match` levels for literal queries; default stays exact | Med |
| New `model/loosefold.py` (Option B) | Primary | Fold, offset map, levelled search, Word-code translation | Yes (new) | One shared matcher | High |
| `model/anchor.py` | Dependent | Exact, normalised, fuzzy tiers | Option B: aligns tiers | Adds a loose tier before fuzzy | Med |
| `ops/rename.py`, `ops/history.py` | Dependent | Use the `find` matcher | Option B: Yes | Inherit the mode or opt out | Low |
| `ops/locate.py`, `model/wordfind.py` | Dependent | Generate Word-exact phrases | No (guarded by a test) | Must not change | High if touched |
| `live/word_com.py`, `live/lo_uno.py` | Dependent | Select a paragraph by index | No | Receive a paragraph index as today | Low |
| `tests/bench` | Validation | Perf tests | Yes | New loose-match benchmark | Low |
| literary-critic | Consumer | Sends find phrases | No | Fewer misses; must read the level | Low |

**Impact**:
- **API**: additive. A response field names the level; a request field `match` (`"exact"`, `"default"`, `"loose"`) sets it. `"default"` is the default for the phrase ops, which retry looser levels before failing, so some calls that failed now succeed. Plain `find` stays exact. Agent phrases also accept Word's find codes (`^p`, `^t`, `^l`, `^^`).
- **Data**: none. These are read ops or selections.
- **Performance**: folding each paragraph costs linear time. Cache the folded text per document revision.
- **User**: a wrong loose pick is visible and recoverable (the owner's rule). The level report lets the agent notice it.
- **Risk of inaction**: agents keep missing paragraphs on punctuation they cannot see. Dialogue in Spanish and French is the common case.

## 3. Options

### Option A: Loose matching in `live.select` and `find` only (Conservative)

- **Description**: add a small `loose_fold` helper. `_found` searches in three tiers over the joined text. `find` gets the same tiers for literal queries when `match` is `"loose"`.
- **Scope**: `ops/live.py`, `ops/find.py`, `model/search.py`, tests.
- **Effort**: Small.
- **Tradeoffs**: fast to ship and covers the reported case. `anchor.resolve`, `rename` and `history.search` keep their own rules, so the same phrase can match in one op and not in another.
- **Compatibility**: additive. `match: "exact"` restores today's behaviour.

### Option B: One shared matcher module for every phrase-taking op (Recommended)

- **Description**: a new module `model/loosefold.py` owns the folding, the offset map, the level search and the Word-code translation. `live.select`, `find`, `anchor.resolve`, `rename` and `history.search` call it. `locate` does not.
- **Scope**: the module, the five ops above, the `match` parameter and response fields, docs/operations.md, tests and benchmarks.
- **Effort**: Medium.
- **Tradeoffs**: one rule everywhere, one place to test and tune. More files touched, and `anchor.resolve`'s existing normalised tier must be merged without changing its scores.
- **Compatibility**: additive with the same `match` opt-out.

### Option C: Loose matching only when the exact search fails (Fallback-only)

- **Description**: keep exact matching first. Only when it finds nothing, retry loosely. No tiering among several hits.
- **Scope**: `_found` and `find`.
- **Effort**: Small.
- **Tradeoffs**: simplest to reason about. But a phrase that matches one paragraph exactly and others loosely is handled only because exact is tried first; there is no case-only tier, and two exact hits are not told apart from loose ones in the report.
- **Compatibility**: additive.

### Match levels (D-F1)

`match` takes three values, from strictest to loosest:

- `"exact"`: today's behaviour for that op. No folding, no retry.
- `"default"`: case and accents are ignored, and punctuation and dashes are folded away. Symbols (S*) still count.
- `"loose"`: everything in `"default"`, and symbols are dropped as well. An opt-in level; it never applies unless asked for or reached by the retry below.

**Defaults**: the phrase ops (`live.select`, `anchor.resolve`, `rename`'s search, `history.search`) default to `"default"`. Plain `find` defaults to `"exact"` (D-F5) and only folds when the caller passes `match`.

**Retry before failing**: when a phrase is not found at the level in force, a phrase op tries the less restrictive levels itself, one at a time, before it raises `ANCHOR_NOT_FOUND`. Each level keeps the exact-first tiebreak below. `"exact"` never retries. For `find`, retry applies only when `match` is `"default"` or `"loose"`. The reply names the level that matched.

### The folding rule

Both the phrase and each paragraph are folded to a comparison string, and an offset map keeps the position of every folded character in the original text.

- **Accents**: NFD, then drop combining marks (category M). `Estás` becomes `estas`.
- **Case**: `casefold`.
- **Punctuation and dashes** (levels `default` and `loose`): every character in Unicode category P* is dropped. The sequence `--` is dropped as well. `¿`, `?`, `-`, `—`, `–`, `«` and curly quotes all fold away.
- **Symbols** (level `loose` only): every character in category S* (`$`, `*`, `+`, `=`, `<`, `^` and so on) is dropped. At `default` they stay.
- **Whitespace**: runs of whitespace (including no-break space and tabs) become one space. Leading and trailing space is trimmed, and a space left next to dropped characters is merged.
- **Offsets**: each folded character records the original start and end. A match from folded positions `i..j` maps to the original span from the start of character `i` to the end of character `j-1`. The span then widens to take punctuation that touches the match on its own side only when the phrase itself began or ended with punctuation. Combining marks stay with their base. The result is a normal `p142#…:s-e` range, as `find` returns today.
- **Empty phrase after folding** (for example a phrase of only `--`, or only symbols at `loose`): matched exactly only; folded matching is refused with `SCHEMA_INVALID`, since it would match every paragraph.
- **Position**: a match may start anywhere in a paragraph, not only at its start (D-F2). `Estas ahi` finds `-¿Estás ahí?`, and a phrase without `¿` finds a passage that has `¿` in the middle of a paragraph. `find` already searches inside paragraphs; the folded levels do the same.

### Word find codes in agent phrases (D-F6)

Word's Find box accepts special codes, and they are useful to an agent. Before matching, agent phrases are translated, left to right, in `model/loosefold.py`:

| Code | Meaning | Becomes |
|------|---------|---------|
| `^p` | paragraph mark | `\r`, as an alias |
| `^t` | tab | `\t`, where the op supports it |
| `^l` | manual line break | the line-break character, where the op supports it |
| `^^` | a literal caret | `^` |

Any other `^x` stays literal (further codes are not promised). Because translation runs left to right, `^^p` is a caret followed by `p`, not a paragraph mark. Word's simple Find box in the Navigation pane also takes `^p` and `^t`, so a phrase an agent copies from Word keeps its meaning. Translation happens before folding; a literal `^` that the writer typed is matched with `^^` (at `loose` it is a symbol and is dropped like any other).

`locate`'s generated phrases (`find_phrase`, `find_phrase_word`) do not change and do not emit `^` codes (direction b).

### Tiebreak levels

1. **exact**: the phrase equals the text verbatim.
2. **case-only**: equal after `casefold` (today's `live.select` rule).
3. **default**: equal after the accent, case and punctuation fold.
4. **loose**: equal after the symbol fold as well.

The search takes the first level that has any match, starting from the strictest and escalating only as far as the retry rule allows. Within that level, one match is the answer. Two or more raise `ANCHOR_AMBIGUOUS` (as now), and the error lists the level and the candidate addresses. A looser level is never consulted once a stricter one matches, so one exact paragraph beats any number of looser ones.

### Reporting a loose match

Responses carry `"match": "exact" | "case" | "default" | "loose"`, the level that matched, in the same key `anchor.resolve` already uses. Whenever the level is not `exact` the response also carries `matched_text`, the original text of the span, so the agent can compare it with what it asked for. `live.select` already returns the paragraph text. `find` marks each hit.

### Opt-out

`match: "exact"` on each phrase-taking op restores today's behaviour. For `find`, the existing `ignore_case`, `ignore_accents` and `whole_word` flags stay as they are; `match: "default"` implies the first two plus the punctuation fold, and `match: "loose"` adds the symbol fold. Omitting `match` leaves `find` exactly as it is today (D-F5).

### Performance

- Folded text and offset maps are built once per paragraph and cached with the `Document` index; a search over N paragraphs is O(total characters).
- The levels share one pass: the exact and case levels run first on the raw and casefolded text. The folded levels are built only when both are empty, and the symbol-folded text only when the punctuation-folded search is empty too.
- `locate` indexes are unchanged.
- The repo's `tests/bench` documents are the base. A new benchmark runs the four levels on a 100k-paragraph document.

### Comparison

| | Option A | Option B | Option C |
|---|---|---|---|
| Effort | Small | Medium | Small |
| Risk | Low | Medium (anchor tiers merge) | Low |
| Completeness | Covers the reported case | Same rule in every op | Misses the exact-wins rule |
| Compatibility | Additive | Additive | Additive |
| Maintenance | Two matchers | One matcher | Two matchers |

## 4. Recommendation

**Recommended option**: Option B. The owner asked for a generic rule, and a shared module is the only way to make the same phrase behave the same in `live.select`, `find`, `anchor.resolve`, `rename` and `history.search`. The owner chose it on 2026-10-14 (D-F1). Ship it in two steps: the module with `live.select` first (the reported failure), then the other ops. `locate` stays exact by a regression test.

**Immediate next action**: after approval, `design` the plan, starting with the fixture table of variants and the tier tests, so the matcher is written against its gate.

**Open questions**: none. The owner answered Q1–Q5 in r2 (see D-F1..D-F6):
- [x] Q1. Symbols (S*): not folded by default; dropped only at the opt-in `loose` level, with an automatic retry (D-F1).
- [x] Q2. Matching inside a paragraph: yes, same levels (D-F2).
- [x] Q3. `find` default: stays exact (D-F5); Word find codes `^p`, `^t`, `^l`, `^^` are added to agent phrases (D-F6).
- [x] Q4. Refuse a loose match that needs a second look: no refusal; the level report is enough (D-F4).
- [x] Q5. Fuzzy tier of `anchor.resolve`: stays as is, after the loose level (D-F3).

**Decisions log**:

| ID | Decision | Status | Date |
|----|----------|--------|------|
| D-F1 | Option B. Symbols (S*) are not folded by default. `match: "exact" \| "default" \| "loose"`; `"loose"` also drops symbols. A phrase not found at the default level is retried at looser levels before failing, exact-first tiebreak kept, and the reply says which level matched | agreed | 2026-10-14 |
| D-F2 | Matching works in the middle of a paragraph too, not only at its start; a phrase without `¿` must still match | agreed | 2026-10-14 |
| D-F3 | The fuzzy tier of `anchor.resolve` stays as is, after the loose level (Q5, as recommended) | agreed | 2026-10-14 |
| D-F4 | No refusal of loose matches that look doubtful; the level report is enough (Q4, as recommended) | agreed | 2026-10-14 |
| D-F5 | Plain `find` stays exact by default; folding only when `match` is passed | agreed | 2026-10-14 |
| D-F6 | Agent phrases accept Word's find codes: `^p` (alias for `\r`), `^t`, `^l` where the op supports them, `^^` for a literal caret. `locate`'s generated phrases do not change | agreed | 2026-10-14 |

---

> Sections 5–6 (contracts, risks) are appended by `design`.
