#!/usr/bin/env python3
"""pr_audit.py — which open PR is waiting, and on what.

`query_status.py` reports the board from issues and task files. It answers
"what is claimed and what is done", and it is silent on the state a stalled
fleet actually lands in: eight sessions each open a PR, the machine goes down
mid-cycle, and three days later seven PRs are open with CI green, claims still
held and issues still assigned. Every ledger reads "in progress". Nothing is.

So this asks the other question. For each open PR it prints the head SHA and
the merge conditions **against that SHA**, plus the one next action. The SHA
matters more than it looks: a review of an earlier tree is not a review of this
one, and a check run that reported on the previous push is not evidence about
what would be merged.

It performs no network I/O. The caller hands it the payload GitHub already
returned (`--prs`), the same split `query_status.py` uses, because the surfaces
this bundle runs on do not all have a scriptable GitHub channel and a report
that only works where `gh` is installed is a report that goes missing exactly
when a fleet is in trouble. `bin/merge_ready.sh` is the fetching half; it calls
this for the verdict so the conditions are evaluated in one place instead of
being re-derived by hand in every session that merges.

Input (`--prs PATH|-`): a JSON array of PR records. Unknown keys are ignored,
so the raw GitHub shapes can be passed through; the keys read are

    number, title, draft, mergeable, created_at, updated_at,
    head: {sha, ref},
    checks:  [{name, status, conclusion, head_sha}]     — for the head SHA
    reviews: [{state, commit_id, submitted_at, user: {login}}]
    threads: [{id, resolved, path}]                      — review threads

Optionally `--claims PATH`: a JSON array of `{ref, task_id, created_at, pr}`
for the claim refs currently held, to name the claims nothing is working on.

Exit: 0 report produced (with `--require-ready`: the PR is mergeable under the
policy); 1 `--require-ready` and it is not; 2 bad input or bad policy;
3 the policy is `never` — ready or not, a human merges.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

POLICIES = {"always", "after-review", "after-ci", "after-ci-and-review", "never"}

# Conclusions that are not a failure. `neutral` and `skipped` are how a matrix
# job reports "not applicable here", and a required check that legitimately did
# not apply must not read as a red PR forever.
CHECK_OK = {"success", "neutral", "skipped"}

# GitHub's own vocabulary for a review that is not a verdict. A COMMENTED review
# is somebody talking, not somebody approving, and counting it as a review is how
# `after-review` gets satisfied by a bot's "looks fine so far".
REVIEW_VERDICTS = {"APPROVED", "CHANGES_REQUESTED"}


class AuditError(Exception):
    """Bad input — distinct from a PR that is simply not ready."""


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_hours(raw: object) -> float | None:
    when = _parse_time(raw)
    return None if when is None else (_now() - when).total_seconds() / 3600.0


def _short(sha: object) -> str:
    return str(sha)[:7] if isinstance(sha, str) and sha else "?"


def _dig(record: dict[str, Any], *path: str) -> Any:
    cur: Any = record
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _records(value: object) -> list[dict[str, Any]]:
    """The dict entries of a list, tolerating nulls and stray scalars."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def read_policy(repo_root: Path) -> str:
    """`merge-policy` from arsenal/config.toml, or the bundle default.

    Read here rather than shelled out to `arsenal_config.py` so a fleet audit of
    forty PRs is one file read and not forty subprocesses.
    """
    config = repo_root / "arsenal" / "config.toml"
    if not config.is_file():
        return "after-ci"
    try:
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AuditError(f"cannot read {config}: {exc}") from exc
    value = raw.get("merge-policy", "after-ci")
    if value not in POLICIES:
        raise AuditError(f"merge-policy must be one of {sorted(POLICIES)}, got {value!r}")
    return str(value)


def check_state(pr: dict[str, Any], head: str) -> tuple[str, str]:
    """(state, detail) for CI on `head`.

    States: `green`, `failing`, `pending`, `absent`, `stale`.

    `absent` is deliberately not `green`. A repo out of runner minutes, with no
    workflows, or whose jobs die before a runner is assigned has produced no
    evidence — and "no failures" is not a pass. Whoever wrote that shortcut into
    a one-line `--jq` got a fleet that merges during a CI outage.
    """
    checks = _records(pr.get("checks"))
    # A check that reported on an earlier push is evidence about that push.
    for_head = [c for c in checks if not c.get("head_sha") or c.get("head_sha") == head]
    if checks and not for_head:
        return "stale", f"{len(checks)} check(s), none on {_short(head)}"
    if not for_head:
        return "absent", "no checks reported"

    failing = [
        str(c.get("name") or "?")
        for c in for_head
        if c.get("status") == "completed" and c.get("conclusion") not in CHECK_OK
    ]
    if failing:
        return "failing", ", ".join(sorted(failing)[:3])
    pending = [c for c in for_head if c.get("status") != "completed"]
    if pending:
        return "pending", f"{len(pending)} of {len(for_head)} still running"
    return "green", f"{len(for_head)} check(s)"


