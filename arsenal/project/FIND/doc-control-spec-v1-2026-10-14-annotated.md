# Doc Control — Specification (annotated edition)

> Generated 2026-10-14. This is the document with a **note slot** after every section that has text of its own. Read it in any Markdown app. To annotate, replace the `_(your notes…)_` placeholder under any section. When done, send the file back — notes are acted on.

---

# Spec

## Preamble & scope

**Date**: 2026-10-14
**Ticket / PR**: — (workspace `FIND`)
**Author**: Claude (doc-control session), for the owner
**Revision**: 1
**Status**: draft
**Revision log**:
- r1 — first draft, from the owner's report that literary-critic's find phrase missed a dialogue paragraph opening with `--`

> Review record. Each round of reviewer notes makes a new revision: bump
> **Revision**, add a log line naming the export it applied, and commit that
> export beside this file in the same commit. On approval set **Status** to
> `approved (YYYY-MM-DD, revision N)` and, when the reviewer sent no notes for
> that revision, add `— without annotations`.

<!-- -->

> **✎ Notes** · `SPEC · intro`
> _(your notes here — replace this line)_

## §1 Problem statement

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

- [ ] `loose_match_failures == 0` on a fixture table of variants. Each row is a document paragraph and a phrase that differs by accent, case, punctuation or dash form, whitespace, or a leading `-`, `--`, `—`, `¿` or `«`. The table includes `Estas ahi` against `¿Estás ahí?`, `-¿Estás ahí?`, `-Estas ahí?` and `--Estas ahi`.
- [ ] `exact_tiebreak_violations == 0`: when one paragraph matches the phrase exactly and others match loosely, the exact one is returned, in every fixture that mixes tiers.
- [ ] `tier_ambiguity_misses == 0`: two matches in the same tier still raise `ANCHOR_AMBIGUOUS`; none is silently chosen.
- [ ] `span_mismatches == 0`: for every loose match, the returned range, cut from the original text, is the matched passage (property test on random text with accents and punctuation).
- [ ] `locate_phrase_changes == 0`: `locate` output on the corpus is byte-identical before and after (direction b is untouched).
- [ ] `loose_report_missing == 0`: every response where the tier was not exact says so (`match` tier field).
- [ ] `find_loose_overhead_ratio <= 1.5` against the current literal `find` on the 100k-paragraph benchmark document in `tests/bench`; `live.select` stays under the current time plus 50 ms on the same document.
- [ ] No existing test changes behaviour: `exact` mode reproduces today's results (`regression_failures == 0`).
- [ ] Judged, not measured: the owner re-runs the original `--` dialogue case from literary-critic and it is found.

<!-- -->

> **✎ Notes** · `SPEC §1`
> _(your notes here — replace this line)_

## §2 Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| `ops/live.py` `_found` | Primary | Resolves `find_phrase` for `live.select` | Yes | Uses the tiered matcher; reports the tier | High |
| `ops/find.py`, `model/search.py` | Primary | Literal and regex search | Option B: Yes | New `match` mode for literal queries | Med |
| New `model/loosefold.py` (Option B) | Primary | Fold, offset map, tiered search | Yes (new) | One shared matcher | High |
| `model/anchor.py` | Dependent | Exact, normalised, fuzzy tiers | Option B: aligns tiers | Adds a loose tier before fuzzy | Med |
| `ops/rename.py`, `ops/history.py` | Dependent | Use the `find` matcher | Option B: Yes | Inherit the mode or opt out | Low |
| `ops/locate.py`, `model/wordfind.py` | Dependent | Generate Word-exact phrases | No (guarded by a test) | Must not change | High if touched |
| `live/word_com.py`, `live/lo_uno.py` | Dependent | Select a paragraph by index | No | Receive a paragraph index as today | Low |
| `tests/bench` | Validation | Perf tests | Yes | New loose-match benchmark | Low |
| literary-critic | Consumer | Sends find phrases | No | Fewer misses; must read the tier | Low |

