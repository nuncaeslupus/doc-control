#!/usr/bin/env python3
"""arsenal_config.py — read the host-owned `arsenal/config.toml`.

The point of this file is that a consumer configures behaviour **without
editing a skill**. Skills are vendored build output: an upgrade overwrites
them, so a preference stored in one is a preference that silently disappears.
`arsenal/config.toml` is host-owned and upstream never rewrites it.

Every key has a default, so a repo with no config file behaves sensibly and a
repo with a partial file only overrides what it names.

Keys are flat, except that a TOML table reads as dotted keys — `[models]`
with `workers = "sonnet"` is the key `models.workers`. Grouping the model
choices under one header is what lets a consumer state them the way they think
of them — one decision per role — rather than as unrelated top-level strings.

Usage:
    arsenal_config.py                      # print the effective config as JSON
    arsenal_config.py --get merge-policy   # print one value, bare
    arsenal_config.py --get models.workers # dotted key: a value inside a table
    arsenal_config.py --explain            # show each value and where it came from

Exit: 0 on success, 2 on an invalid value (a typo in a policy name is a
configuration bug worth failing loudly for, not silently defaulting past).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

# Defaults live here rather than being scattered through the scripts that read
# them, so `--explain` can always say what the fallback is.
DEFAULTS: dict[str, Any] = {
    # How far a task PR must get before it may be merged. Read at the merge
    # step itself — `AGENTS.md` § Completion, expanded in
    # `references/github-automation.md`, which maps each value to a check.
    # It pointed at the `github` skill for six versions while nothing there,
    # or anywhere else in the bundle, ever read the key (#192): a policy
    # nothing consults decides nothing, and the cost is quiet — every merge
    # stops to ask a question the host already answered.
    "merge-policy": "after-ci",
    # Shell command run by open_task_pr.sh before a task PR is opened, refusing
    # on non-zero. Empty by default: a repo with no gate is unaffected. This is
    # what gives worker.md's "run the host lint gate" a data path — the prose
    # named `make lint` as its example, so a repo whose real gate is five
    # commands had four of them enforced by nobody.
    "host-gate": "",
    # The cheap counterpart, run by `open_task_pr.sh <task> --preflight` over
    # the archived tree and nowhere else. Empty by default: the honest default
    # is that this bundle cannot see inside `make test`, so preflight without
    # it checks only that the gates RESOLVE.
    #
    # It exists for the one failure resolution cannot reach. The host gate runs
    # after the task file is archived into `tasks/_history/`, so a gate that
    # measures the repo's own files reads a different tree than the one the
    # worker tested — and the worker learns that from a refusal at the end of
    # the run (#220 is that failure, with the archive as the difference).
    # Declare the slice of the gate that touches those files here and preflight
    # finds it in seconds.
    #   preflight-gate = "make verify-gates"
    #
    # It is also the fast gate level (#463): bin/fast_gate.sh runs it on every
    # review round after the first, over ARSENAL_CHANGED_FILES, so the full
    # host-gate is paid once before the PR opens and once before merge rather
    # than once per push. Change-scoped lint/typecheck plus the tests the change
    # selects is the usual shape.
    "preflight-gate": "",
    # Shell command run by bin/host_setup.sh in a fresh worktree, before the
    # first gate. Empty by default. A worktree carries tracked files and nothing
    # an install produces, so the first gate in one fails on a missing tool
    # rather than on the change under test — and every worker rediscovers that
    # separately. Naming the command here is what turns it from a diagnosis
    # into a step.
    "host-setup": "",
    # How hard the pre-PR adversarial review binds ON THE TASK-PR PATH. Read by
    # open_task_pr.sh and nowhere else, via bin/adversarial_review.sh, whose
    # `check` asks whether a reviewer that never saw this work cleared THIS
    # tree. It is not a global switch: `execution`, `github` and `ship` run the
    # same gate as a step of their own workflow, and a session following those
    # skills does not consult this key.
    #   warn      (default) open the PR either way, and record the outcome —
    #             cleared, blocked, stale, or never run — in the PR body, where
    #             the human merging it looks. Chosen as the default because a
    #             gate that breaks every existing worker loop on upgrade gets
    #             turned off, and one that says nothing gets forgotten.
    #   required  refuse to open the PR without a CLEAR receipt for this tree.
    #   off       skip the check and write no line.
    "pre-pr-review": "warn",
    # How many adversarial-review rounds one change gets before the loop is
    # declared non-convergent. Read by bin/adversarial_review.sh, which refuses
    # to emit a packet past it. Round two is a follow-up on the previous
    # findings plus the delta, and only BLOCKERs earn it.
    #
    # The counter belongs to the branch, not the base commit: when it was bound
    # to the base, every rebase or base merge restarted it, and one PR ran eight
    # rounds of ~40 minutes under a cap of three. `adversarial_review.sh --reset`
    # starts over deliberately.
    "review-max-rounds": 2,
    # How often a session stops to ask the user. Read by AGENTS.md, which every
    # session loads; /init sets it from the setup interview.
    #   ask-often         confirm each step before taking it
    #   ask-when-blocked  ask only when blocked or before a risky action
    #   autonomous        never wait on the user for reversible work
    "autonomy": "ask-when-blocked",
    # How deep local verification goes, given what CI and the review bots
    # already did on the PR. Read by scripts/review_sources.py (the decision
    # line every caller follows), bin/fast_gate.sh and bin/adversarial_review.sh.
    #   fast      local review only for high-risk diffs or when nothing external ran
    #   balanced  no local review when CI and a bot both passed a low-risk diff;
    #             one diff review, targeted tests only, when the bots are out
    #   strict    always one local review; reviewer may run the full suite and
    #             mutations; no CI-green shortcut at merge
    "verification": "balanced",
    # Minutes one review round is told it has, as an elapsed/budget line in the
    # packet. Advisory to the reviewer; the dispatcher stops waiting at twice it.
    "review-budget-min": 10,
    # Minutes a configured review bot may stay silent on a PR head before
    # review_sources.py sends its trigger (see bot-triggers) and, after a
    # second wait, records it as absent so the loop moves on.
    "bot-wait-min": 20,
    # Manual review commands, one "login=comment" per bot. A bot that skipped
    # the PR (some skip small or unstarred repos) or stayed silent gets this
    # comment once per head. Empty means never post one. A starting value: /init
    # suggests entries for the bots in review-bots.
    "bot-triggers": [],
    # Globs whose change always gets a local review round, whatever CI and the
    # bots said (auth, migrations, release scripts — your call).
    "risk-paths": [],
    # A diff larger than this many changed lines always gets a local round.
    "risk-lines": 400,
    # Which review bots the PR review loop waits on, by GitHub username. Read
    # by the github skill's query_pr_state.py, which watches for their :eyes:,
    # their line comments and their approval before a PR is callable.
    #
    # The three shipped here are a starting value, not a claim about your repo:
    # they are the ones this bundle was exercised against. A repo running a
    # different reviewer had no way to say so short of passing --watch-bots at
    # every call, and a repo running none at all had the loop wait for a signal
    # that was never coming.
    #
    # An empty list is the documented way to say "no review bot here": the loop
    # then runs CI-only, and `ready_to_merge` rests on green CI alone. That is
    # the weaker gate, which is why it is opt-in rather than the default — a
    # host that has a bot should not lose it by upgrading.
    "review-bots": ["gemini-code-assist[bot]", "coderabbitai[bot]", "claude[bot]"],
    # The skills-listing character budget the auditor enforces. It is a real
    # constraint, but its value differs by surface and has changed over time,
    # so a consumer whose budget differs can set it here instead of being
    # unable to pass the audit at all (#143).
    "listing-budget": 8000,
    # Whether `/init` keeps .github/workflows/arsenal-queue.yml installed. Set
    # to `true` on first install and to `false` when the file is found deleted,
    # so removing it is a real opt-out rather than something the next session's
    # `init --silent` quietly undoes.
    "queue-automation": True,
    # What `/init` did about GitHub branch protection on the default branch,
    # recorded so the API is asked once rather than on every run: applied,
    # existing (a rule was already there and was left alone), unavailable
    # (GitHub refused — a plan or permission limit), or off (--no-branch-protection).
    # Empty = not yet attempted. Clear it to have the next `/init` try again.
    "branch-protection": "",
    # The label an issue must carry before `issue_import.py` turns it into a
    # task. Opt-in on purpose: every open issue becoming claimable work means a
    # worker opens a PR against a question someone asked.
    "import-label": "arsenal:queue",
    # Where host-owned project content lives, relative to the repo root.
    "home": "arsenal",
    # The label that marks an issue as a machine-managed task handle. Anything
    # without it is never treated as claimable work, which is what keeps
    # ordinary issues people file out of the queue.
    "task-label": "arsenal:task",
    # Ref namespace for atomic claim refs.
    "claim-prefix": "arsenal/claims",
    # Which skill sections /init vendors into .claude/skills/, on top of the
    # always-installed core. Registered here so `--explain` can report them and
    # so the loader keeps them; the values are written by init.py from the
    # profile chosen at install, not hand-seeded into the config template —
    # one writer, so the shipped defaults and the recorded answer cannot drift.
    # The defaults here are what a FRESH install gets; an upgrade preserves
    # whatever the repo already had (init.py:_resolve_sections).
    "skills.workflow": True,
    "skills.python": False,
    # The auto-compact threshold, in tokens, written into the host's
    # `.claude/settings.json` as `autoCompactWindow` by `/init`. 0 means "no
    # opinion" — leave whatever the harness defaults to, and never touch a value
    # already in settings.json.
    #
    # This is the largest single lever on what a fleet costs, and it is the one
    # that reads backwards. Cost is `turns x context`, not output: a day of nine
    # sessions read 438M tokens to write 1.3M. A host that raises its window to
    # "avoid filling up" raises the per-turn floor instead of lowering it —
    # replaying those same turns at lower caps gives 500k -> 453M, 300k -> 387M,
    # 200k -> 287M, 120k -> 186M. Compaction is the cheap event; carrying the
    # context that postpones it is the expensive one.
    #
    # Off by default because the right value is a judgement about this repo's
    # work, not one upstream can make: too low and sessions compact mid-task and
    # re-read what they dropped, which costs turns instead of context. Set it,
    # measure with scripts/usage_report.py, move it.
    "context-window": 0,
    # Which model runs the session that dispatches work. Advisory, and the one
    # key here nothing can enforce from inside a session: a session cannot
    # change the model it is already running as, so this is read and reported
    # at session start for the person launching it (`claude --model <value>`).
    # Empty means "whatever the session was launched with" — no opinion.
    "models.orchestrator": "",
    # Which model runs worker subagents. This one has a data path: the
    # orchestrator resolves it and passes it as the dispatch's own model
    # argument, so it governs every worker in the session. It used to be a
    # model id hardcoded in the protocol prose, which meant a consumer who
    # wanted a different one had to edit a vendored file an upgrade overwrites.
    #
    # The transport is the dispatch argument and not an exported
    # CLAUDE_CODE_SUBAGENT_MODEL because on cloud surfaces every Bash call gets
    # a fresh shell: the export died with the call that made it, and the fleet
    # ran on the orchestrator's model — the expensive default — while this key
    # resolved to something cheaper and governed nothing (#379).
    "models.workers": "sonnet",
    # Which model runs adversarial reviewer subagents (`agents/reviewer.md`,
    # dispatched from a case file by bin/adversarial_review.sh). Empty means
    # "no separate opinion — use models.workers", which is why it is not
    # defaulted to a model name: copying the workers value here would make the
    # two drift the moment a consumer edits one of them.
    #
    # It exists because the roles are not symmetric. An implementer is usually
    # applying a named remedy; the reviewer derives the spec and mutates
    # against a change it has never seen, and it is the half that earns the
    # stronger model. Before this key the only lever was `models.workers`,
    # which governs the other half — and a `reviewers = ...` written anyway
    # parsed fine, sat in the file looking configured, and reached nothing,
    # because unknown keys are tolerated on read (below). A key that records a
    # decision and changes nothing is worse than no key (#380).
    "models.reviewers": "",
}

ENUMS: dict[str, set[str]] = {
    # `after-review` is not a weaker `after-ci-and-review` — it is the other
    # axis. `after-ci` is "the machines agree"; `after-review` is "a reader
    # agrees"; a repo should be able to require either, both, or neither.
    # Without it, a repo whose CI is structurally unavailable — out of runner
    # minutes, or no CI at all — can only choose between a policy that blocks
    # every merge indefinitely and one that everybody learns to wave through.
    "merge-policy": {"always", "after-review", "after-ci", "after-ci-and-review", "never"},
    # open_task_pr.sh compares this against the literal "required", so anything
    # else — "Required", "requried", "on" — takes the warn path: the PR opens,
    # its body says no review ran, and the consumer who wrote the value believes
    # a binding gate is in place. A misspelled opt-out fails the other way,
    # writing a line into the body of someone who switched the check off.
    "pre-pr-review": {"warn", "required", "off"},
    "verification": {"fast", "balanced", "strict"},
    "autonomy": {"ask-often", "ask-when-blocked", "autonomous"},
    "branch-protection": {"", "applied", "existing", "unavailable", "off"},
}

CONFIG_RELPATH = "config.toml"

# What reads each key, by path from the repository root. A key with no reader
# is a setting that scaffolds, validates, round-trips through `--explain`, and
# changes nothing — six shipped that way, and the worst of them was documented
# in AGENTS.md as configurable, so a consumer who set it silently imported no
# issues at all. `config_keys_test.sh` asserts this map covers DEFAULTS and
# that each named file mentions its key, which is what makes adding a key
# without wiring it a failing build rather than a discovery months later.
READERS = {
    "merge-policy": "plugins/core/skills/init/assets/bin/merge_ready.sh",
    "host-gate": "plugins/core/skills/init/assets/bin/open_task_pr.sh",
    "preflight-gate": "plugins/core/skills/init/assets/bin/open_task_pr.sh",
    "host-setup": "plugins/core/skills/init/assets/bin/host_setup.sh",
    "pre-pr-review": "plugins/core/skills/init/assets/bin/open_task_pr.sh",
    "review-max-rounds": "plugins/core/skills/init/assets/bin/adversarial_review.sh",
    "verification": "plugins/core/skills/init/assets/scripts/review_sources.py",
    "autonomy": "plugins/core/skills/init/assets/AGENTS.md",
    "review-budget-min": "plugins/core/skills/init/assets/bin/adversarial_review.sh",
    "bot-wait-min": "plugins/core/skills/init/assets/scripts/review_sources.py",
    "bot-triggers": "plugins/core/skills/init/assets/scripts/review_sources.py",
    "risk-paths": "plugins/core/skills/init/assets/scripts/review_sources.py",
    "risk-lines": "plugins/core/skills/init/assets/scripts/review_sources.py",
    "review-bots": "plugins/core/skills/github/scripts/query_pr_state.py",
    "listing-budget": "plugins/skill-workshop/skills/skill-workshop/scripts/audit_library.py",
    "queue-automation": "plugins/core/skills/init/scripts/init.py",
    "branch-protection": "plugins/core/skills/init/scripts/init.py",
    "import-label": "plugins/core/skills/init/assets/scripts/issue_import.py",
    "task-label": "plugins/core/skills/init/assets/scripts/queue_hooks.py",
    "claim-prefix": "plugins/core/skills/init/assets/scripts/queue_hooks.py",
    # `[skills]` is read as a table, so init.py never names a section: the names
    # are data, and sections.json is where one is declared.
    "skills.workflow": "plugins/core/skills/init/assets/sections.json",
    "skills.python": "plugins/core/skills/init/assets/sections.json",
    "context-window": "plugins/core/skills/init/scripts/init.py",
    "models.orchestrator": "plugins/core/skills/init/assets/AGENTS.md",
    "models.workers": "plugins/core/skills/init/assets/agents/worker.md",
    "models.reviewers": "plugins/core/skills/init/assets/agents/reviewer.md",
    # `home` is the exception, and the only one: it names the directory holding
    # config.toml, so it cannot be read from config.toml. ARSENAL_HOME is the
    # channel, and FILE_ONLY_REJECTS below refuses the key in the file rather
    # than accepting it and relocating nothing.
    "home": None,
}

# Keys that must not be set in the file. Accepting one there looks like it
# works — `--explain` echoes it back — and does nothing at all.
FILE_ONLY_REJECTS = {
    "home": "ARSENAL_HOME (the file lives inside the directory this names)",
}


class ConfigError(Exception):
    """An invalid configuration value — loud on purpose."""


def _config_path(repo_root: Path, home: str) -> Path:
    return repo_root / home / CONFIG_RELPATH


# A model may be an alias Claude Code resolves (`opus`, `sonnet`, `haiku`) or a
# full model id. Deliberately not an enum: model names change every few months,
# and a closed set here would reject the model a consumer is actually running
# — the vendored file would have to ship a new version to allow a name that
# already works everywhere else. So the check is on shape, not membership: a
# bare token, because the value is interpolated into a dispatch argument (and,
# on surfaces where it survives, an exported environment variable), and
# anything with quotes, spaces or shell metacharacters in it is a typo at best.
MODEL_VALUE_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

MODEL_KEYS = ("models.orchestrator", "models.workers", "models.reviewers")

# Claude Code's own accepted bounds for `autoCompactWindow`; it additionally caps
# the value at the running model's context window, which is not knowable here.
CONTEXT_WINDOW_RANGE = (100_000, 1_000_000)


def _flatten(raw: dict[str, Any]) -> dict[str, Any]:
    """One level of TOML table → dotted keys: `[models] workers=` → `models.workers`.

    Only one level, because the config is a flat list of settings that happens
    to group two of them. Recursing would invite a nesting depth nobody reading
    `--explain` output could hold in their head.
    """
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            for sub, subvalue in value.items():
                flat[f"{key}.{sub}"] = subvalue
        else:
            flat[key] = value
    return flat


def load(repo_root: Path | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Return (effective config, source-of-each-value).

    Sources are reported so `--explain` can print "8000 (default)" versus
    "12000 (arsenal/config.toml)". A threshold whose value is invisible in the
    output is one nobody can tell has been quietly raised.
    """
    root = repo_root or Path.cwd()
    values: dict[str, Any] = dict(DEFAULTS)
    sources: dict[str, str] = dict.fromkeys(DEFAULTS, "default")

    # ARSENAL_HOME may relocate the whole host-owned tree; it also decides
    # where we look for the config itself, so it is resolved first.
    # `or`, not a get() default: an exported but empty ARSENAL_HOME would
    # otherwise make the host root the empty string, putting every
    # host-owned path at the repo root.
    home = os.environ.get("ARSENAL_HOME") or DEFAULTS["home"]
    if home != DEFAULTS["home"]:
        values["home"] = home
        sources["home"] = "ARSENAL_HOME"

    path = _config_path(root, home)
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: not valid TOML — {exc}") from exc
        for key, value in _flatten(raw).items():
            if key in FILE_ONLY_REJECTS:
                raise ConfigError(
                    f"{path}: {key} cannot be set here — use {FILE_ONLY_REJECTS[key]}"
                )
            if key not in DEFAULTS:
                # Unknown keys are tolerated rather than fatal: a consumer on
                # an older bundle should not break when a newer one adds a key,
                # and vice versa.
                continue
            values[key] = value
            sources[key] = str(path)

    # `host-gate = "none"` is the recorded answer "this repo has no gate", as
    # distinct from an empty value nobody chose — init nags about the second
    # and not the first. Every reader runs a non-empty value as a command, so
    # it is normalised here, once, rather than taught to each of them.
    if isinstance(values["host-gate"], str) and values["host-gate"].strip().lower() == "none":
        values["host-gate"] = ""

    for key, allowed in ENUMS.items():
        # The isinstance() guard comes first because TOML permits an array or a
        # table for any key, and both are unhashable: `value not in allowed`
        # would raise TypeError and print a traceback instead of the readable
        # ConfigError this function documents.
        if not isinstance(values[key], str) or values[key] not in allowed:
            raise ConfigError(
                f"{key}: {values[key]!r} is not one of {sorted(allowed)} (from {sources[key]})"
            )
    # Same strictness init.py applies when it reads this table: a non-boolean
    # here decides whether skills are installed or pruned, so a typo must stop
    # rather than be coerced.
    for key in (k for k in DEFAULTS if k.startswith("skills.")):
        if not isinstance(values[key], bool):
            raise ConfigError(
                f"{key} must be true or false, got {values[key]!r} (from {sources[key]})"
            )
    # `type(...) is int`, not `isinstance`: `bool` subclasses `int` in Python, so
    # `listing-budget = true` passed validation and produced `True` — which then
    # behaves as the number 1 everywhere downstream, capping the skills listing
    # at one character rather than being refused as the wrong type.
    if type(values["listing-budget"]) is not int or values["listing-budget"] <= 0:
        raise ConfigError(
            f"listing-budget must be a positive integer, got {values['listing-budget']!r}"
        )
    # Same `type(...) is int` guard and the same reason as listing-budget above:
    # `review-max-rounds = true` would otherwise validate and cap the review at
    # one round, which reads as the gate having become stricter on its own.
    if type(values["review-max-rounds"]) is not int or values["review-max-rounds"] < 1:
        raise ConfigError(
            f"review-max-rounds must be an integer >= 1, got "
            f"{values['review-max-rounds']!r} (from {sources['review-max-rounds']})"
        )
    for key in ("review-budget-min", "bot-wait-min", "risk-lines"):
        if type(values[key]) is not int or values[key] < 1:
            raise ConfigError(
                f"{key} must be an integer >= 1, got {values[key]!r} (from {sources[key]})"
            )
    for key in ("risk-paths", "bot-triggers"):
        if not isinstance(values[key], list) or not all(isinstance(v, str) for v in values[key]):
            raise ConfigError(
                f"{key} must be a list of strings, got {values[key]!r} (from {sources[key]})"
            )
    for entry in values["bot-triggers"]:
        login, sep, comment = entry.partition("=")
        if not sep or not login.strip() or not comment.strip():
            raise ConfigError(
                f'bot-triggers entries are "login=comment", got {entry!r} '
                f"(from {sources['bot-triggers']})"
            )
    # A list, because that is what it is — and a bare string here would be a
    # comma-separated list nobody told the consumer about. Empty is legal and
    # means "no review bot in this repo"; the reader below documents what that
    # costs.
    bots = values["review-bots"]
    if not isinstance(bots, list) or not all(isinstance(b, str) for b in bots):
        raise ConfigError(
            f"review-bots must be a list of usernames, got {bots!r} (from {sources['review-bots']})"
        )
    # Same `type(...) is int` guard and the same reason as listing-budget above.
    # The bounds are Claude Code's own for `autoCompactWindow`; a value outside
    # them is rejected here rather than written into settings.json, because a
    # settings key the harness discards is precisely the failure this key exists
    # to end — it would sit in two files looking configured and govern nothing.
    window = values["context-window"]
    low, high = CONTEXT_WINDOW_RANGE
    if type(window) is not int or (window != 0 and not low <= window <= high):
        raise ConfigError(
            f"context-window must be 0 (no opinion) or an integer between {low} "
            f"and {high}, got {window!r} (from {sources['context-window']})"
        )
    for key in MODEL_KEYS:
        value = values[key]
        if not isinstance(value, str):
            raise ConfigError(f"{key} must be a string, got {value!r} (from {sources[key]})")
        # Empty is meaningful for the orchestrator ("no opinion, use whatever
        # the session was launched with") and for reviewers ("no separate
        # opinion, use models.workers"). It is meaningless for workers, which
        # would hand the dispatch an empty model argument and silently get the
        # default — a setting that looks configured and is not.
        if value == "":
            if key == "models.workers":
                raise ConfigError(
                    "models.workers cannot be empty — name a model, or remove the key "
                    f"to get the default {DEFAULTS['models.workers']!r}"
                )
            continue
        if not MODEL_VALUE_REGEX.match(value):
            raise ConfigError(
                f"{key}: {value!r} is not a model name — expected a tier alias like "
                f"'opus' or a full model id (from {sources[key]})"
            )

    return values, sources


