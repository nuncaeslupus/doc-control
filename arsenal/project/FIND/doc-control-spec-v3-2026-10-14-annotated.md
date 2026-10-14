# Doc Control — Specification (annotated edition)

> Generated 2026-10-14. This is the document with a **note slot** after every section that has text of its own. Read it in any Markdown app. To annotate, replace the `_(your notes…)_` placeholder under any section. When done, send the file back — notes are acted on.

---

# Spec

## Preamble & scope

**Date**: 2026-10-14
**Ticket / PR**: — (workspace `FIND`)
**Author**: Claude (doc-control session), for the owner
**Revision**: 3
**Status**: approved (2026-10-14, revision 3) — without annotations
**Revision log**:
- r1 — first draft, from the owner's report that literary-critic's find phrase missed a dialogue paragraph opening with `--`
- r2 — owner's answers of 2026-10-14 applied (chat; no notes export): Option B chosen, Q1–Q5 answered (D-F1..D-F6)
- r3 — cold-review clarifications (D-F7): parameter renamed `match_level` so `anchor.resolve`'s `match` is untouched; `rename`/`history.search` stay exact by default and never retry; multi-hit ops return every hit; Word-code set aligned with `model/wordfind.py`; punctuation folds to a word boundary; benchmark is the 100k-word novel

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

- [ ] `loose_match_failures == 0` on a fixture table of variants. Each row is a document paragraph and a phrase that differs by accent, case, punctuation or dash form, whitespace, or a leading `-`, `--`, `—`, `¿` or `«`, at the start or in the middle of the paragraph. The table includes `Estas ahi` against `¿Estás ahí?`, `-¿Estás ahí?`, `-Estas ahí?` and `--Estas ahi`, and a phrase without `¿` against a paragraph that has it mid-text (D-F2).
- [ ] `symbol_fold_violations == 0`: at the default level with no retry (`find` with `match_level: "default"`) a symbol (`$`, `+`, `*`) is never folded, so `5$` does not match `5`; at `loose` it does; and a phrase op that only matches after retrying to `loose` reports `match_level: "loose"`, never `"default"`. Rows cover both levels (D-F1).
- [ ] `fallback_level_misreports == 0`: when a phrase is not found at the default level, the op retries at looser levels on its own, and every reply names the level that matched (D-F1).
- [ ] `word_code_failures == 0` on a fixture table: `^p` equals `\r` (also mid-phrase, across paragraphs where the op allows it), `^t` a tab, `^s`, `^-`, `^~` and `^uNNN` their characters, `^l` a manual line break where the op supports it, `^^` a literal caret, and an unknown `^x` stays literal (D-F6).
- [ ] `exact_tiebreak_violations == 0`: when one paragraph matches the phrase exactly and others match loosely, the exact one is returned, in every fixture that mixes tiers.
- [ ] `tier_ambiguity_misses == 0`: two matches in the same level still raise `ANCHOR_AMBIGUOUS`; none is silently chosen.
- [ ] `span_mismatches == 0`: for every loose match, the returned range, cut from the original text, is the matched passage (property test on random text with accents and punctuation).
- [ ] `locate_phrase_changes == 0`: `locate` output on the corpus is byte-identical before and after (direction b is untouched); and every `find_phrase_word` it emits, translated back, equals its source text (`word_code_roundtrip_failures == 0`).
- [ ] `loose_report_missing == 0`: every response where the level was not exact says so (`match_level` field).
- [ ] `find_loose_overhead_ratio <= 1.5` against the current literal `find` on the 100k-word novel benchmark in `tests/bench`; `live.select` stays under the current time plus 50 ms on the same document.
- [ ] No existing test changes behaviour: `match_level: "exact"` reproduces today's results, and plain `find`, `rename` and `history.search` with no `match_level` argument is unchanged (`regression_failures == 0`, D-F5).
- [ ] Judged, not measured: the owner re-runs the original `--` dialogue case from literary-critic and it is found.

