# Session Handover

## Last task

- **ID**: T51 hard-wrapped plain-text headings (t-ba441550), PR #114 (with this handover).
- **Status at handover**: all of this session's work is merged; 0.2.0 (and 0.1.0) PyPI releases wait on the owner's approval of the `pypi` environment.

## What was done this session

Merged: T38 context-aware recipes (#90), T39 in-process API (#93), T40 parts and style-marked chapters (#100), T41 normalised exact anchors + 0.1.0 (#101), T42 rendered page marks win (#102), T43 segment ranges (#103), T44 writer safety + T45 API polish (#105), T46 performance gate + T47 cold bulk read (#107), T48 page facts + 0.2.0 (#108), T29 references + coverage (#109), T49 scene styles + heading subtitles (#111), T50 plain-text headings + `chapter_level: auto` + uncapped in-process suppressed list (#113), T51 hard-wrapped headings (#114). T49-T50 were literary-critic's T6 asks; on a real manuscript `segment` now gives 156 scenes (literary-critic's own count), 54 chapters, 3 parts. literary-critic now runs all .docx handling on doc-control through `api.open`; it talks to this repo's sessions by send_message and files its needs as gated tasks.

## What remains

- literary-critic's open ask: read .txt/.md directly (spec open question 1) instead of re-converting to .docx.
- Review notes declared on T49/T50: a subtitle under a chapter later suppressed by `min_chapter_words` stays out of word counts; decorated `(3)`/`[2]` laid out as headings parse as numerals; multi-word `scene_style_words` entries never match; a lone H1 is skipped under `chapter_level: auto`.
- Rename `boundary`/`unsupported_structure` skip reasons untested.
- T30 manual real-Word check (needs Windows + Word 2016+/M365): the only task left in the queue.
- Performance still open against literary-critic's asks: cold `locate` ~1.0–1.2 s on 100k words (accepted; warm per-paragraph ≤ 50 ms is gated); first bulk read on a real 74k-word manuscript ~1.05 s (target 0.3–0.5 s): the next win is `splice.scan` (pure-Python byte scanner, ~0.4 s) and op-module import (~0.2 s).
- Declared risks from reviews: per-paragraph parsed-element cache holds memory for an open document; process-wide caches (anchor normalised text LRU, find fold cache) are bounded by count, not bytes; `MARKS_TRUST_RATIO = 2.0` is uncalibrated; switching a document to a custom store hides its earlier history.
- Earlier follow-ups (unchanged): zipio `_umask` thread safety and size cap; style batches not logged in history; T14 `w:bCs`; T15 reply order in one batch; T13 log-append atomicity; T21 stylesWithEffects retarget; T17 rename as batch kind; recipe engine edge cases (overlapping `$para`, deadline, OPS_CAP counting); GitHub Release for v0.0.1 not created.

## How to continue

1. Gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`; benchmarks with `make bench` (a bench test named by node id also runs, see `tests/conftest.py`).
2. Merge policy: CI green + adversarial review CLEAR (review-max-rounds = 2). `open_task_pr.sh` cuts its own branch from main when the work is already committed: instead archive the task file into `arsenal/tasks/_history/` by hand, push, open the PR, then post the marker from `adversarial_review.sh marker` (CI runs mypy over tests too: run plain `uv run mypy`).
3. One designated branch per session: ship PRs in sequence; restart the branch from main after each merge (force-with-lease over merged history only).
4. Private corpus only via `DOC_CONTROL_PRIVATE_CORPUS` or a gitignored `private/`; never commit or print names, paths or text from it.
