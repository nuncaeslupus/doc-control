# Session Handover

## Last task

- **IDs**: T46 performance gate (t-c60111a5) and T47 cold bulk read (t-1c0bdf9c), shipped together in one PR after T44/T45 (#105).
- **Status at handover**: T38–T43 merged; T44+T45 in #105; T46+T47 PR follows #105. Version 0.1.0 is on main; its PyPI release waits on the owner's approval of the `pypi` environment.

## What was done this session

Merged: T38 context-aware recipes (#90), T39 in-process API `api.open` / `paragraphs()` (#93), T40 parts, style-marked chapters, front matter (#100), T41 exact anchors on normalised text + release 0.1.0 (#101), T42 rendered page marks win (#102), T43 segment ranges by span or unit id (#103). Queued from literary-critic's requests: T41–T48. literary-critic now runs all .docx handling on doc-control (its T2/T5 build on `api.open`); it is the main consumer and talks to this repo's sessions by send_message.

## What remains

- Merge #105 (T44+T45), then the T46+T47 PR; message literary-critic per landing.
- T48 page basis in the bulk read + section breaks as page_break_before (t-a9ee5410).
- T29 coverage and docs (t-00827bbf); T30 manual real-Word check (needs Windows + Word).
- Performance still open against literary-critic's asks: cold `locate` ~1.0–1.2 s on 100k words (accepted; warm per-paragraph ≤ 50 ms is gated); first bulk read on a real 74k-word manuscript ~1.05 s (target 0.3–0.5 s): the next win is `splice.scan` (pure-Python byte scanner, ~0.4 s) and op-module import (~0.2 s).
- Declared risks from reviews: per-paragraph parsed-element cache holds memory for an open document; process-wide caches (anchor normalised text LRU, find fold cache) are bounded by count, not bytes; `MARKS_TRUST_RATIO = 2.0` is uncalibrated; switching a document to a custom store hides its earlier history; `DOC_LOCKED` hint still says Word; rename `boundary`/`unsupported_structure` skip reasons untested.
- Earlier follow-ups (unchanged): zipio `_umask` thread safety and size cap; style batches not logged in history; T14 `w:bCs`; T15 reply order in one batch; T13 log-append atomicity; T21 stylesWithEffects retarget; T17 rename as batch kind; recipe engine edge cases (overlapping `$para`, deadline, OPS_CAP counting); GitHub Release for v0.0.1 not created.

## How to continue

1. Gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`; benchmarks with `make bench` (a bench test named by node id also runs, see `tests/conftest.py`).
2. Merge policy: CI green + adversarial review CLEAR (review-max-rounds = 2; `open_task_pr.sh` archives the task after the review, so post the marker by hand naming that commit).
3. One designated branch per session: ship PRs in sequence; restart the branch from main after each merge (force-with-lease over merged history only).
4. Private corpus only via `DOC_CONTROL_PRIVATE_CORPUS` or a gitignored `private/`; never commit or print names, paths or text from it.
