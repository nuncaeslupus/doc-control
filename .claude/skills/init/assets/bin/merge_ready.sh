#!/usr/bin/env bash
# merge_ready.sh <pr> — is this PR mergeable under the host's merge policy?
#
# Every session that merges used to answer this by writing its own `gh ... --jq`:
# the same three questions, spelled five different ways, each with its own bugs.
# The bugs were not random. They were the same two every time, because both are
# the shortcut the shell makes easy:
#
#   * reading "no failing checks" as green, when what the repo produced was NO
#     CHECKS AT ALL — a runner outage merges the whole queue;
#   * reading a review's summary state, when the finding list is the open
#     threads and a bot happily reports "review completed" over three of them.
#
# So the fetch lives here, once, and the verdict lives in `pr_audit.py`, once.
# This script is the half that knows how to ask GitHub; that script is the half
# that knows what the answers mean, and it is the same evaluator the fleet-wide
# audit runs, so one PR and forty PRs cannot disagree.
#
# CHECKS ARE FETCHED FOR THE HEAD SHA, not for the PR. A check run that reported
# on the previous push is evidence about the previous push. The distinction is
# invisible in `gh pr checks`, which is why it kept being lost.
#
# ORDER: this is the last question, not the first. On a new head, the second
# reader goes first; the full gate (`fast_gate.sh --full`) starts only once that
# reader returns CLEAR on the exact head, and this runs after the gate passes.
# A full gate started beside the review is wasted whenever the review BLOCKs
# (references/orchestrator-tick.md § Review before the full gate).
#
# Stdout: the condition table, then the verdict. With `--body`, and only when
# the PR is ready, the merge commit body to carry.
#
# Exit: 0 ready to merge, 1 not ready (the table says what is missing),
#       2 error, 3 merge-policy is `never` — report it and let a human merge,
#       5 no scriptable GitHub channel; the calls to make are printed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Git Bash: a native python.exe cannot open a `/c/...` path, and MSYS rewrites
# one it is handed into a wrong `C:\c\...`. Resolve to `C:/...` once, here, so
# every path built from SCRIPT_DIR below works for both shells. Identity elsewhere.
command -v cygpath >/dev/null 2>&1 && SCRIPT_DIR="$(cygpath -m "${SCRIPT_DIR}")"
# Boundary timing. Sourced, not run: it only defines functions, and does
# nothing at all under ARSENAL_METRICS=off. See bin/_timing.sh.
# shellcheck source=/dev/null
[[ -f "${SCRIPT_DIR}/_timing.sh" ]] && source "${SCRIPT_DIR}/_timing.sh"
CHANNEL="${SCRIPT_DIR}/github_channel.sh"
PR_AUDIT="${SCRIPT_DIR}/../scripts/pr_audit.py"

PR=""
WANT_BODY=0
WANT_JSON=0
for arg in "$@"; do
    case "${arg}" in
        --body) WANT_BODY=1 ;;
        --json) WANT_JSON=1 ;;
        -*) echo "usage: merge_ready.sh <pr> [--body] [--json]" >&2; exit 2 ;;
        *) PR="${arg}" ;;
    esac
done

_fail() { echo "error: $1" >&2; exit 2; }

[[ -n "${PR}" ]] || { echo "usage: merge_ready.sh <pr> [--body] [--json]" >&2; exit 2; }
[[ "${PR}" =~ ^[0-9]+$ ]] || _fail "pr must be a number, got '${PR}'"
[[ -f "${PR_AUDIT}" ]] || _fail "pr_audit.py not found at ${PR_AUDIT}"

# One call is one check of the gate, not the whole wait — the loop that polls
# this is what blocks, and the report divides by PR to show that.
command -v arsenal_timing_begin >/dev/null 2>&1 && {
    arsenal_timing_begin merge-ready "pr-${PR}"
    trap 'arsenal_timing_end $?' EXIT
}

slug="$(bash "${CHANNEL}" --slug)" || _fail "cannot determine owner/repo from the git remote"

api() {
    local out status
    out="$(bash "${CHANNEL}" --api GET "$1" 2>/dev/null)"
    status=$?
    case ${status} in
        0) printf '%s' "${out}" ;;
        5) return 5 ;;
        *) return 4 ;;
    esac
}

pr_json="$(api "/repos/${slug}/pulls/${PR}")"
case $? in
    5)
        # Not an error and not a skip. The step still happens, by the model's
        # own GitHub tools; printing the exact calls is what keeps a surface
        # without `gh` from quietly merging on no evidence.
        cat <<MANUAL
