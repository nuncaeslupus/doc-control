# LAYOUT: Session Handover

<!-- Written at session end. A new session reading this file can resume
     without additional context. -->

## Last task

- **ID**: <!-- e.g. lo-a3f8 -->
- **Title**: <!-- task title -->
- **Status at handover**: <!-- open | in_progress | done | blocked -->

## What was done this session

<!-- One-paragraph summary. Include commit SHAs if relevant. -->

## What remains

<!-- Bulleted list of sub-tasks or acceptance-criteria items not yet met. -->

## How to continue

1. Read `claude-arsenal/references/worker-loop.md` for the worker loop algorithm.
2. Fetch the `arsenal:task` issues, then run
   `claude-arsenal/scripts/task_select.py --issues <file>` for the next task.
3. Claim it with `claude-arsenal/bin/claim_task.sh <task_id>`; `lost` means
   another session has it, so take the next one.
