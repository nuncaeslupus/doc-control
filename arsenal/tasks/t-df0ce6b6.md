---
id: t-df0ce6b6
title: "FIND T4: match levels and retry ladder"
label: "match ladder"
priority: 5
deps: [t-d1246598, t-998f7bcc, t-966f93b3]
workspace: FIND
---

## Acceptance gate
loose_match_failures == 0 — from the FIND plan (`arsenal/project/FIND/plan.md`), spec `arsenal/project/FIND/spec.md` §1 and §5.

```bash
uv run pytest -q tests/model/test_loosefold_levels.py -k test_variant_table_matches
```

## Tests
`test_variant_table_matches` in `tests/model/test_loosefold_levels.py` — every row (`Estas ahi` against `¿Estás ahí?`, `-¿Estás ahí?`, `-Estas ahí?`, `--Estas ahi`, mid-paragraph `¿`, dashes, quotes, whitespace) is found at its expected level; `test_symbols_kept_at_default` — `5$` does not match `5` at `default` and does at `loose`

## Location
model

## Description
`hits` and `ladder`: hits of a phrase at one level over a list of texts, each tagged with its strictest level; the retry ladder; the variant fixture table of the spec (surface: cloud)