manual: no scriptable GitHub channel here. Make these calls with your GitHub
tools, assemble one JSON array of the record below, and pipe it in:

  GET /repos/${slug}/pulls/${PR}
  GET /repos/${slug}/commits/<head-sha>/check-runs
  GET /repos/${slug}/pulls/${PR}/reviews
  (thread resolution is GraphQL-only; omit \`threads\` if you cannot read it)

  [{"number": ${PR}, "title": …, "draft": …, "mergeable": …, "created_at": …,
    "head": {"sha": …, "ref": …},
    "checks": [{"name": …, "status": …, "conclusion": …, "head_sha": …}],
    "reviews": [{"state": …, "commit_id": …}],
    "threads": [{"id": …, "resolved": …}]}]

  python3 claude-arsenal/scripts/pr_audit.py --pr ${PR} --require-ready --prs -
MANUAL
        exit 5
        ;;
    4) _fail "cannot read PR ${PR} from ${slug}" ;;
esac

head_sha="$(printf '%s' "${pr_json}" | python3 -c \
    'import json,sys;print((json.load(sys.stdin).get("head") or {}).get("sha") or "")')"
[[ -n "${head_sha}" ]] || _fail "PR ${PR} has no head sha"

checks_json="$(api "/repos/${slug}/commits/${head_sha}/check-runs")" || checks_json=""
reviews_json="$(api "/repos/${slug}/pulls/${PR}/reviews")" || reviews_json=""

# Thread resolution is not in the REST API at all, so it is fetched only where
# GraphQL is reachable. `threads` is then ABSENT rather than empty, and pr_audit
# reports that it could not be read instead of counting zero open threads — an
# empty list is an answer, a missing one is not.
threads_json=""
if [[ "$(bash "${CHANNEL}" --detect 2>/dev/null)" == "gh" ]]; then
    threads_json="$(gh api graphql -f query='
      query($owner:String!,$name:String!,$pr:Int!){
        repository(owner:$owner,name:$name){
          pullRequest(number:$pr){
            reviewThreads(first:100){nodes{id isResolved}}}}}' \
      -F owner="${slug%%/*}" -F name="${slug##*/}" -F pr="${PR}" \
      --jq '[.data.repository.pullRequest.reviewThreads.nodes[]
             | {id: .id, resolved: .isResolved}]' 2>/dev/null)" || threads_json=""
fi

payload="$(PR_JSON="${pr_json}" CHECKS_JSON="${checks_json}" REVIEWS_JSON="${reviews_json}" \
    THREADS_JSON="${threads_json}" python3 - <<'PY'
import json, os, sys

def load(name, default):
    raw = os.environ.get(name) or ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default

pr = load("PR_JSON", {})
checks = load("CHECKS_JSON", {})
reviews = load("REVIEWS_JSON", [])
threads_raw = os.environ.get("THREADS_JSON") or ""

runs = checks.get("check_runs", []) if isinstance(checks, dict) else []
record = {
    "number": pr.get("number"),
    "title": pr.get("title") or "",
    "draft": bool(pr.get("draft")),
    "mergeable": pr.get("mergeable"),
    "created_at": pr.get("created_at"),
    "updated_at": pr.get("updated_at"),
    "head": {"sha": (pr.get("head") or {}).get("sha"), "ref": (pr.get("head") or {}).get("ref")},
    "checks": [
        {"name": r.get("name"), "status": r.get("status"),
         "conclusion": r.get("conclusion"), "head_sha": r.get("head_sha")}
        for r in runs if isinstance(r, dict)
    ],
    "reviews": [
        {"state": r.get("state"), "commit_id": r.get("commit_id"),
         "submitted_at": r.get("submitted_at")}
        for r in (reviews if isinstance(reviews, list) else []) if isinstance(r, dict)
    ],
}
# Only set `threads` when it was actually read. See the note above.
if threads_raw:
    try:
        record["threads"] = json.loads(threads_raw)
    except json.JSONDecodeError:
        pass
json.dump([record], sys.stdout)
PY
)" || _fail "cannot assemble the PR record"

audit_args=(--pr "${PR}" --require-ready --prs -)
[[ ${WANT_JSON} -eq 1 ]] && audit_args+=(--json)

printf '%s' "${payload}" | python3 "${PR_AUDIT}" "${audit_args[@]}"
verdict=$?

if [[ ${verdict} -eq 0 && ${WANT_BODY} -eq 1 ]]; then
    policy="$(python3 "${SCRIPT_DIR}/../scripts/arsenal_config.py" --get merge-policy 2>/dev/null \
        || echo after-ci)"
    cat <<BODY

--- merge commit body ---
Merged under merge-policy \`${policy}\`.

Head: ${head_sha}
Conditions checked against that head by claude-arsenal/bin/merge_ready.sh.
BODY
fi

exit ${verdict}
