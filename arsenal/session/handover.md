# Session Handover

## Last task

- **ID**: Word COM live adapter (t-9e24ff41, #145), LIVE T8, merged as #164; this handover follows it.
- **Status at handover**: the LIVE plan is done (T1-T8 merged). The only open task is `list the MCP server (t-954cacd8, #89)`, which waits on the owner.

## What was done this session

The live engine on LibreOffice, on the owner's Ubuntu PC: protocol, fake adapter and dispatch (#152), lock awareness (#157), LibreOffice session open/save/close (#158), select by address and find phrase (#159), latency bench (#160), comments and tracked edits through `batch` with `engine: "live"` plus the `live-libreoffice` CI job (#161), CLI/MCP parity test and README (T7). Reviews were done by a second desktop session ("distinct-session"), since the headless `claude -p` login had expired and CodeRabbit skips this repository.

On the owner's Windows 11 machine (Word 16.0.17932, Spanish), a later session: no Word Find string for text with a soft or no-break hyphen (#153), then Find strings that carry each hyphen's code (#162), and the Word live adapter (#164): `src/doc_control/live/word_com.py`, the extra `doc-control[live]` (pywin32), `tests/live/word_suite.py` + `tests/live/test_word_com.py`, `scripts/live_word_check.py`. Gate measured there: `live_word_failures == 0`; `live.select` by find phrase on the 2 033-paragraph novel p95 369 ms (not gated). #164 was reviewed by one cold subagent (the owner's standing rule for that machine: merge on green after such a review); its findings were fixed before the merge.

## What remains

- **Word, not measured**: Microsoft 365 (D-L4 asks for it; none on the owner's machine). CI has no Word: the scenario test is skipped there, so a change to `word_com.py` must be run on Windows with `uv run --no-sync python scripts/live_word_check.py`.
- **Word, known limits**: whether tracked deletions are hidden is decided from the first one (a reviewer filter hiding only some is not handled); a Selection ending inside a hyperlink grows to the whole field (Word's rule; edits use ranges and are exact); `tracked_replace` on a span ending inside a hyperlink field is untested; each op of a live batch reads the model again (slow on a heavily revised manuscript); date fields are recomputed by Word.
- Issue #148 (Word's shortcuts are localised): the adapter drives Word by automation calls only; nothing in the engine or docs names a key.
- **Owner's call, not decided**: LibreOffice is started listening on a localhost TCP port any local process can reach, and is left running after `live.close` (the snap build cannot use a pipe). Say yes to this, or ask for a token/pipe where the build allows.
- Open review notes on the live engine: `loadComponentFromURL` has no timeout (a dialog in a visible LibreOffice could hang `live.open`); any service on the port counts as `listening`; a paragraph with a date field reads stale; `live.selection` refuses a selection spanning a table as `ENGINE_UNAVAILABLE`; history keeps `engine: live` entries of edits later discarded; live `batch` is not all-or-nothing if the app dies mid-batch; a span over two paragraphs inside one table cell is untested; select by address reads the whole text three times (the raw p95 on the novel is 490-570 ms on the owner's PC, 99 ms in CI).
- `tests/ops/test_odt_parity.py::test_odt_parity_gate` fails on the owner's PC (snap LibreOffice cannot read `/tmp`); it passes in CI.
- `arsenal/config.toml`: new settings `bot-unavailable-notices` and `review.reviewer-identity` are not acknowledged.
- literary-critic asks still open: find phrase for a range inside a paragraph, in-memory open of a draft.
- Older notes: see git history of this file.

## How to continue

1. Gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` (over 10 minutes here; run the tests of the area plus `tests/test_registry.py tests/test_docs.py tests/ci`, and let CI run the rest). A new operation goes in `OPERATION_MODULES` (`src/doc_control/registry.py`) and the docs are regenerated with `uv run python -m doc_control.reference`.
2. Live tests on this PC: `uv run pytest -q tests/live` (about 6 minutes; starts its own headless LibreOffice). **Never run live code or `import uno` under the system Pythons here**: the snap's bridge is built for Python 3.12 and aborts the process in any other, which raises Ubuntu crash dialogs. `uv run` only.
3. CI's `live-libreoffice` job needs the full `libreoffice-writer` package: the `-nogui` build crashes on a document with comments opened in a view.
4. Merge policy: CI green + adversarial review CLEAR from another session (review-max-rounds = 2); `bash claude-arsenal/bin/merge_ready.sh <pr>` decides. Archive the task file into `arsenal/tasks/_history/` by hand in the task's PR.
5. On the owner's Windows machine: there is no `make`, and `open_task_pr.sh` cannot finish (archive the task file by hand, `bash scripts/hygiene.sh`, `gh pr create`). Use `uv run --no-sync` (a plain `uv run` syncs and removes pywin32 from `.venv`; put it back with `uv sync --extra live`). The owner's manuscript is usually open in Word: tests and scripts drive a hidden Word of their own (`word_suite.hidden_word`), never the running one. Commit with `git -c core.safecrlf=false`.
6. Private corpus only via `DOC_CONTROL_PRIVATE_CORPUS` or a gitignored `private/`; never commit or print names, paths or text from it.
