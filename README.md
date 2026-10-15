# <img src="https://raw.githubusercontent.com/nuncaeslupus/doc-control/main/docs/logo.png" width="40" height="40" alt="" align="top"> doc-control

<!-- mcp-name: io.github.nuncaeslupus/doc-control -->

<!-- claude-arsenal:attribution: added by claude-arsenal's /init. Delete this block to drop it (it is not put back), or set `attribution = false` in the arsenal config.toml. -->
[![Built with claude-arsenal](https://img.shields.io/badge/built%20with-claude--arsenal-d97756)](https://github.com/nuncaeslupus/claude-arsenal)
<!-- /claude-arsenal:attribution -->

[![doc-control MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/nuncaeslupus/doc-control/badges/score.svg)](https://glama.ai/mcp/servers/nuncaeslupus/doc-control)

<p align="center">
  <img src="https://raw.githubusercontent.com/nuncaeslupus/doc-control/main/docs/hero.png" width="100%" alt="doc-control: an agent sends one JSON batch that replaces a word and adds a comment; the open Word page shows the replacement as a tracked deletion and insertion with Accept and Reject buttons, and the comment in a margin balloon">
</p>

> Status: **file engine working** (pre-1.0; the API may still change). The live engine
> drives LibreOffice Writer and Microsoft Word (Windows). See `status/specification.md` and `status/plan.md`.

A deterministic tool that lets AI agents work on Word documents quickly,
precisely and safely, ideally live in the Word window the writer has open.
Every content change is a tracked change the writer can accept or reject.

For anyone — writers and editors first — who wants an AI agent's help inside a
Word document without a single silent change.

## Usage

Install with `pip install doc-control` (or run it without installing: `uvx doc-control`).
Python 3.11+, no other dependencies. To drive the Word that is open on Windows, install
`doc-control[live]` (it adds pywin32); LibreOffice needs no extra package.

Command line, JSON in and JSON out:

```bash
doc-control doc.outline --json '{"doc": "Novela.docx"}'
doc-control text.find --json '{"doc": "Novela.docx", "query": "lluvia"}'
doc-control doc.batch --json '{"doc": "Novela.docx", "ops": [
  {"op": "text.replace", "at": "p12#a7b2a7b2:4-10", "text": "nieve"},
  {"op": "comment.add", "at": "p40#c0ffee00", "text": "¿Seguro?"}]}'
doc-control server.help doc.batch   # the JSON Schema of any operation
```

Every write is a tracked change by `DOC_CONTROL_AUTHOR` (default `Claude`), made after a
backup and verified, with `history.undo` and `history.*` to go back. Addresses like
`p12#a7b2a7b2` come from read results and fail loudly if the paragraph changed.
One edit can also go straight to its own operation (`text.replace`, `para.insert`,
`para.delete`, `comment.add`, `revision.accept`, `revision.reject`), which runs it as a
one-edit `doc.batch`.

From Python, a document can be held open for many reads, with whole results:

```python
from doc_control import api

with api.open("Novela.docx") as doc:
    for p in doc.paragraphs(normalized=True, pages=True):
        print(p["at"], p["style_name"], p["page"], p["text"][:40])
    chapters = doc.run("doc.segment", {"unit": "chapters"})["chapters"]
```

Recipes propose batches of fixes (Spanish typography, quotes, dashes, spacing, empty
paragraphs and more): `recipe.list`, then `recipe.run` returns a batch to review and apply.

### Notes, headers, footers and tables

Reads cover the body only unless asked; `stories` opts in to the rest (`"notes"` for
footnotes and endnotes, `"headers"` for headers and footers) on `para.list`,
`text.find`, `para.locate`, `para.get`, `doc.outline`, `doc.stats` and `revision.list`.
Their paragraphs carry an address with a prefix (`fn3/p1#0c9e0c9e`, `en2/…`, `hdr1.default/…`,
`ftr1.first/…`) that the edit operations accept, so a tracked edit inside a footnote or a
running header works like one in the body. A body read stays as fast as before.

```bash
doc-control para.list --json '{"doc": "Tesis.docx", "stories": ["body", "notes"]}'
doc-control text.replace --json '{"doc": "Tesis.docx", "at": "fn3/p1#0c9e0c9e:4-10", "text": "2019"}'
doc-control note.add --json '{"doc": "Tesis.docx", "at": "p40#c0ffee00:17", "kind": "footnote", "text": "Ver el cap. 2."}'
doc-control section.list --json '{"doc": "Tesis.docx"}'
doc-control table.get --json '{"doc": "Tesis.docx", "table": 1}'
doc-control table.row.insert --json '{"doc": "Tesis.docx", "table": 1, "after_row": 2, "cells": ["a", "b"]}'
doc-control table.row.delete --json '{"doc": "Tesis.docx", "table": 1, "row": 4}'
```

`note.add` inserts the reference mark and the note as tracked insertions (rejecting them
removes both); the row operations write tracked row insertions and deletions. All of it is
backed up, verified and undoable like any other write. `scripts/real_word_stories.py`
checks it in real Word (see [docs/real-word-check.md](docs/real-word-check.md)).

### Page layout: front matter, numbering, headers and fields

`layout.get` returns the document's page plan: per section where it starts (`from`), its
break, its page numbering and its header and footer text per kind (`default`, `first`,
`even`; `null` inherits the previous section's). `layout.apply` takes a plan in the same
shape and brings the document to it. This one gives a novel an unnumbered title page,
then starts chapter 1 on a right-hand page numbered 1, with the chapter title in the odd
headers and the book title in the even ones:

```bash
doc-control layout.get --json '{"doc": "Novela.docx"}'
doc-control layout.apply --json '{"doc": "Novela.docx", "untracked": true, "plan": {
  "version": 1, "two_sided": true, "mirror_margins": true,
  "sections": [
    {"from": "p1#ea7412aa", "break": "nextPage",
     "numbering": {"shown": false, "start": 1, "format": "lowerRoman"},
     "header": {"default": "", "even": ""}, "footer": {"default": "", "even": ""}},
    {"from": "p2#5fac7aad", "break": "oddPage",
     "numbering": {"shown": true, "start": 1, "format": "decimal"},
     "header": {"default": "{chapter}", "even": "La casa del río"},
     "footer": {"default": "{PAGE}", "even": "{PAGE}"}}]}}'
```

Each `from` is the address of the paragraph the section starts at (from `doc.outline` or
`para.list`). `{PAGE}`, `{NUMPAGES}` and `{chapter}` (the current Heading 1) become
fields. Keys left out stay as they are. Breaks and numbering go in as tracked changes;
a new header or footer part and the two-sided and mirror settings cannot be tracked in
Word, so the plan needs `untracked: true` (without it the answer names what needs it).
Run it with `"dry_run": true` first to see the changes; nothing is written unless the
plan read back from the result equals the request. A new section break adds a paragraph
to carry it, so the addresses after it renumber: read them again with `layout.get`.

Fields: `field.list` shows every field (TOC, PAGE, SEQ, REF, citations) and whether its
result is stale; `toc.insert`, `caption.add`, `xref.add`, `index.mark` and
`index.insert` add them as tracked insertions; `field.update` refreshes the TOC, page,
sequence and reference results through headless LibreOffice (`ENGINE_UNAVAILABLE`
without it; `set_update_on_open` asks Word to refresh them on open instead). An index is
built by Word (F9); citation fields are never changed. `scripts/real_word_layout.py` checks the
result in real Word (see [docs/real-word-check.md](docs/real-word-check.md)).

### Live: the document the writer has open

With LibreOffice Writer installed, the same front doors work on the open window: the
writer sees each comment and tracked change appear, and saves when they choose.

```bash
doc-control live.open --json '{"doc": "Novela.docx"}'
doc-control live.select --json '{"doc": "Novela.docx", "find_phrase": "lluvia fina"}'
doc-control doc.batch --json '{"doc": "Novela.docx", "engine": "live", "ops": [
  {"op": "comment.add", "at": "p40#c0ffee00", "text": "¿Seguro?"}]}'
doc-control live.close --json '{"doc": "Novela.docx", "save": true}'
```

Live `doc.batch` takes `comment.add`, `text.replace` and `para.insert`; anything else, or a
machine with no app to drive, answers `ENGINE_UNAVAILABLE` with the reason
(`live.status` lists the apps found). LibreOffice needs a Python its own bridge was
built for: the system Python with `python3-uno` for a distribution's package, the
Python version the snap bundles for the snap (`live.status` names it). Closing never
drops unsaved work: a changed document needs `save` or `discard`.

Operations are named `resource.action` (`doc.outline`, `text.find`, `para.list`); the
names used before 0.7.0 (`outline`, `find`, `paragraphs.list`…) still run but are no longer
listed ([renamed operations](docs/operations.md#renamed-operations)).

References: [operations](docs/operations.md) · [errors](docs/errors.md).

## Planned shape

- **One core, two engines, one API**
  - *File engine*: edits a closed `.docx` with the Python standard library only,
    rewriting only the parts it touches. Read operations also take a `.txt`, `.md` or
    `.odt` file (hard-wrapped text detected; Markdown `#` headings and OpenDocument
    headings become Heading styles); writes refuse it.
  - *Live engine*: controls the open document (LibreOffice Writer, and Word on
    Windows over COM; Mac later).
- **Two front doors**: a CLI with JSON in/out, and an MCP server
  (`doc-control mcp`, stdio) for Claude Desktop / Cowork and other MCP clients.
- **Safety first**: lock-file and on-disk change detection, backup before every
  write, verification, automatic restore, operation log with undo, and
  `unintended_changes == 0` as a gate.
- **No AI inside.** Judgment belongs to the agent; approval belongs to the writer.

It will replace `docx-fixer`, whose rules become packs built on this tool, and
serve `literary-critic` as its first downstream consumer.

## MCP server

`doc-control mcp` serves every operation as an MCP tool over stdio, one tool per
operation (`para.move` becomes `para_move`), with the same JSON Schemas and the
same response envelope as the CLI. It needs nothing beyond the package itself:

```json
{
  "mcpServers": {
    "doc-control": { "command": "uvx", "args": ["doc-control", "mcp"] }
  }
}
```

To drive the Word a writer has open on Windows, the server needs the `live` extra:
`"args": ["--from", "doc-control[live]", "doc-control", "mcp"]`.

## Documents

- [Operation reference](docs/operations.md) and [error reference](docs/errors.md), generated from the code
- [Initial research: tooling and risks](docs/research/01-tooling-and-risks.md)

## Development

This repo uses [claude-arsenal](https://github.com/nuncaeslupus/claude-arsenal)
(spec-driven workflow, gated tasks in `arsenal/tasks/`). See `CLAUDE.md`.

## Related projects

Same author:

- [claude-arsenal](https://github.com/nuncaeslupus/claude-arsenal) — a Claude Code skill and plugin marketplace: specs before code, failing tests first, gates with numbers.
- [literary-critic](https://github.com/nuncaeslupus/literary-critic) — lets an LLM index, analyse and advise on a full novel manuscript the way a professional editor would; doc-control's first planned consumer.
- [drawspec](https://github.com/nuncaeslupus/drawspec) — declarative diagram spec → clean, themeable SVG; JSON with no coordinates, so an LLM can write it.
- [integral-job-search](https://github.com/nuncaeslupus/integral-job-search) — a candidate-centred job search run as a conversation in Claude Code.

## License

[Apache-2.0](LICENSE)
