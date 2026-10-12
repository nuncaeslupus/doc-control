---
name: github
description: Applies Conventional Commits and branch naming, opens PRs, and polls review bots and CI until the PR can merge. Use when the user commits, opens a PR or addresses review feedback. Not for reviewing a diff (review) or plain git mechanics (execution).
metadata:
  type: workflow
---

# github

Apply commit and PR conventions, then run an automated review loop instead of asking
the user to relay bot comments. After a PR opens, the loop watches what gates a merge
— CI, review-bot reactions and comments, and merge conflicts — answers each comment,
and tells the user when the PR is ready.

CANARY: github-loaded-2026-05-20-d436255c-54a7f770e04e4983

## When to load

- The user asks to commit, open a PR, or amend a PR body.
- The user asks whether CI is done, what a review bot said, or to address the review.
- A `/loop` cycle is checking PR state.

Judging whether a diff is correct belongs to `review` or `execution`; this skill owns
the mechanics of the review loop.

## Commit conventions

Conventional Commits: `<type>(scope): description`. Types: `feat`, `fix`, `refactor`,
`test`, `docs`, `chore`, `ci`. Scope is the module or domain. Imperative voice, no
trailing period, first line ≤72 chars. Body after a blank line explains *why*.

End the commit with the `Co-Authored-By:` line the harness's git instructions supply,
copied verbatim; add no other model name to commits, PR bodies, prose or
examples, because the harness value is the one that stays current.

## PR conventions

```markdown
## Summary
<1-3 bullets>

## Test plan
- [ ] <verifiable check>
```

Branches: `feat/<short-description>`, `fix/<short-description>`. Main branch is `main`.
For several PRs that merge in sequence, load
[stacking](references/stacking.md) before creating the second branch.

## Pre-PR gate

Before `gh pr create`, run the host repo's lint/format/test gate (`make lint`,
`npm run lint`, …), since the review loop assumes CI was green at push time. If the
project has no lint target, say so, propose one, and proceed.

```bash
FAST="${CLAUDE_SKILL_DIR}/../init/assets/bin/fast_gate.sh"
bash "$FAST"          # each review round: `preflight-gate` over the changed files
bash "$FAST" --full   # before merge, only if no receipt or green CI covers the tree
```

`--full` writes a receipt reused while the tree is unchanged. Review rounds run the
fast gate once, then push once, because every push is a CI run. Load
`claude-arsenal:core:init § references/ci-minutes.md` when CI minutes are a concern.

A green gate says the change does not break the repo, not that it is the change that
was asked for; run the independent review before opening the PR, and on the open PR
when its decision line calls for one: `claude-arsenal:core:init § references/pre-pr-review.md`.

## The review loop

**In a cloud session that offers PR activity events** (`subscribe_pr_activity` is in
its tools), subscribe to the PR instead of starting `/loop`, then end the turn: CI
results, reviews, comments and conflicts each wake the session, so polling only spends
turns on what is already pushed. On each wake, run `query_pr_state.py --pr <N>
--unresolved-only --trigger` once and act on its state as listed below and per the
rubric in the loop command. Some changes send no event (a silent bot, a reaction, the
quiet window, mergeability), so whenever the script exits 1 in a state other than
`ci_running`, schedule one check-in rather than a loop. If the subscription result says
another agent already receives the events, run the loop instead. Everywhere else, run
the loop.

After `gh pr create` returns the PR number, start the loop with the rubric inline, so
each tick carries its own instructions, and with `--unresolved-only`, so handled
comments drop out:

```bash
/loop 90s python3 "${CLAUDE_SKILL_DIR}/scripts/query_pr_state.py" --pr <PR_NUMBER> --unresolved-only --trigger — if state is bot_commented, address per the rubric (agree → fix, then after the round's last fix run fast_gate.sh once and push once + reply "addressed in <sha>" via gh api repos/<owner>/<repo>/pulls/<PR_NUMBER>/comments/<id>/replies; disagree → reply with rationale on the same endpoint; ambiguous → reply asking for clarification + ping the user). Pair every fix or dismissal with a reply on the thread, since the reply is what --unresolved-only filters on next tick. If bot_skipped / bot_rate_limited / bot_absent, follow its decision: local-review none → continue; diff or full → run one review round per the pre-PR review protocol, then add --local-review-done. If conflicts, rebase onto (or merge) the base branch, resolve, and push. If ci_failed, fetch the failing job log, fix, commit and push, then reply on related comments. Stop only on ready_to_merge, merged, or closed (bot_approved still waits for the quiet window); then CronDelete <job-id> and hand back to the user.
```

`/loop` rounds `90s` up to every 2 minutes. Stop early with `CronDelete <job-id>`
(`CronList` recovers the ID).

The script prints JSON and exits 0 when there is something to act on or the loop is
done, 1 while waiting, 2 on `conflicts` or `ci_failed`. Exit 2 with any other state
(authentication, repo not found) is permanent: stop and tell the user.

- `conflicts` — rebase onto the base branch, resolve, push. When the parent PR of a
  stacked branch merged, use `rebase_stack.sh` from [stacking](references/stacking.md).
- `ci_failed` — `gh run view --log-failed <run-id>`, fix, push, reply on related comments.
- `ready_to_merge` — tell the user "PR #N ready to merge". In a repo with
  `arsenal/config.toml`, read `merge-policy` and the vendored protocol's completion
  step first: the host may already have answered whether an agent merges.

Load [pr-review-loop](references/pr-review-loop.md) when a state is unclear
(`bot_eyeing` that persists, a bot that never reviews), when configuring watched
bots or `bot-triggers`, or when judging a comment the inline rubric does not settle.

## Projects Classic vs v2

Projects Classic breaks `gh pr view --comments` and `gh pr edit --body`. On first use
in a repo:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/query_project_type.py" --write-claude-md
```

It prints `classic` / `v2` / `none` and records `<!-- github-skill: projects=… -->`
in `CLAUDE.md` so later sessions skip detection. When it says `classic`, load
[projects-detection](references/projects-detection.md) for the workarounds.
