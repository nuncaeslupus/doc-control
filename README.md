# word-control

> Working name. Status: **exploration / pre-spec** — no code yet.

A deterministic tool that lets AI agents work on Word documents quickly,
precisely and safely, ideally live in the Word window the writer has open.
Every content change is a tracked change the writer can accept or reject.

## Planned shape

- **One core, two engines, one API**
  - *File engine*: edits a closed `.docx` with the Python standard library only,
    rewriting only the parts it touches.
  - *Live engine*: controls the open Word (Windows/COM first, Mac later).
- **Two front doors**: a CLI with JSON in/out, and an MCP server for Claude
  Desktop / Cowork.
- **Safety first**: lock-file and on-disk change detection, backup before every
  write, verification, automatic restore, operation log with undo, and
  `unintended_changes == 0` as a gate.
- **No AI inside.** Judgment belongs to the agent; approval belongs to the writer.

It will replace `docx-fixer`, whose rules become packs built on this tool, and
serve `literary-critic` as its first downstream consumer.

## Documents

- [Initial research: tooling and risks](docs/research/01-tooling-and-risks.md)

## Development

This repo uses [claude-arsenal](https://github.com/nuncaeslupus/claude-arsenal)
(spec-driven workflow, gated tasks in `arsenal/tasks/`). See `CLAUDE.md`.

## License

[Apache-2.0](LICENSE)
