# doc-control

<p align="center">
  <img src="docs/social-preview.png" width="100%" alt="doc-control: let AI agents edit Word documents safely. Deterministic .docx edits, every change a tracked change the writer can reject.">
</p>

> Status: **spec and plan approved** — implementation starting. See `status/specification.md` and `status/plan.md`.

A deterministic tool that lets AI agents work on Word documents quickly,
precisely and safely, ideally live in the Word window the writer has open.
Every content change is a tracked change the writer can accept or reject.

For anyone — writers and editors first — who wants an AI agent's help inside a
Word document without a single silent change. Nothing to install yet; watch the
repo for the first release.

## Planned shape

- **One core, two engines, one API**
  - *File engine*: edits a closed `.docx` with the Python standard library only,
    rewriting only the parts it touches.
  - *Live engine*: controls the open Word (Windows/COM first, Mac later).
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

## Documents

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
