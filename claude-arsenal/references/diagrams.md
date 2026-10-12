# Diagrams in specs, plans and docs — drawspec, not hand-drawn

Loaded when a spec, plan or design document needs a picture. `specify` and
`design` point here; so does anything else that writes one.

## Write what the diagram means, not where things go

A diagram is a [drawspec](https://github.com/nuncaeslupus/drawspec) document:
JSON naming the nodes, edges, labels and a semantic role for each. drawspec
computes every coordinate and renders SVG that takes its colour from the page, so
it reads in dark mode and in greyscale print.

Not hand-written SVG, and not ASCII art. Both ask for coordinates an agent cannot
check without seeing the output: boxes overlap, arrows miss, labels run off the
edge, and nothing fails. drawspec refuses a coordinate by name and reports every
violation with the JSON pointer of where it was written, so a broken diagram is
an error message instead of a picture that looks wrong.

The format — the kinds (`flow`, `tree`, `timeline`, `matrix`, …), the roles, the
one rule — is drawspec's own
[AGENTS.md](https://github.com/nuncaeslupus/drawspec/blob/main/AGENTS.md). Read it
whole before the first diagram; `drawspec kinds` and `drawspec example <kind>`
print the short version.

## Where it goes: a fenced block in the Markdown

The JSON source lives in the document, in a fence tagged `drawspec`:

````markdown
```drawspec
{"version": 1, "kind": "flow",
 "nodes": [{"id": "in", "text": "Request", "role": "start"},
           {"id": "ok", "text": "Authorised?", "role": "decision"}],
 "edges": [{"from": "in", "to": "ok"}]}
```
````

The source is what gets reviewed and diffed; nothing rendered is committed
beside it. `create_reader.py` draws it: each fence is validated, then rendered
to inline SVG in the HTML reader, and the annotated Markdown keeps the JSON. A
diagram drawspec refuses fails the reader run with the block named and drawspec's
own message, and nothing is written — fix the JSON, regenerate.

Check one while writing it:

```bash
drawspec validate diagram.json     # every violation, with its JSON pointer
```

## Running drawspec

It is not on PyPI. `create_reader.py` uses, in order: the command in
`ARSENAL_DRAWSPEC` (e.g. `ARSENAL_DRAWSPEC="uvx --from /path/to/drawspec
drawspec"`), `drawspec` on `PATH`, then
`uvx --from git+https://github.com/nuncaeslupus/drawspec drawspec`. With none of
those available, a document containing a `drawspec` fence fails with that hint
rather than rendering without its picture. A document with no fence never runs
drawspec, so it adds no dependency to any reader that does not draw.