def review_state(pr: dict[str, Any], head: str) -> tuple[str, str]:
    """(state, detail) for review on `head`.

    States: `clear`, `unresolved`, `changes-requested`, `stale`, `absent`.

    Two rules from the merge-policy prose are enforced here because prose is
    where they kept being lost. A summary line is not the finding list: a bot can
    report "review completed" while leaving open threads, so unresolved threads
    beat any approval. And a review of an earlier tree is not a review of this
    one — the same rule `adversarial_review.sh check` applies to its receipts.

    Thread resolution is GraphQL-only, so on a REST-or-nothing surface the caller
    cannot supply it. An ABSENT `threads` key therefore says so in the detail and
    does not block, which is the opposite of how an absent check run is treated —
    deliberately. A missing check is a fetchable fact about a repo that produced
    no CI; a missing thread list is a field this channel cannot return, and a
    condition that cannot be satisfied anywhere is not a gate, it is a wall. An
    EMPTY list is a real answer and means no thread is open.
    """
    known = pr.get("threads") is not None
    threads = _records(pr.get("threads"))
    open_threads = [t for t in threads if not t.get("resolved")]
    caveat = "" if known else "; thread state unavailable on this channel"

    reviews = [r for r in _records(pr.get("reviews")) if r.get("state") in REVIEW_VERDICTS]
    if not reviews:
        # Open threads with no verdict is still "nobody has reviewed this": the
        # unresolved count is the more useful thing to say about it.
        if open_threads:
            return "unresolved", f"{len(open_threads)} open thread(s), no verdict"
        return "absent", f"no review{caveat}"

    on_head = [r for r in reviews if not r.get("commit_id") or r.get("commit_id") == head]
    if not on_head:
        latest = _short(reviews[-1].get("commit_id"))
        return "stale", f"reviewed {latest}, head is {_short(head)}"
    if open_threads:
        return "unresolved", f"{len(open_threads)} open thread(s)"
    if any(r.get("state") == "CHANGES_REQUESTED" for r in on_head):
        return "changes-requested", "changes requested on the head"
    return "clear", f"{len(on_head)} review(s) on the head{caveat}"


def _next_action(pr: dict[str, Any], audit: dict[str, Any], policy: str) -> str:
    """The single thing to do next. Ordered by what blocks what."""
    if pr.get("draft"):
        return "mark ready for review (it is a draft)"
    if pr.get("mergeable") is False:
        return "merge the base branch in and resolve the conflict"

    ci, ci_detail = audit["ci"], audit["ci_detail"]
    if policy in {"after-ci", "after-ci-and-review"}:
        if ci == "failing":
            return f"fix the failing check: {ci_detail}"
        if ci == "pending":
            return f"wait — {ci_detail}"
        if ci == "stale":
            return f"push or re-run: {ci_detail}"
        if ci == "absent":
            return "no checks reported on the head — absent is not green, so this is blocked"

    review, review_detail = audit["review"], audit["review_detail"]
    if policy in {"after-review", "after-ci-and-review"}:
        if review == "unresolved":
            return f"answer or fix {review_detail}"
        if review == "changes-requested":
            return "address the requested changes, then re-request review"
        if review == "stale":
            return (
                f"re-read the head: claim_review.sh {pr.get('number')} <head-sha> ({review_detail})"
            )
        if review == "absent":
            return f"dispatch a second reader: claim_review.sh {pr.get('number')} <head-sha>"

    if policy == "never":
        return "ready — merge-policy is `never`, so a human merges"
    return "merge"


def audit_pr(pr: dict[str, Any], policy: str) -> dict[str, Any]:
    head = _dig(pr, "head", "sha") or ""
    ci, ci_detail = check_state(pr, str(head))
    review, review_detail = review_state(pr, str(head))

    ci_ok = ci == "green"
    review_ok = review == "clear"
    needs_ci = policy in {"after-ci", "after-ci-and-review"}
    needs_review = policy in {"after-review", "after-ci-and-review"}

    ready = (
        not pr.get("draft")
        and pr.get("mergeable") is not False
        and (ci_ok or not needs_ci)
        and (review_ok or not needs_review)
    )

    result = {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "head": str(head),
        "branch": _dig(pr, "head", "ref") or "",
        "draft": bool(pr.get("draft")),
        "mergeable": pr.get("mergeable"),
        "age_hours": _age_hours(pr.get("created_at")),
        "idle_hours": _age_hours(pr.get("updated_at") or pr.get("created_at")),
        "ci": ci,
        "ci_detail": ci_detail,
        "review": review,
        "review_detail": review_detail,
        "ready": ready,
    }
    result["next_action"] = _next_action(pr, result, policy)
    return result


