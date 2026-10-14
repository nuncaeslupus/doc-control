---
id: t-d13faf21
title: "loosefold follow-ups: cold-fold cost, widening over combining marks"
priority: 5
requires: [human:gate]
---

Imported from issue #258

Left open by the review of #256 (FIND T2, `model/loosefold.py`).

- **RISK, performance:** a cold `fold` of 600k characters takes about 1.3 s. The process-wide `lru_cache`, keyed on paragraph text, only helps repeat calls. This puts FIND T7's `find_loose_overhead_ratio <= 1.5` on the 100k-word bench at risk (#238), so check it there first.
- **NOTE:** `to_original(widen_right=True)` cuts a cluster when the trailing punctuation carries a combining mark: `"ahí?́"` gives `(5, 9)`. The property gate never runs with widening on.
- **NOTE, minor:**
  - clusters longer than 32 code points are split;
  - Indic vowel signs (Mc) are dropped;
  - `fold_phrase("", "exact")` gives contradictory advice.

Size: S.

## Acceptance gate

<!-- This task came from an issue, so its "gate" is prose. Write a real check
     below, then DELETE the `requires: [human:gate]` line in the front matter.
     Until that line is gone the selector will not offer this task, which is
     deliberate: a prose gate runs nothing, and a gate that runs nothing passes
     everything. -->

```bash
# arsenal:gate-placeholder — replace with the real check; it may land in this task's own PR
false
```