# A capability name lands in surface_profile.json and in `requires:` lists, so
# it is held to the shape those already use: `surface:cli`, `net:example.com`.
CAPABILITY_NAME_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


def capability_probes(repo_root: Path | None = None) -> dict[str, str]:
    """The host's `[capabilities]` table: capability name → probe command.

    `detect_surface.sh` runs each probe and grants the capability only on exit
    0. What differs between a laptop and a cloud container in a real repo —
    network egress to one host, a gitignored data directory — is not on the
    script's hard-coded list, and a hand-passed `--capability` is a claim
    nothing checks (#481). Kept out of DEFAULTS because the keys are the host's
    own, not settings this bundle knows.
    """
    root = repo_root or Path.cwd()
    home = os.environ.get("ARSENAL_HOME") or DEFAULTS["home"]
    path = _config_path(root, home)
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML — {exc}") from exc
    table = raw.get("capabilities", {})
    if not isinstance(table, dict):
        raise ConfigError(f'{path}: [capabilities] must be a table of name = "command"')
    probes: dict[str, str] = {}
    for name, command in table.items():
        if not CAPABILITY_NAME_REGEX.match(name):
            raise ConfigError(f"{path}: capability name {name!r} — use letters, digits and ._:/-")
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(f"{path}: capability {name!r} needs a non-empty probe command")
        probes[name] = command
    return probes