**Impact**:
- **API**: additive. A response field names the tier; a request field `match` (`"loose"` default, `"exact"`) opts out. Defaults change for `live.select`, so some calls that failed now succeed.
- **Data**: none. These are read ops or selections.
- **Performance**: folding each paragraph costs linear time. Cache the folded text per document revision.
- **User**: a wrong loose pick is visible and recoverable (the owner's rule). The tier report lets the agent notice it.
- **Risk of inaction**: agents keep missing paragraphs on punctuation they cannot see. Dialogue in Spanish and French is the common case.

<!-- -->

> **✎ Notes** · `SPEC §2`
> _(your notes here — replace this line)_

## §3 Options

### Option A: Loose matching in live.select and find only (Conservative)

- **Description**: add a small `loose_fold` helper. `_found` searches in three tiers over the joined text. `find` gets the same tiers for literal queries when `match` is `"loose"`.
- **Scope**: `ops/live.py`, `ops/find.py`, `model/search.py`, tests.
- **Effort**: Small.
- **Tradeoffs**: fast to ship and covers the reported case. `anchor.resolve`, `rename` and `history.search` keep their own rules, so the same phrase can match in one op and not in another.
- **Compatibility**: additive. `match: "exact"` restores today's behaviour.

<!-- -->

> **✎ Notes** · `SPEC › Option A: Loose matching in live.select and find only (Conservative)`
> _(your notes here — replace this line)_

### Option B: One shared matcher module for every phrase-taking op (Recommended)

- **Description**: a new module `model/loosefold.py` owns the folding, the offset map and the tier search. `live.select`, `find`, `anchor.resolve`, `rename` and `history.search` call it. `locate` does not.
- **Scope**: the module, the five ops above, response fields, docs/operations.md, tests and benchmarks.
- **Effort**: Medium.
- **Tradeoffs**: one rule everywhere, one place to test and tune. More files touched, and `anchor.resolve`'s existing normalised tier must be merged without changing its scores.
- **Compatibility**: additive with the same `match` opt-out.

<!-- -->

> **✎ Notes** · `SPEC › Option B: One shared matcher module for every phrase-taking op (Recommended)`
> _(your notes here — replace this line)_

### Option C: Loose matching only when the exact search fails (Fallback-only)

- **Description**: keep exact matching first. Only when it finds nothing, retry loosely. No tiering among several hits.
- **Scope**: `_found` and `find`.
- **Effort**: Small.
- **Tradeoffs**: simplest to reason about. But a phrase that matches one paragraph exactly and others loosely is handled only because exact is tried first; there is no case-only tier, and two exact hits are not told apart from loose ones in the report.
- **Compatibility**: additive.

<!-- -->

> **✎ Notes** · `SPEC › Option C: Loose matching only when the exact search fails (Fallback-only)`
> _(your notes here — replace this line)_

### The folding rule (all options)

Both the phrase and each paragraph are folded to a comparison string, and an offset map keeps the position of every folded character in the original text.

- **Accents**: NFD, then drop combining marks (category M). `Estás` becomes `estas`.
- **Case**: `casefold`.
- **Punctuation and dashes**: every character in Unicode category P* is dropped. The sequence `--` is dropped as well. `¿`, `?`, `-`, `—`, `–`, `«` and curly quotes all fold away. Symbol categories (S*) stay, so `$` and `+` still count.
- **Whitespace**: runs of whitespace (including no-break space and tabs) become one space. Leading and trailing space is trimmed, and a space left next to dropped punctuation is merged.
- **Offsets**: each folded character records the original start and end. A match from folded positions `i..j` maps to the original span from the start of character `i` to the end of character `j-1`. The span then widens to take punctuation that touches the match on its own side only when the phrase itself began or ended with punctuation. Combining marks stay with their base. The result is a normal `p142#…:s-e` range, as `find` returns today.
- **Empty phrase after folding** (for example a phrase of only `--`): matched exactly only; loose matching is refused with `SCHEMA_INVALID`, since it would match every paragraph.
- **Prefix match**: for `live.select`, a phrase that begins a paragraph may match at the folded start of the paragraph, so `Estas ahi` finds `-¿Estás ahí?`.

<!-- -->

> **✎ Notes** · `SPEC › The folding rule (all options)`
> _(your notes here — replace this line)_

### Tiebreak tiers

1. **exact**: the phrase equals the text verbatim.
2. **case-only**: equal after `casefold` (today's `live.select` rule).
3. **loose**: equal after the full fold.

The search takes the first tier that has any match. Within that tier, one match is the answer. Two or more raise `ANCHOR_AMBIGUOUS` (as now), and the error lists the tier and the candidate addresses. A looser tier is never consulted once a stricter one matches, so one exact paragraph beats any number of loose ones.

<!-- -->

> **✎ Notes** · `SPEC › Tiebreak tiers`
> _(your notes here — replace this line)_

### Reporting a loose match

Responses carry `"match": "exact" | "case" | "loose"`, the same key `anchor.resolve` already uses. For a loose match the response also carries `matched_text`, the original text of the span, so the agent can compare it with what it asked for. `live.select` already returns the paragraph text. `find` marks each hit.

<!-- -->

> **✎ Notes** · `SPEC › Reporting a loose match`
> _(your notes here — replace this line)_

### Opt-out

`match: "exact"` on each phrase-taking op restores today's behaviour. `"loose"` is the default for `live.select`, `anchor.resolve`, `rename`'s search and `history.search`. For `find`, the existing `ignore_case`, `ignore_accents` and `whole_word` flags stay as they are; `match: "loose"` is a new value that implies both and the punctuation fold. Its default is open (Q3 below).

<!-- -->

> **✎ Notes** · `SPEC › Opt-out`
> _(your notes here — replace this line)_

### Performance

- Folded text and offset maps are built once per paragraph and cached with the `Document` index; a search over N paragraphs is O(total characters).
- The three tiers share one pass: the exact and case tiers run first on the raw and casefolded text. The loose tier is built only when both are empty.
- `locate` indexes are unchanged.
- The repo's `tests/bench` documents are the base. A new benchmark runs the three tiers on a 100k-paragraph document.

<!-- -->

> **✎ Notes** · `SPEC › Performance`
> _(your notes here — replace this line)_

### Comparison

| | Option A | Option B | Option C |
|---|---|---|---|
| Effort | Small | Medium | Small |
| Risk | Low | Medium (anchor tiers merge) | Low |
| Completeness | Covers the reported case | Same rule in every op | Misses the exact-wins rule |
| Compatibility | Additive | Additive | Additive |
| Maintenance | Two matchers | One matcher | Two matchers |

<!-- -->

> **✎ Notes** · `SPEC › Comparison`
> _(your notes here — replace this line)_

## §4 Recommendation

**Recommended option**: Option B. The owner asked for a generic rule, and a shared module is the only way to make the same phrase behave the same in `live.select`, `find`, `anchor.resolve`, `rename` and `history.search`. Ship it in two steps: the module with `live.select` first (the reported failure), then the other ops. `locate` stays exact by a regression test.

**Immediate next action**: after approval, `design` the plan, starting with the fixture table of variants and the tier tests, so the matcher is written against its gate.

**Open questions**:
- [ ] Q1. Should the fold also drop symbols (category S*, for example `$`, `*`, `+`) or only punctuation (P*)? Recommended: punctuation only.
- [ ] Q2. Should a loose match be allowed inside a paragraph (not only at its start)? `find` already finds inside paragraphs. Recommended: yes, same tiers.
- [ ] Q3. Should `find` default to `match: "loose"`? It changes the meaning of a plain query. Recommended: no, keep `find` exact by default and make loose the default only for the phrase ops that must land on one paragraph.
- [ ] Q4. Should a loose match that needs a second look (for example, folding removed digits' neighbours) be refused? Recommended: no refusal; the report is enough.
- [ ] Q5. Should the fuzzy tier of `anchor.resolve` stay as is after the loose tier? Recommended: yes.

**Decisions log**: none (the work did not start in `explore-idea`). The owner's rules above are constraints: loose by default, exact paragraph wins, wrong picks are recoverable, `locate` phrases stay exact.


> Sections 5–6 (contracts, risks) are appended by `design`.

<!-- -->

> **✎ Notes** · `SPEC §4`
> _(your notes here — replace this line)_

