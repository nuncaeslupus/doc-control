---
id: t-954cacd8
title: "List the MCP server on Glama and awesome-mcp-servers, once the tool is mature"
label: "glama and awesome-mcp listing"
priority: 1
deps: [t-00827bbf]
requires: [access:human]
tags: [DOCS]
---

# List the MCP server on Glama and awesome-mcp-servers, once the tool is mature

Deferred on purpose (owner, 2026-10-13): the tool is not mature enough to list yet.
Waits on T29 (docs and coverage), the last v1 task that needs no Windows machine.

## What to do

1. Confirm with the owner that the tool is ready to be listed.
2. Check `glama.json` at the repo root against Glama's current docs (its `$schema` URL
   was written from memory), then add and claim the server on Glama with the owner's
   account.
3. Open a PR to `punkpeye/awesome-mcp-servers` adding doc-control, following that
   repo's contribution rules (it asks for the Glama listing first).

The server is `doc-control mcp` (stdio, `src/doc_control/mcp.py`); the client config is
in the README's "MCP server" section.

## Acceptance gate

```bash
curl -fsSL -o /dev/null https://glama.ai/mcp/servers/@nuncaeslupus/doc-control
```

## Location
manual (Glama account, a PR in another repository)
