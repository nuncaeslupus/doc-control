# The annotatable reader — a gate, not a final flourish

Loaded by `specify` and `design`, and by anything else that writes a spec or
plan. The rules for handing one over are the same whoever wrote it, so they live
here rather than in skill bodies that would drift apart on the next edit.

## Whichever skill wrote it

A brainstorming or planning skill from another plugin is not an exemption. In an
arsenal repo spec work goes through `specify` and plan work through `design`;
when another skill runs anyway, its output goes to `status/specification.md` /
`status/plan.md` (a workspace's `arsenal/project/<ws>/spec.md` / `plan.md`) and
gets a reader like any other. No plan is written before the annotated spec is
approved.

Two mechanisms make this hold without anyone remembering it:

- **A PostToolUse hook** (`claude-arsenal/bin/reader_hook.sh`, registered by
  `/init`) fires on every Write/Edit to `status/specification.md`,
  `status/plan.md`, `arsenal/project/*/{spec,plan}.md`, `docs/**/specs/*.md` or
  `docs/**/plans/*.md` and tells the session the reader is stale, with the
  command that regenerates it.
- **A gate** — `python3 claude-arsenal/scripts/reader_check.py branch` exits 1
  when any of those documents changed on the branch without a reader generated
  from its current content (the reader embeds a digest of what it rendered).
  `ship` treats that as No-Go; a host that wants it on every PR adds it to
  `host-gate` in `arsenal/config.toml`.

## Any document that specifies or plans work gets a reader

Not only `status/specification.md` and workspace specs, which are all that
`create_reader.py` auto-discovers. A design document under `docs/design/`, an
RFC, a proposal written straight into a docs tree: same rule, named explicitly
since discovery will not find it, and with `--output-dir` pointed beside it.

```bash
create_reader.py --input docs/design/0007-thing.md --output-dir docs/design \
                 --name "Thing"
```

Publishing it some other way — a chat summary, a hand-built page, a link to the
raw file — does not satisfy this. The reader exists so notes attach to the
section they are about; a substitute that drops that property is not a
substitute.

A reader is named for what it is — `<repo>-<doc>-v<N>-<YYYY-MM-DD-HHMM>-reader.html`, with
`…-annotated.md` beside it (`yourproject-spec-v1-2026-10-12-1735-reader.html`): the git
repo's name, `spec` / `plan` (or the stem of any other document, so design docs
sharing a directory never overwrite each other's readers), the `**Revision**`
(1 without one) and the minute it was built, so two documents reviewed the
same day never share a name. A rebuild removes the earlier readers
of that document. Do not rename the output — `reader_check.py` looks for that
name; `--output FILE` is the explicit override when a name is required.

## Work that consumes the document waits for the annotations

Handing over the reader is where this step ends and the next one has not
started: do not open a pull request to merge the spec or plan, do not begin
`design` off a spec or `execution` off a plan, until the reviewer's export has
come back and been read.

A document reviewed only after it has been merged, or after the work it
describes is underway, has been ratified rather than reviewed. When the reviewer
says to proceed without annotating, that is their call and the work starts — but
it is their call to make, not an assumption to act on while waiting.

## The returned export is review history

The export is named like the reader, `<repo>-<doc>-v<N>-<YYYY-MM-DD-HHMM>-notes.md`, N
being the document's `**Revision**` header line and the stamp the export's. When one
arrives — a path in `~/Downloads`, an upload, a paste — move it into the
document's directory beside the reader. Applying it makes the next revision: bump `**Revision**`, add a
`**Revision log**` line naming the export, regenerate the reader, and commit the
export **in the same commit** as that revision. Left in Downloads it is gone by
the next session; `query_status.py` names any export of this project's still
sitting in a Downloads folder (`~/Downloads`, `~/Descargas`, the XDG download
dir, …) that the repo does not track.

Approval is recorded in the header, not in chat:
`**Status**: approved (<date>, revision N)`, backed by a committed notes file
named for that revision or an explicit `— without annotations`.
`validate_spec.py` / `validate_plan.py` fail on a named notes file that is not
committed, and with `--require-approved` on a missing or unbacked approval —
`design` runs that on the spec before planning, and seeding or `execution` runs
it on the plan.

To re-seed a rebuilt reader with previous notes, pass `--notes <the returned
file>`; the export carries its own note data, so hand back the file the reviewer
sent, unrenamed.
