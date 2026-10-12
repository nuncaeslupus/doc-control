---
name: init
description: Sets up claude-arsenal/ in a host repo or registers a workspace (--workspace); safe to re-run. Use when the user wants the arsenal installed, refreshed or a workspace added. Not for adding tasks (queue-add) or resuming work (queue-next).
user-invocable: true
argument-hint: "[--repo-path PATH] [--profile NAME] [--sections A,B] [--no-branch-protection] [--interview] [--workspace NAME] [--root PATH] [--spec PATH] [--plan PATH]"
---

# init

Bootstraps the `claude-arsenal/` framework in the host repository. Run once per repo;
re-run to add workspaces or refresh the bundle. The script prints what it created,
refreshed and skipped, so relay its output rather than re-describing it. The parts
below are the ones that need the user.

CANARY: init-loaded-2026-06-13-fb78d23e-a1b2c3d4e5f6a7b8

## When to load

- A repo needs the task queue set up for the first time ("init the arsenal", "/init").
- Adding a new workspace to an existing `claude-arsenal/` setup.

## First install — ask for the profile

Every installed skill adds a row to the skills listing of every future session in
this repo, so ask before installing, when `arsenal/config.toml` has no `[skills]`
table yet:

> What kind of project is this? **python** (adds the Python toolchain skills),
> **general** (spec/design/execute/review/ship), or **minimal** (queue and
> GitHub only)?

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/init.py" --repo-path . --profile general
```

Take the answer from the user rather than the file tree: a repo with `.py` files is
not necessarily one whose release process this bundle should advise on. The profile
is written as an editable `[skills]` table; `--sections workflow,python` names
sections directly instead.

## Setup interview

On a first install, or when the user asks to redo setup (`/init --interview`), ask
these in one AskUserQuestion with the defaults first, and let the user skip it. An
upgrade does not ask again, because the answers are already in `arsenal/config.toml`.

1. How often should a session stop to ask? `ask-when-blocked` (default), `ask-often`,
   `autonomous`.
2. How deep should local verification go? `balanced` (default), `fast`, `strict`.
3. Minutes per review round (10) and minutes to wait on a silent review bot (20)?
4. Which manual review triggers to post? Offer what `init.py --suggest-bot-triggers`
   prints for the configured bots.

Pass only what was answered; a skipped question writes nothing and keeps its default:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/init.py" --repo-path . --autonomy autonomous \
  --verification balanced --review-budget-min 10 --bot-wait-min 20 \
  --bot-triggers "coderabbitai[bot]=@coderabbitai review"
```

## After a first install — questions init prints

Init runs non-interactively, so it prints what is undecided and the session asks:

- `HOST-GATE UNSET` — ask what must pass before a PR opens, offering init's
  suggestion, and write it as `host-gate` in `arsenal/config.toml`. "No gate" is a
  valid answer, recorded as `host-gate = "none"`; an empty value means nobody
  decided, and init keeps asking.
- `PREFLIGHT-GATE UNSET` — ask for the fast, change-scoped slice (lint/typecheck the
  changed files, the tests the change selects) and write it as `preflight-gate`.
  Review rounds run it instead of the full `host-gate`.
- `MERGE-POLICY is …` — confirm the value against the repo's real CI and review
  tooling in the same question; a repo with no CI should not stay on `after-ci`.
  For a private repo or one with no CI, init recommends `always` with a real
  `host-gate` and `pre-pr-review = "required"`; say the trade-off (nothing
  independent re-runs the local gates) and change only what the user confirms.
  Load `claude-arsenal:core:init § references/ci-minutes.md` when the user asks
  about CI cost or the per-PR trigger block.
- **Branch protection** — a deliberate run protects the default branch once and
  records the outcome as `branch-protection`. When it cannot, it prints the reason
  and the manual step; pass both to the user. `branch_protection.py` (in
  `claude-arsenal/scripts/`) re-runs it: `--dry-run` previews, `--force` adds to an
  existing rule.
- **A skill folder "is not arsenal-vendored — left alone"** — init replaces and
  prunes only folders carrying the `.arsenal-vendored` marker, so a same-named
  folder without it keeps the shipped skill out. Ask the user whether to remove or
  rename theirs, then re-run.

## Other invocations

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/init.py" --repo-path . --silent         # session-start refresh
python3 "${CLAUDE_SKILL_DIR}/scripts/init.py" --repo-path . --list-sections  # capability map, read-only
python3 "${CLAUDE_SKILL_DIR}/scripts/init.py" --repo-path . --section python  # one section's skills
python3 "${CLAUDE_SKILL_DIR}/scripts/init.py" --workspace BACKEND --root ./backend/
```

`--silent` is what the session-start protocol runs; it refreshes stale bundle
files and prints an upgrade banner when the plugin is newer. `--workspace NAME`
creates `arsenal/project/<NAME>/` stubs and updates `arsenal/project/overview.md`.

## Gotchas

- **Upstream owns `claude-arsenal/`, the host owns `arsenal/`.** Re-runs overwrite
  stale bundle files by checksum and never touch `arsenal/` (tasks, project data,
  settings).
- **Cloud sessions need the commit.** A cloud session runs on a fresh clone and
  never sees `~/.claude/`, so what reaches it is the vendored `.claude/skills/`
  and the hooks in `.claude/settings.json` — commit both.
- **Web without hooks.** `detect_surface.sh` does not run there, so tasks that
  declare `requires:` wait until it is run by hand; tasks without `requires:` are
  eligible everywhere.
- **The injected block belongs in the root `CLAUDE.md`**, not a nested one.
- **Refresh only moves forward.** When the host's bundle is newer than this
  skill's copy, init writes nothing and says so; update the plugin instead.
  `--allow-downgrade` is for recovering a broken install.
