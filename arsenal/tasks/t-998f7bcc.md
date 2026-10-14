---
id: t-998f7bcc
title: "FIND T2: fold with an offset map"
label: "loose fold module"
priority: 5
workspace: FIND
---

## Acceptance gate
span_mismatches == 0 — from the FIND plan (`arsenal/project/FIND/plan.md`), spec `arsenal/project/FIND/spec.md` §1 and §5.

```bash
uv run pytest -q tests/model/test_loosefold.py -k test_fold_spans_cut_the_matched_passage
```

## Tests
`test_fold_spans_cut_the_matched_passage` in `tests/model/test_loosefold.py` — for random accented, punctuated text, every folded hit mapped back is a whole-character span that folds to the phrase; `test_fold_keeps_paragraph_mark_untrimmed` — `^pEstas` and `ahí^p` keep their `\r` at every level; `test_fold_drops_cf_like_normalised_tier` — every `tests/model/test_anchor_normalised.py` quote matches at `case`

## Location
model

## Description
`model/loosefold.py`: `fold` at `case`, `default`, `loose` with the offset map; Cf dropped, NFD marks dropped, `casefold`, P* to a boundary, S* dropped at `loose`; whitespace collapse that keeps `\r` and trims spaces only; `to_original` with the punctuation widening rule; empty fold refused (surface: cloud)