def stale_claims(
    claims: Iterable[dict[str, Any]], audits: list[dict[str, Any]], stale_hours: float
) -> list[dict[str, Any]]:
    """Claims held longer than `stale_hours` with no open PR behind them.

    A claim ref cannot be deleted from a sandboxed session, which is the right
    trade — a crashed session blocks nothing, because the next attempt takes the
    next ref. The cost is that the ref outlives the work, so nothing anywhere
    says "this task is claimed and no session is on it". This does.
    """
    open_tasks = {a.get("branch", "").rsplit("/", 1)[-1] for a in audits}
    open_prs = {a.get("number") for a in audits}
    stale = []
    for claim in claims:
        task_id = str(claim.get("task_id") or claim.get("ref", "").rsplit("/", 1)[-1])
        age = _age_hours(claim.get("created_at"))
        if claim.get("pr") in open_prs or task_id in open_tasks:
            continue
        if age is None or age < stale_hours:
            continue
        stale.append({"task_id": task_id, "ref": claim.get("ref", ""), "age_hours": age})
    return stale


def _load(path: str) -> Any:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuditError(f"{path}: not JSON ({exc})") from exc


def _hours(value: float | None) -> str:
    return "?" if value is None else (f"{value:.0f}h" if value < 96 else f"{value / 24:.0f}d")


def render(audits: list[dict[str, Any]], stale: list[dict[str, Any]], policy: str) -> str:
    lines = [f"pr_audit: {len(audits)} open PR(s), merge-policy `{policy}`", ""]
    if not audits:
        lines.append("  (nothing open)")
    for a in audits:
        flag = "READY" if a["ready"] else "WAIT "
        lines.append(
            f"  {flag} #{a['number']} {_short(a['head'])} {_hours(a['age_hours'])} "
            f"{a['title'][:52]}"
        )
        lines.append(f"        ci: {a['ci']} ({a['ci_detail']})")
        lines.append(f"        review: {a['review']} ({a['review_detail']})")
        if a["mergeable"] is False:
            lines.append("        mergeable: CONFLICT")
        lines.append(f"        → {a['next_action']}")
    if stale:
        lines.append("")
        lines.append(f"  {len(stale)} claim(s) with no open PR:")
        for claim in stale:
            lines.append(
                f"    {claim['task_id']} held {_hours(claim['age_hours'])} ({claim['ref']})"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit open PRs against the merge policy.")
    parser.add_argument("--prs", default="-", help="JSON array of PR records, or - for stdin")
    parser.add_argument("--claims", help="JSON array of held claim refs")
    parser.add_argument("--repo-root", type=Path, default=Path())
    parser.add_argument("--policy", choices=sorted(POLICIES), help="override arsenal/config.toml")
    parser.add_argument("--pr", type=int, help="narrow the report to one PR")
    parser.add_argument("--stale-hours", type=float, default=24.0)
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="exit 1 unless every reported PR is ready to merge",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        policy = args.policy or read_policy(args.repo_root)
        payload = _load(args.prs)
        claims = _records(_load(args.claims)) if args.claims else []
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    prs = _records(payload)
    if not isinstance(payload, list):
        print("error: --prs must hold a JSON array of PR records", file=sys.stderr)
        return 2
    if args.pr is not None:
        prs = [p for p in prs if p.get("number") == args.pr]
        if not prs:
            print(f"error: no PR #{args.pr} in the payload", file=sys.stderr)
            return 2

    audits = [audit_pr(pr, policy) for pr in prs]
    stale = stale_claims(claims, audits, args.stale_hours)

    if args.json:
        print(json.dumps({"policy": policy, "prs": audits, "stale_claims": stale}, indent=2))
    else:
        print(render(audits, stale, policy))

    if args.require_ready:
        # `never` is not a verdict about the PR: it is the host saying no agent
        # merges here. Reporting it as "not ready" would send a session off to
        # fix conditions that are already met.
        if policy == "never":
            return 3
        return 0 if all(a["ready"] for a in audits) else 1
    return 0


if __name__ == "__main__":
    # Windows consoles default to a legacy codepage (cp1252 and friends);
    # a non-ASCII line must degrade to "?", never take the process down.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
