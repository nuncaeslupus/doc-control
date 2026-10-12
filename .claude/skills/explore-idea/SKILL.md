---
name: explore-idea
description: Shapes a vague idea one question at a time, with pros and cons and logged decisions, then hands off to specify. Use when the user has an idea but no stated problem yet. Not for a problem already stated (specify).
metadata:
  section: workflow
  type: workflow
---

# Explore-idea Workflow

Talk an idea into shape with an owner who may not be an expert, one decision at a time,
until the direction is agreed. The skill ends at the hand-off: it writes no spec, no plan
and no code. Per-task scratch goes in `tmp/`, never in `status/`.

CANARY: explore-idea-loaded-2026-09-29-fb78d23e-0a0a801de422d737

## When to load

Load when there is an idea but no problem statement yet — a new project, a "what if we…",
a choice the owner cannot frame. Use it rather than another plugin's brainstorming skill,
which tends to skip the annotated review `specify` requires. A stated problem goes to
`specify`; an approved spec goes to `design`.

## Steps

### Step 1: Size the request — out loud

Classify before exploring, and say the class in the reply so the owner can override it:

| Class | Looks like | Ends in |
|---|---|---|
| **spike** | a question to answer, a thing to try | a recommendation in chat |
| **bounded** | one component, a clear edge, no new architecture | a short design in chat and an explicit yes |
| **architectural** | new system, several components, lasting structural choices | hand-off to `specify` (Step 7) |

The class can move up mid-task, never down. Done when the reply names the class and the
owner has not overridden it.

### Step 2: Explore context before asking anything

Read what already answers questions: the repo (README, `AGENTS.md`, existing
`status/specification.md`), prior projects the owner mentions, and saved memory. Ask
nothing the context already settles. Done when the first question asked is one the
context could not answer.

### Step 3: Check scope early

When the idea spans independent subsystems — pieces that could ship, fail or be dropped
separately — split it into sub-projects before going deeper, name them, and ask which to
explore first. Explore one at a time. Done when exactly one sub-project is in play.

### Step 4: Ask one question at a time

Each question is multiple choice where the domain allows, and each option carries its
pros and cons in the owner's terms (cost, time, risk, what it rules out later), with a
recommendation and the reason for it:

```text
Q3 — Where should the data live?
  A) A spreadsheet the app reads    + nothing to run  − breaks past a few users
  B) A hosted database              + scales, backups − monthly cost, one more account
  C) Files in the repo              + versioned free  − no concurrent edits
  Recommended: B, because more than one person will edit at once (D-2).
```

Wait for the answer before asking the next question. Done when every answer is recorded
in the decisions log (Step 6).

### Step 5: Gate on research instead of guessing

When an answer needs outside evidence — market data, a vendor's current limits, what
comparable products do — stop and draft a research prompt the owner can run in a
deep-research tool: the question, why it matters to the decision, what a useful answer
looks like, and the sources to prefer. Log the decision as `open (research)` until the
findings come back. Done when the prompt is in the reply and the decision is marked open.

### Step 6: Keep the decisions log

Keep a running, numbered log in chat and reprint it whenever it changes:

```markdown
| ID  | Decision                    | Status          | Date       |
|-----|-----------------------------|-----------------|------------|
| D-1 | Web app first, mobile later | agreed          | 2026-09-29 |
| D-2 | Several editors at once     | agreed          | 2026-09-29 |
| D-3 | Hosting provider            | open (research) | 2026-09-29 |
```

Status is `agreed`, `open`, `open (research)` or `superseded by D-N`. Keep every row and
its number, so the spec can say why a superseded decision changed.
Done when the latest reply that changed a decision reprints the whole log.

### Step 7: Hand off behind an explicit approval

Summarise the agreed direction and the log, and ask for an explicit yes before anything
else happens — silence or "sounds good, carry on" about a single point is not approval of
the direction. Then:

- **spike** — give the recommendation; stop.
- **bounded** — give the short design; stop after the yes.
- **architectural** — route to the `specify` skill with the decisions log as input; it
  records the log under the spec's Recommendation section.

Done when the owner has said yes and the next skill (or none) is named.

## References — load on demand

- [Red flags](references/red-flags.md) — load when about to skip a step, downgrade the class, or hand off without a yes; it lists the rationalisations that lead there and the correct response to each.
