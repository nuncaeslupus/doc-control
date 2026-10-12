---
name: design
description: Defines the technical solution after discovery — contracts, task split, risks, sequencing (validate_plan). Use when the user is planning how to build scoped work. Not for investigation (specify), implementation (execution) or PR review (review).
metadata:
  section: workflow
  type: workflow
---

# Design Workflow

CANARY: design-loaded-2026-05-19-e08675ccb0a5c932

Owns sections 5–6 of `status/specification.md` (contracts, risks) and creates `status/plan.md` (task split). Reads sections 1–4 of `status/specification.md` to understand what the spec already covers.

**Before Step 1 — the spec must be approved.** The `specify` skill's validator answers it:

```bash
python3 "${CLAUDE_SKILL_DIR}/../specify/scripts/validate_spec.py" \
    --input status/specification.md --require-approved
```

Anything but exit 0 means stop and hand the spec back for approval, because a plan
written before approval is built on a draft.

## Steps

### Step 1: Define the technical solution

Translate the chosen option into a concrete technical design.

- **Architecture overview**: how the change fits into the existing system
- **Data flow**: how data moves through affected services (request/response, events, jobs)
- **State changes**: what data is created, updated, or deleted and where
- **Technology choices**: any new libraries, tools, or patterns (justify each)
- **What is NOT changing**: explicit boundaries to prevent scope creep

### Step 2: Define contracts

For every interaction between components:

- **API contracts**: request/response schemas, status codes, error formats
- **Event contracts**: message schemas, routing keys, retry policies
- **Database changes**: schema modifications, migration approach (always forward-compatible)
- **Configuration**: new env vars, feature flags, deployment parameters

Use concrete examples (JSON payloads, SQL migrations, config snippets).

### Step 3: Split into tasks

Break the implementation into ordered, independently testable tasks.

For each task:
- **What**: specific deliverable
- **Where**: which files/services
- **Dependencies**: what must be done first
- **Gate**: `<metric> <op> <threshold>` derived from the spec's success criteria (`p95_latency_ms <= 200`, `line_coverage >= 0.90`), in the `gate-check` grammar so it can be checked mechanically; non-numeric only when no number fits.
- **Tests**: file path(s) and `test_<what>_<condition>_<expected_result>` names, each with a one-sentence assertion; they are copied into the task payload so the worker writes them failing first.
- **Estimated effort**: Small (< 1h) / Medium (1-4h) / Large (4h+)

Keep each task small enough for one commit. `execution` records each gate's evidence in the plan's **Evidence log**, which `review` and `ship` audit.

### Step 4: Anticipate risks

For each risk:
- **What could go wrong**: specific failure scenario
- **Likelihood**: Low / Medium / High
- **Impact**: Low / Medium / High
- **Mitigation**: what to do to prevent or handle it
- **Rollback plan**: how to undo if it goes wrong

Pay special attention to:
- Backwards compatibility (API consumers, data formats)
- Data integrity during migration
- Performance under production load
- Deployment ordering (if multi-service)

### Step 5: Produce design output

Load `references/template-specification-tail.md` when appending contracts and risks (sections 5–6) to `status/specification.md`. Load `references/template-plan.md` when creating `status/plan.md`.

After writing the files, confirm the plan's structure:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/validate_plan.py" --input status/plan.md
```

It checks shape only: the required sections and task-table columns, Gate included. `gate-check`'s `run_gate.py --strict` audits the gate values.

Draw any picture as a fenced `drawspec` block: `claude-arsenal:core:init § references/diagrams.md`.

### Step 6: Publish the annotatable plan

Once the validator passes, generate the reader and give the user the HTML in the same
reply; the plan is what they sign off on, section by section. Name the plan
explicitly, since auto-discovery finds only specs, and send `--output-dir` with it
(`arsenal/project/<WORKSPACE>/` for a workspace plan):

```bash
create_reader.py --input status/plan.md --output-dir status   # in claude-arsenal/scripts/; uv run --with markdown
```

It writes `plan-reader.html` and `plan-annotated.md` and prints both paths. The plan
keeps the spec's review record (`**Revision**`, `**Status**`, `**Revision log**`);
seeding tasks or starting `execution` waits for
`validate_plan.py --input <plan> --require-approved`. Load
`claude-arsenal:core:init § references/annotatable-reader.md` when a returned notes
export arrives or when recording approval.

---

## Abbreviation

**Abbreviated design** = Step 1 (one paragraph) + Step 3 (task list only), where the host repo's `CLAUDE.md` allows it.

## Workspace-aware paths

When `arsenal/project/<WORKSPACE>/` exists, write the plan to `arsenal/project/<WORKSPACE>/plan.md` (and the contracts/risks tail to the workspace's `spec.md`) instead of `status/plan.md`. Otherwise use `status/` as above. The validator takes the path via `--input`; point it at whichever file was written.
