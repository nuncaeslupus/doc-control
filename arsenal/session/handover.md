# Session Handover

## Last task

- **ID**: STORIES T3, story index (t-0a709c5e, #173), in PR #194, which also records the LAYOUT spec r2 approval.
- **Status at handover**: the STORIES plan is approved (`arsenal/project/STORIES/plan.md`, T1–T11, issues #173–#183). T1 and T2 were merged in #192. T3 is in #194. T4–T11 are open.

## What was done this session (cloud, 2026-10-13/14)

- #169: a tracked deletion over a whole table read wrong live (LibreOffice gave its ends swapped); fixed. Also a symlink `_find` test and `tests.helpers.soffice_dir` for snap LibreOffice. Plus `status/ideas.md` (42 ideas, S-1..S-9).
- STORIES (S-1): spec r2 approved (#171); contracts, risks and plan (#172); T1 and T2 (#192); T3 (#194).
- LAYOUT (S-9): spec r2 approved (in #194). The next step is `design` for its plan, after STORIES T8.
- Live tests run in a cloud container with `UV_PROJECT_ENVIRONMENT=<dir> uv venv --python /usr/bin/python3 --system-site-packages`, then `uv run --python /usr/bin/python3 pytest tests/live`.
- This cloud session pushes to one designated branch only, so PRs went out one at a time. A session allowed per-task branches can parallelise T4/T5/T8/T9.
- Duplicate issues #184–#190 were created by mistake (LIVE tasks already archived) and closed with their labels removed.
- Reviews were cold subagents of the implementing session, and their CLEAR markers carry a `cse_` session id that `merge_ready.sh` counts as distinct from the implementer's `session_` id. That is a claude-arsenal quirk to fix upstream.

## What remains

- Cloud session after 0.6.0: a deletion over a whole table read wrong (LibreOffice gave its ends swapped: cells kept their text, the paragraph after the deletion read empty); fixed and tested. `_find` through a symlink tested. The `.odt` parity gate and the round-trip validator use a home-directory work dir under a snap LibreOffice (`tests.helpers.soffice_dir`). Live tests run in a cloud container with `uv venv --python /usr/bin/python3 --system-site-packages` (as CI).

- Windows session after #169/#170: the Word suite mirrors the LibreOffice fixes (paragraphs inside a tracked deletion, a deletion over a whole table — made in Word, read under All and Simple Markup — and open through a symlink) and adds a span over two paragraphs of one cell and edits around a hyperlink, each checked against the file engine on a copy (`_same_edits`). Fixed in Word: a whole-table deletion under Simple Markup (cell ends show in every view, and merged the paragraph before the table with its first cell); edits that touch a field or link are now refused before any write as the file engine refuses them (they failed mid-batch or went through). New protocol method `editable(path, span, replace)`, checked for every span of a live batch before the first write. `live_word_failures == 0`, select p95 449 ms.
- Windows session, 2026-10-14: open documents by name (#193, PR #195). `live.status` without `doc` lists `documents: [{app, path, name}]`. A bare file name as `doc` resolves to the one open document of that name; a same-named file in the working directory also counts as a candidate. Several candidates give `DOC_AMBIGUOUS`; an app that cannot list its documents gives `ENGINE_UNAVAILABLE`. New protocol method `documents()`. Released as **0.6.1** (#198).
- **Queued, not started**: #196 (literary-critic, relaying the owner): centre the selected passage, a safe select mode (cursor, not selection), bring Word to the front on select/open, and an exploratory `doc-control://` link handler. The link handler needs the owner's decision first (security, and whether the desktop app opens such links). Peer to message when any of it ships: the literary-critic session.
- **LibreOffice, divergence**: `editable` there only refuses a range across containers; a replace inside a hyperlink still goes through live where the file engine refuses it.
- **Word, not measured**: Microsoft 365 (D-L4 asks for it; none on the owner's machine). CI has no Word: the scenario test is skipped there, so a change to `word_com.py` must be run on Windows with `uv run --no-sync python scripts/live_word_check.py`.
- **Word, known limits**: whether tracked deletions are hidden is decided from the first one (a reviewer filter hiding only some is not handled); a Selection ending inside a hyperlink grows to the whole field (Word's rule; edits use ranges and are exact); each op of a live batch reads the model again (slow on a heavily revised manuscript); date fields are recomputed by Word.
- Issue #148 (Word's shortcuts are localised): the adapter drives Word by automation calls only; nothing in the engine or docs names a key.
- **Owner's call, not decided**: LibreOffice is started listening on a localhost TCP port any local process can reach, and is left running after `live.close` (the snap build cannot use a pipe). Say yes to this, or ask for a token/pipe where the build allows.
- Open review notes on the live engine: `loadComponentFromURL` has no timeout (a dialog in a visible LibreOffice could hang `live.open`); any service on the port counts as `listening`; a paragraph with a date field reads stale; `live.selection` refuses a selection spanning a table as `ENGINE_UNAVAILABLE`; history keeps `engine: live` entries of edits later discarded; live `batch` is not all-or-nothing if the app dies mid-batch; select by address reads the whole text three times (the raw p95 on the novel is 490-570 ms on the owner's PC, 99 ms in CI).
- literary-critic asks still open: find phrase for a range inside a paragraph, in-memory open of a draft.
- Older notes: see git history of this file.

## How to continue

1. Gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` (over 10 minutes here; run the tests of the area plus `tests/test_registry.py tests/test_docs.py tests/ci`, and let CI run the rest). A new operation goes in `OPERATION_MODULES` (`src/doc_control/registry.py`) and the docs are regenerated with `uv run python -m doc_control.reference`.
2. Live tests on this PC: `uv run pytest -q tests/live` (about 6 minutes; starts its own headless LibreOffice). **Never run live code or `import uno` under the system Pythons here**: the snap's bridge is built for Python 3.12 and aborts the process in any other, which raises Ubuntu crash dialogs. `uv run` only.
3. CI's `live-libreoffice` job needs the full `libreoffice-writer` package: the `-nogui` build crashes on a document with comments opened in a view.
4. Merge policy: CI green + adversarial review CLEAR from another session (review-max-rounds = 2); `bash claude-arsenal/bin/merge_ready.sh <pr>` decides. Archive the task file into `arsenal/tasks/_history/` by hand in the task's PR.
5. On the owner's Windows machine: there is no `make`, and `open_task_pr.sh` cannot finish (archive the task file by hand, `bash scripts/hygiene.sh`, `gh pr create`). Use `uv run --no-sync` (a plain `uv run` syncs and removes pywin32 from `.venv`; put it back with `uv sync --extra live`). The owner's manuscript is usually open in Word: tests and scripts drive a hidden Word of their own (`word_suite.hidden_word`), never the running one. Commit with `git -c core.safecrlf=false`.
6. Private corpus only via `DOC_CONTROL_PRIVATE_CORPUS` or a gitignored `private/`; never commit or print names, paths or text from it.