<!-- -->

> **✎ Notes** · `SPEC §1`
> _(your notes here — replace this line)_

## §2 Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| `ops/live.py` `_found` | Primary | Resolves `find_phrase` for `live.select` | Yes | Uses the tiered matcher; reports the tier | High |
| `ops/find.py`, `model/search.py` | Primary | Literal and regex search | Option B: Yes | New `match_level` levels for literal queries; default stays exact | Med |
| New `model/loosefold.py` (Option B) | Primary | Fold, offset map, levelled search, Word-code translation | Yes (new) | One shared matcher | High |
| `model/anchor.py` | Dependent | Exact, normalised, fuzzy tiers | Option B: aligns tiers | Adds a loose tier before fuzzy | Med |
| `ops/rename.py`, `ops/history.py` | Dependent | Use the `find` matcher | Option B: Yes | Inherit the mode or opt out | Low |
| `ops/locate.py`, `model/wordfind.py` | Dependent | Generate Word-exact phrases | No (guarded by a test) | Must not change | High if touched |
| `live/word_com.py`, `live/lo_uno.py` | Dependent | Select a paragraph by index | No | Receive a paragraph index as today | Low |
| `tests/bench` | Validation | Perf tests | Yes | New loose-match benchmark | Low |
| literary-critic | Consumer | Sends find phrases | No | Fewer misses; must read the level | Low |