def setting(key: str, repo_root: Path | None = None) -> Any:
    """One configured value, for a module that needs a single key.

    Falls back to the shipped default when the config cannot be read, so a
    module-level constant resolved through this cannot make a script fail to
    import on a malformed file. An invalid config is still reported loudly
    where a human is looking — `--explain` and `init.py` both call `load()`
    directly and let ConfigError out.
    """
    try:
        return load(repo_root)[0][key]
    except (ConfigError, OSError, KeyError):
        return DEFAULTS[key]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--get", metavar="KEY", help="print a single value, bare")
    parser.add_argument("--explain", action="store_true", help="show each value and its source")
    parser.add_argument(
        "--probes",
        action="store_true",
        help="print the [capabilities] probes, one 'name<TAB>command' per line",
    )
    args = parser.parse_args(argv)

    if args.probes:
        try:
            probes = capability_probes(args.repo_root)
        except ConfigError as exc:
            print(f"arsenal_config: {exc}", file=sys.stderr)
            return 2
        for name, command in probes.items():
            print(f"{name}\t{' '.join(command.splitlines())}")
        return 0

    try:
        values, sources = load(args.repo_root)
    except ConfigError as exc:
        print(f"arsenal_config: {exc}", file=sys.stderr)
        return 2

    if args.get:
        if args.get not in values:
            print(f"arsenal_config: unknown key {args.get!r}", file=sys.stderr)
            return 2
        value = values[args.get]
        # Bare output is meant to be substituted into a command. A list's repr
        # is not: it is the comma-separated form the flags reading it take.
        print(",".join(value) if isinstance(value, list) else value)
        return 0

    if args.explain:
        width = max(len(k) for k in values)
        for key in sorted(values):
            src = "default" if sources[key] == "default" else sources[key]
            print(f"{key:<{width}}  {values[key]}   ({src})")
        return 0

    print(json.dumps(values, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    # Windows consoles default to a legacy codepage (cp1252 and friends);
    # a non-ASCII line must degrade to "?", never take the process down.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
