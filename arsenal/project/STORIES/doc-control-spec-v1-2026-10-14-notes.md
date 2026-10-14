# Specification notes

_Exported 2026-10-14 · 2 notes._

> Send this file back to continue the review. Notes are embedded as data at the bottom
> so importing this file restores them in the reader.

## Spec

**§1 Problem statement**  `SPEC §1`
> I think you are talking about headers and footers in general, but one very important thing are page numbers and understanding sections, renumbering, etc. This is usually very complex in Word to manage. If there was an automated way to treat them, it would be great! For example, tools like literary-critic could create a front page, some quotes page, etc. and not start numbering pages until the text begins and choose if part page is page 1 with or without page number, if pages are to be printed both sides, how headers and footers must be for each left or right page... All this goes probably to user tools, but doc-control should be able to manage this.
> I'm also thinking about indexes, summaries, table of contents with page references... We need to be able to work with this kind of things on other tools.
> Novels are simpler, but thesis and complex documents need references, tables, drawings...

**§4 Recommendation**  `SPEC §4`
> Option B.
> 1. Stored id.
> 2. New footnote in scope.
> 3. Yes, out of it for now, but take note.
> 4. Only rows for now, but keep it to be thought later.
> 5. Idea for the backlog too.

<!-- SPEC-NOTES-DATA
{"s-spec-1":"I think you are talking about headers and footers in general, but one very important thing are page numbers and understanding sections, renumbering, etc. This is usually very complex in Word to manage. If there was an automated way to treat them, it would be great! For example, tools like literary-critic could create a front page, some quotes page, etc. and not start numbering pages until the text begins and choose if part page is page 1 with or without page number, if pages are to be printed both sides, how headers and footers must be for each left or right page... All this goes probably to user tools, but doc-control should be able to manage this.\nI'm also thinking about indexes, summaries, table of contents with page references... We need to be able to work with this kind of things on other tools.\nNovels are simpler, but thesis and complex documents need references, tables, drawings...","s-spec-4":"Option B.\n1. Stored id.\n2. New footnote in scope.\n3. Yes, out of it for now, but take note.\n4. Only rows for now, but keep it to be thought later.\n5. Idea for the backlog too."}
-->