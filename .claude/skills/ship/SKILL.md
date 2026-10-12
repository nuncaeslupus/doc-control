---
name: ship
description: Confirms a change is production-ready — compatibility, tests, observability, rollback. Use when the user wants release sign-off. Not for implementation (execution) or PR review (review).
metadata:
  section: workflow
  type: workflow
---

# Ship Workflow

CANARY: ship-loaded-2026-06-15-3b7e91c2d84fa056

Reads `status/specification.md` to know what should be shipping. Confirms scope coverage, compatibility, tests, observability, and rollback before the merge.

## Steps

### Step 1: Confirm final scope

- Intended vs actual scope. Any drift? Any missing pieces?
- If drift → decide: acceptable or split into separate PR?
- If scope grew significantly → does the risk assessment need updating?
- If the README carries a hero image, does it still match what ships? Load `references/readme-hero.md` when it might not, or when drawing one.

### Step 2: Confirm objective coverage

- Does the change solve the stated problem?
- All acceptance criteria satisfied?
- Every task's **Gate** is recorded and met:

  ```bash
  python3 "${CLAUDE_SKILL_DIR}/../gate-check/scripts/run_gate.py" --input status/plan.md
  ```

  Exit 1 is No-Go. Exit 0 still leaves each manual (`?`) gate to a human verdict; one not yet confirmed is No-Go. Exit 2 (no Gate column or usage error): confirm the plan path, then fall back to the acceptance criteria only for a plan that predates gates.
- Every spec or plan the branch changed has a reader built from its current text (exit 1 is No-Go: regenerate the reader it names, hand the HTML over, and commit it with the document):

  ```bash
  python3 "${CLAUDE_SKILL_DIR}/../init/assets/scripts/reader_check.py" branch
  ```
- If partial delivery → is the partial state safe and functional?

### Step 3: Compatibility check

- [ ] Backwards compatible with previous API version (if API changes)
- [ ] Database migration is forward-compatible (no destructive changes in same deploy)
- [ ] Inter-service contracts maintained or migrated
- [ ] Client notification sent (if public API changes)

### Step 4: Test confirmation

- [ ] All unit/integration tests pass (including the new tests written for this change). A gate receipt for this tree or green CI on the PR head is the evidence; run the full suite only when neither covers the tree.
- [ ] E2E tests pass (if applicable)
- [ ] Manual testing completed for high-risk paths
- [ ] No flaky tests introduced

### Step 5: Observability check

- [ ] New endpoints have tracing instrumentation
- [ ] Error conditions produce meaningful log entries
- [ ] Business metrics updated (if applicable)
- [ ] Alerts configured for new failure modes (if applicable)

### Step 6: Deployment plan

- [ ] Deployment order defined (if multi-service)
- [ ] Feature flags configured (if gradual rollout)
- [ ] Data migration tested (if applicable)
- [ ] Rollback plan documented
- [ ] On-call team aware (if high-risk)

### Step 7: Adversarial reviewer gate

Reuse the verdict on record when it covers the merge-ready tree; run a new round
only if commits landed after the last reviewed tree. Protocol and exits:
`claude-arsenal:core:init § references/pre-pr-review.md`.

- **CLEAR** (reused or new) → record it in the ship output (§ 3 Adversarial
  review row) and proceed to Step 8.
- **BLOCK** → show the findings verbatim and stop. A finding judged a false
  positive may be overridden by recording which, why, and what was checked in
  the same row; once every blocking finding is overridden this way, the BLOCK is
  cleared: proceed to Step 8. Otherwise resolve it and re-run from Step 1.
- **No verdict or a moved tree** → re-run.

### Step 8: Produce ship output

Load `references/template.md` when producing the ship output document.

---

## Abbreviation

**Abbreviated ship** = Steps 2 + 4 + 7 + Go/No-Go, where the host repo's
`CLAUDE.md` allows it. Step 7 may be skipped only when that file carries
`<!-- ship: adversarial-review=skip -->` and the change is docs-only or
config-only; every code change runs it.