**Impact**:
- **API**: additive. A response field names the level; a request field `match_level` (`"exact"`, `"default"`, `"loose"`) sets it. `"default"` is the default for the single-target phrase ops (`live.select`, `anchor.resolve`), which retry looser levels before failing, so some calls that failed now succeed. Plain `find`, `rename` and `history.search` stay exact. Agent phrases also accept Word's find codes (`^p`, `^t`, `^l`, `^^`).
- **Data**: none. These are read ops or selections.
- **Performance**: folding each paragraph costs linear time. Cache the folded text per document revision.
- **User**: a wrong loose pick is visible and recoverable (the owner's rule). The level report lets the agent notice it.
- **Risk of inaction**: agents keep missing paragraphs on punctuation they cannot see. Dialogue in Spanish and French is the common case.

<!-- -->

> **✎ Notes** · `SPEC §2`
> _(your notes here — replace this line)_

## §3 Options

### Option A: Loose matching in live.select and find only (Conservative)

- **Description**: add a small `loose_fold` helper. `_found` searches in three tiers over the joined text. `find` gets the same tiers for literal queries when `match_level` is `"loose"`.
- **Scope**: `ops/live.py`, `ops/find.py`, `model/search.py`, tests.
- **Effort**: Small.
- **Tradeoffs**: fast to ship and covers the reported case. `anchor.resolve`, `rename` and `history.search` keep their own rules, so the same phrase can match in one op and not in another.
- **Compatibility**: additive. `match_level: "exact"` restores today's behaviour.

<!-- -->

> **✎ Notes** · `SPEC › Option A: Loose matching in live.select and find only (Conservative)`
> _(your notes here — replace this line)_

### Option B: One shared matcher module for every phrase-taking op (Recommended)

- **Description**: a new module `model/loosefold.py` owns the folding, the offset map, the level search and the Word-code translation. `live.select`, `find`, `anchor.resolve`, `rename` and `history.search` call it. `locate` does not.
- **Scope**: the module, the five ops above, the `match_level` parameter and response field, docs/operations.md, tests and benchmarks.
- **Effort**: Medium.
- **Tradeoffs**: one rule everywhere, one place to test and tune. More files touched, and `anchor.resolve`'s existing normalised tier must be merged without changing its scores.
- **Compatibility**: additive with the same `match_level` opt-out.

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

### Match levels (D-F1)

`match_level` takes three values, from strictest to loosest:

- `"exact"`: today's behaviour for that op. No folding, no retry.
- `"default"`: case and accents are ignored, and punctuation and dashes are folded away. Symbols (S*) still count.
- `"loose"`: everything in `"default"`, and symbols are dropped as well. An opt-in level; it never applies unless asked for or reached by the retry below.

**Defaults**: the single-target phrase ops (`live.select`, `anchor.resolve`) default to `"default"`. The multi-hit ops — plain `find` (D-F5), `rename` and `history.search` — default to `"exact"` and only fold when the caller passes `match_level`; `rename` writes tracked changes, so widening its default would silently edit more occurrences.

**Retry before failing**: when a phrase is not found at the level in force, a phrase op tries the less restrictive levels itself, one at a time, before it raises `ANCHOR_NOT_FOUND`. Each level keeps the exact-first tiebreak below. `"exact"` never retries. Multi-hit ops (`find`, `rename`, `history.search`) never retry: they search at the level passed and return every hit at that level. The reply names the level that matched.

<!-- -->

> **✎ Notes** · `SPEC › Match levels (D-F1)`
> _(your notes here — replace this line)_

### The folding rule

Both the phrase and each paragraph are folded to a comparison string, and an offset map keeps the position of every folded character in the original text.

- **Accents**: NFD, then drop combining marks (category M). `Estás` becomes `estas`.
- **Case**: `casefold`.
- **Punctuation and dashes** (levels `default` and `loose`): every character in Unicode category P* becomes a word boundary: it is replaced by a space, then runs of whitespace collapse to one and the ends are trimmed, so `ahí—dijo` matches `ahí dijo` and `-¿Estás ahí?` matches `Estas ahi`. `--` is two P* characters and folds the same way. `¿`, `?`, `-`, `—`, `–`, `«` and curly quotes all fold away.
- **Symbols** (level `loose` only): every character in category S* (`$`, `*`, `+`, `=`, `<`, `^` and so on) is dropped. At `default` they stay.
- **Whitespace**: runs of whitespace (including no-break space and tabs) become one space. Leading and trailing space is trimmed, and a space left next to dropped characters is merged.
- **Offsets**: each folded character records the original start and end. A match from folded positions `i..j` maps to the original span from the start of character `i` to the end of character `j-1`. The span then widens to take punctuation that touches the match on its own side only when the phrase itself began or ended with punctuation. Combining marks stay with their base. The result is a normal `p142#…:s-e` range, as `find` returns today.
- **Empty phrase after folding** (for example a phrase of only `--`, or only symbols at `loose`): matched exactly only; folded matching is refused with `SCHEMA_INVALID`, since it would match every paragraph.
- **Position**: a match may start anywhere in a paragraph, not only at its start (D-F2). `Estas ahi` finds `-¿Estás ahí?`, and a phrase without `¿` finds a passage that has `¿` in the middle of a paragraph. `find` already searches inside paragraphs; the folded levels do the same.

<!-- -->

> **✎ Notes** · `SPEC › The folding rule`
> _(your notes here — replace this line)_

### Word find codes in agent phrases (D-F6)

Word's Find box accepts special codes, and they are useful to an agent. Before matching, agent phrases are translated, left to right, in `model/loosefold.py`:

| Code | Meaning | Becomes |
|------|---------|---------|
| `^p` | paragraph mark | `\r`, as an alias (D-F6). A phrase may cross paragraphs where the op already allows it (`live.select`'s `_found`, `find_phrase_across`); the fold keeps `\r` as a hard separator and never turns it into a space |
| `^t` | tab | `\t`, where the op supports it |
| `^l` | manual line break | the line-break character, where the op supports it |
| `^s` | no-break space | U+00A0 |
| `^-` | optional hyphen | U+00AD |
| `^~` | non-breaking hyphen | U+2011 |
| `^uNNN` | character by decimal code | that character (`^u173`, `^u8209`) |
| `^^` | a literal caret | `^` |

Any other `^x` stays literal (further codes are not promised). Because translation runs left to right, `^^p` is a caret followed by `p`, not a paragraph mark. Word's simple Find box in the Navigation pane also takes `^p` and `^t`, so a phrase an agent copies from Word keeps its meaning. Translation happens before folding, and only in agent phrases and in `find` calls that pass `match_level`; plain and regex `find` queries are not translated (D-F5); a literal `^` that the writer typed is matched with `^^` (at `loose` it is a symbol and is dropped like any other).

`locate`'s generated phrases (`find_phrase`, `find_phrase_word`) do not change (direction b). `find_phrase_word` already writes these codes (`model/wordfind.py` `_WORD_CODES`, `_HYPHEN_CODES`); the translator accepts exactly that set, so every `locate` phrase reads back to its own text.

<!-- -->

> **✎ Notes** · `SPEC › Word find codes in agent phrases (D-F6)`
> _(your notes here — replace this line)_

### Tiebreak levels

1. **exact**: the phrase equals the text verbatim.
2. **case-only**: equal after `casefold` (today's `live.select` rule).
3. **default**: equal after the accent, case and punctuation fold.
4. **loose**: equal after the symbol fold as well.

For a single-target phrase op, the search takes the first level that has any match, starting from the strictest and escalating only as far as the retry rule allows. Within that level, one match is the answer. Two or more raise `ANCHOR_AMBIGUOUS` (as now), and the error lists the level and the candidate addresses. A looser level is never consulted once a stricter one matches, so one exact paragraph beats any number of looser ones. Multi-hit ops do not select a level: every hit that matches at the level passed (or stricter) is returned or renamed, each marked with its own `match_level`, and no `ANCHOR_AMBIGUOUS` is raised.

<!-- -->

> **✎ Notes** · `SPEC › Tiebreak levels`
> _(your notes here — replace this line)_

### Reporting a loose match

Responses carry `"match_level": "exact" | "case" | "default" | "loose"`, the level that matched. It is a new key: `anchor.resolve`'s existing `match` field (`exact|normalized|fuzzy`, `model/anchor.py`) keeps its meaning and values, so no existing contract changes. Whenever the level is not `exact` the response also carries `matched_text`, the original text of the span, so the agent can compare it with what it asked for. `live.select` already returns the paragraph text. `find` marks each hit.

<!-- -->

> **✎ Notes** · `SPEC › Reporting a loose match`
> _(your notes here — replace this line)_

### Opt-out

`match_level: "exact"` on each phrase-taking op restores today's behaviour. For `find`, the existing `ignore_case`, `ignore_accents` and `whole_word` flags stay as they are; `match_level: "default"` implies the first two plus the punctuation fold, and `match_level: "loose"` adds the symbol fold. Omitting `match_level` leaves `find` exactly as it is today (D-F5).

<!-- -->

> **✎ Notes** · `SPEC › Opt-out`
> _(your notes here — replace this line)_

### Performance

- Folded text and offset maps are built once per paragraph and cached with the `Document` index; a search over N paragraphs is O(total characters).
- The levels share one pass: the exact and case levels run first on the raw and casefolded text. The folded levels are built only when both are empty, and the symbol-folded text only when the punctuation-folded search is empty too.
- `locate` indexes are unchanged.
- The repo's `tests/bench` documents are the base. A new benchmark runs the four levels on the 100k-word novel.

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
| D-F1 | Option B. Symbols (S*) are not folded by default. `match_level: "exact" \| "default" \| "loose"` (key renamed in D-F7); `"loose"` also drops symbols. A phrase not found at the default level is retried at looser levels before failing, exact-first tiebreak kept, and the reply says which level matched | agreed | 2026-10-14 |
| D-F2 | Matching works in the middle of a paragraph too, not only at its start; a phrase without `¿` must still match | agreed | 2026-10-14 |
| D-F3 | The fuzzy tier of `anchor.resolve` stays as is, after the loose level (Q5, as recommended) | agreed | 2026-10-14 |
| D-F4 | No refusal of loose matches that look doubtful; the level report is enough (Q4, as recommended) | agreed | 2026-10-14 |
| D-F5 | Plain `find` stays exact by default; folding only when `match` is passed | agreed | 2026-10-14 |
| D-F6 | Agent phrases accept Word's find codes: `^p` (alias for `\r`), `^t`, `^l` where the op supports them, `^^` for a literal caret. `locate`'s generated phrases do not change | agreed | 2026-10-14 |
| D-F7 | Review clarifications (r3): request/response key `match_level` (new; `anchor.resolve`'s `match` keeps `exact\|normalized\|fuzzy`); auto-retry and `"default"` default only for single-target ops (`live.select`, `anchor.resolve`); `find`, `rename`, `history.search` default exact, never retry, return all hits; Word codes = the `model/wordfind.py` set (`^p ^l ^t ^s ^- ^~ ^uNNN ^^`); punctuation folds to a word boundary, not to nothing; Word codes not translated in plain/regex `find` | agreed | 2026-10-14 |

<!-- -->

> **✎ Notes** · `SPEC §4`
> _(your notes here — replace this line)_

## §5 Contracts

All changes are additive registry operations, so CLI, MCP and Python get them the same way. No request field changes its meaning and no response field changes its values.

<!-- -->

> **✎ Notes** · `SPEC §5`
> _(your notes here — replace this line)_

### Two meanings of "exact"

The word appears on both sides of the contract and means different things:

1. **Requested** `match_level: "exact"` means *the op's rule of today*, not "verbatim". For `live.select` that is today's casefold substring rule; for `term.rename` it is today's case-insensitive whole-word rule; for `anchor.resolve` it is today's exact → normalised → fuzzy chain. It never folds accents, punctuation or symbols and never retries.
2. **Reported** `match_level: "exact"` means the phrase (after Word-code translation) equals the matched text character for character. So `live.select` called with `match_level: "exact"` can report `"case"`, because today's rule already ignores case.

<!-- -->

> **✎ Notes** · `SPEC › Two meanings of "exact"`
> _(your notes here — replace this line)_

### Fold levels (model/loosefold.py)

Each level folds everything the one before it folds. The reported level is the strictest one at which the phrase and the text fold to the same string.

| Level | Fold | Notes |
|-------|------|-------|
| `exact` | none | verbatim |
| `case` | Cf characters dropped (soft hyphen U+00AD, U+200B–U+200D, U+2060, U+FEFF, bidi marks; a superset of `model/normalized.py`'s `IGNORABLE`), NFC, `casefold`, whitespace runs to one space | the same characters `anchor.resolve`'s normalised tier ignores, so no phrase that tier finds is lost at a folded level |
| `default` | `case`, plus NFD and combining marks (M*) dropped, every P* character turned into a word boundary (a space) | symbols (S*) are kept |
| `loose` | `default`, plus every S* character dropped | opt-in, or reached by the retry |

Whitespace rules, for every folded level:

1. `\r` (a paragraph mark, or `^p` in the phrase) is a hard separator. It is never turned into a space, never collapsed with one, and never trimmed. A space next to it is dropped.
2. Every other whitespace (space, tab, no-break space, `\n`) becomes one space, and runs collapse.
3. Only spaces are trimmed from the ends of the folded phrase. `^pEstas` keeps its leading `\r`, so `live.select` still names the paragraph after the mark.

The fold of a phrase that leaves no letter, digit or symbol (only `--`, or only symbols at `loose`) is refused with `SCHEMA_INVALID` at the folded levels; such a phrase is matched at `exact` only.

```python
# model/loosefold.py
Level = Literal["exact", "case", "default", "loose"]
LEVELS: tuple[Level, ...] = ("exact", "case", "default", "loose")

@dataclass(frozen=True)
class Folded:
    text: str                      # the comparison string
    starts: tuple[int, ...]        # original start of each folded character
    ends: tuple[int, ...]          # original end (exclusive) of each folded character

def fold(text: str, level: Level) -> Folded: ...           # cached per (text, level)
def to_original(folded: Folded, i: int, j: int, *, widen_left: bool, widen_right: bool) -> tuple[int, int]: ...
def translate_codes(phrase: str) -> str: ...                # Word find codes, left to right
def ladder(requested: Level | Literal["default"], *, retry: bool) -> tuple[Level, ...]: ...
def hits(texts: Sequence[str], phrase: str, level: Level) -> list[Hit]: ...   # Hit = (index, start, end, level)
```

`translate_codes` accepts exactly `model/wordfind.py`'s set (`^^`, `^p`, `^l`, `^t`, `^s`, `^-`, `^~`, `^uNNN`); any other `^x` stays literal. For every text `t` without two-form hyphens, `translate_codes(word_find(t)) == t`.

<!-- -->

> **✎ Notes** · `SPEC › Fold levels (model/loosefold.py)`
> _(your notes here — replace this line)_

### Request parameter match_level

| Op | Values | Default | Retries | `"exact"` reproduces |
|----|--------|---------|---------|----------------------|
| `live.select` (`find_phrase`) | `exact`, `default`, `loose` | `default` | yes | casefold substring, unique (`_found` today) |
| `anchor.resolve` (`anchor.exact`) | `exact`, `default`, `loose` | `default` | yes | exact → normalised → fuzzy (`model/anchor.py` `resolve` today) |
| `find` (`query`) | `exact`, `default`, `loose` | absent = `exact` | never | today's `find`, honouring `ignore_case`, `ignore_accents`, `whole_word` |
| `term.rename` (`from`) | `exact`, `default`, `loose` | absent = `exact` | never | today's case-insensitive whole-word match |
| `history.search` (`query`) | `exact`, `default`, `loose` | absent = `exact` | never | today's `ignore_case` / `ignore_accents` search |

Ladders for the single-target ops (strictest first; a level runs only when every level before it found nothing):

- requested `default`: `exact` → `case` → `default` → `loose`.
- requested `loose`: the same ladder; asking for `loose` only says the caller expects it may be needed.
- requested `exact`: today's rule only.

The multi-hit ops search once, at the level passed, and return every hit that folds equal at that level, each tagged with the strictest level it meets. With `regex: true`, a `match_level` other than `exact` is `SCHEMA_INVALID` (`path: "$.match_level"`). With `match_level` passed, `find`'s `ignore_case` and `ignore_accents` are implied by the level and may not contradict it (`ignore_case: false` with `default` is `SCHEMA_INVALID`).

<!-- -->

> **✎ Notes** · `SPEC › Request parameter match_level`
> _(your notes here — replace this line)_

### live.select

Request: today's fields plus `match_level`. The existing `mode` (`select` | `cursor`) is unrelated and unchanged.

```json
{"doc": "Novel.docx", "find_phrase": "Estas ahi", "match_level": "default"}
```

Reply (new keys `match_level`, `matched_text`; `matched_text` only when the level is not `exact`):

```json
{"text": "-¿Estás ahí? -dijo.", "at": "p142#a7b2a7b2", "front": true,
 "match_level": "default", "matched_text": "¿Estás ahí"}
```

- Two or more hits at the first level that has any: `ANCHOR_AMBIGUOUS` with `details: {"match_level": "default", "candidates": [{"at": "p142#…", "matched_text": "…"}, …]}` (at most 5).
- No hit at any level of the ladder: `ANCHOR_NOT_FOUND` with `details: {"tried": ["exact", "case", "default", "loose"]}`.
- A phrase holding `^` is first tried raw at the `exact` level, then translated, so a `locate` `find_phrase` that contains a literal caret still selects its paragraph.
- `find_phrase_across` and the "phrase opens with `\r`" rule work on the folded joined text as they do now.

<!-- -->

> **✎ Notes** · `SPEC › live.select`
> _(your notes here — replace this line)_

### anchor.resolve

The verbatim stage and its context-score choice are unchanged: several verbatim hits are still told apart by `(0.5·quote + 0.2·prefix + 0.2·suffix)/0.9`, the tie margin and the fingerprint rule, and only that rule raises `ANCHOR_AMBIGUOUS`. A hit count never does. The folded stages slot in after the existing normalised one and before fuzzy (D-F3):

`exact` (verbatim) → `normalized` (today) → `case` → `default` → `loose` → fuzzy (only when `mode: "best"`).

Each later stage runs only when every earlier stage found no span, exactly as `normalized` runs today only when no verbatim span exists. Its spans are scored like any candidate (`quote_score = 1.0`) and pass through the same acceptance and tie rules.

`mode` × `match_level`:

| | `match_level: "exact"` | `match_level: "default"` / `"loose"` |
|---|---|---|
| `mode: "best"` (default) | today: exact → normalised → fuzzy | exact → normalised → case → default → loose → fuzzy |
| `mode: "exact"` | today: exact → normalised, every span accepted whatever its context score, no fuzzy | exact → normalised → case → default → loose, every span accepted whatever its context score, no fuzzy |

`mode` decides whether fuzzy runs and whether the context score gates acceptance; `match_level` decides how far the fold goes. Neither changes the other's meaning.

Reply (new key `match_level`; `match` keeps its values `exact | normalized | fuzzy`):

```json
{"at": "p142#a7b2a7b2:2-12", "score": 0.97, "match": "normalized",
 "match_level": "default", "matched_text": "¿Estás ahí"}
```

- `match: "exact"` ↔ `match_level: "exact"`.
- `match: "normalized"` covers today's normalised tier (reported `match_level: "case"`, the strictest level whose fold equals it) and every folded stage (`case`, `default`, `loose`).
- `match: "fuzzy"` carries no `match_level`.
- Ambiguity candidates gain `match_level` beside their existing `match`.

<!-- -->

> **✎ Notes** · `SPEC › anchor.resolve`
> _(your notes here — replace this line)_

### find

Request: today's fields plus `match_level`. Without it, the reply, the cursor token and `query_word` are byte-identical to today (the token gains `match_level` only when it is passed, as it does for `stories`).

Each hit gains `match_level` when `match_level` was passed:

```json
{"matches": [{"at": "p142#a7b2a7b2:2-12", "text": "¿Estás ahí", "context": "…-¿Estás ahí? -dijo.…",
  "match_level": "default"}], "total": 1, "query_word": null}
```

- `query_word` is `null` at `default` and `loose` (Word's Find cannot fold punctuation).
- `whole_word` checks the original characters on either side of the mapped span.
- Word codes are translated only when `match_level` is passed and `regex` is false (D-F5).

<!-- -->

> **✎ Notes** · `SPEC › find`
> _(your notes here — replace this line)_

### term.rename and history.search

- `term.rename`: `match_level` widens what `from` matches; `whole_word`, `preserve_case` and `inflections` apply to the matched original text as now. Each `preview` change gains `match_level`; the result gains `matches_by_level: {"exact": n, "case": n, …}` when `match_level` was passed (`counts` keeps its integer fields). `dry_run` is the advised first call at a folded level.
- `history.search`: each hit gains `match_level` when it was passed, and the offset of a folded hit is now exact (the fold keeps an offset map), so `text` shows the hit's own context instead of the start of the source.

<!-- -->

> **✎ Notes** · `SPEC › term.rename and history.search`
> _(your notes here — replace this line)_

### Errors (existing codes)

- `SCHEMA_INVALID`: a phrase empty after folding; `match_level` with `regex`; `match_level` contradicting `ignore_case` / `ignore_accents`.
- `ANCHOR_NOT_FOUND`: no hit on the whole ladder (`details.tried`).
- `ANCHOR_AMBIGUOUS`: `live.select` with several hits at the first level that has any; `anchor.resolve` by its score rule only.

<!-- -->

> **✎ Notes** · `SPEC › Errors (existing codes)`
> _(your notes here — replace this line)_

### Inter-component contracts

| Caller | Callee | Protocol | Contract | Failure handling |
|--------|--------|----------|----------|-----------------|
| literary-critic | `live.select`, `anchor.resolve`, `find` | JSON | Sends phrases as typed; reads `match_level` and `matched_text` and tells the user when it is not `exact` | A wrong pick is re-asked with a longer phrase or `match_level: "exact"` (D-F4) |
| `ops/live.py`, `model/anchor.py`, `model/search.py`, `safety/history.py` | `model/loosefold.py` | Python | `fold`, `translate_codes`, `hits`, `ladder` as above | Errors raised as `DocControlError` |
| `ops/locate.py`, `model/wordfind.py` | — | — | Not changed; `locate` output pinned by a golden file | Regression test fails the build |

<!-- -->

> **✎ Notes** · `SPEC › Inter-component contracts`
> _(your notes here — replace this line)_

### Database migrations

| Service | Database | Change | Reversible | Forward-compatible |
|---------|----------|--------|------------|-------------------|
| — | none | — | yes | yes |

<!-- -->

> **✎ Notes** · `SPEC › Database migrations`
> _(your notes here — replace this line)_

## §6 Risks & Validation

| Risk | Likelihood | Impact | Mitigation | Validation |
|------|-----------|--------|------------|------------|
| A loose level picks the wrong paragraph silently | Med | Med | Exact-first ladder; `match_level` and `matched_text` in every reply; ambiguity inside a level is an error, not a guess (D-F4) | unit (variant fixture table) |
| `anchor.resolve` results change for anchors that resolve today | Low | High | Folded stages run only when no verbatim or normalised span exists; their candidates go through the same score and tie rules; `match_level: "exact"` reproduces today | unit (`tests/model/test_anchor*.py` unchanged, plus a parity test over the corpus) |
| The fold trims or collapses `\r`, so a `^p` phrase names the wrong paragraph | Med | High | `\r` is a hard separator in the fold; only spaces are trimmed; fixture rows with `^p` at the start, middle and end | unit |
| Cf characters (soft hyphen, zero-width) block a folded match that the normalised tier would find | Med | Med | The `case` level drops all Cf, a superset of `IGNORABLE`; a test asserts every normalised-tier fixture also matches at `case` | unit |
| A `locate` `find_phrase` holding a literal `^` is mistranslated as a Word code | Low | Med | `live.select` tries the raw phrase at `exact` before translating | unit |
| Widening `term.rename` edits more occurrences than meant | Med | High | Default stays `exact`; no retry for multi-hit ops; `matches_by_level` and `dry_run` show the reach | unit |
| Folded search is too slow on long documents | Med | Med | Fold cached per paragraph text; folded levels built only when the stricter ones are empty; benchmark on the 100k-word novel (`generate("novel", 1)`) | perf (`find_loose_overhead_ratio <= 1.5`, `live_select_loose_extra_ms <= 50`) |
| Offsets drift for combining marks, expansions (`ß` → `ss`) or dropped Cf characters | Med | High | Per-character offset map; spans never cut a base from its marks; property test on random accented text | property test (`span_mismatches == 0`) |
| `locate` output changes by accident (direction b) | Low | High | Golden file of `locate` over the corpus taken before any change; round-trip of `find_phrase_word` through `translate_codes` | unit |

<!-- -->

> **✎ Notes** · `SPEC §6`
> _(your notes here — replace this line)_

