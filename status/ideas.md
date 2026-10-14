# Ideas backlog

Captured 2026-10-14, overnight, from a gap survey: doc-control 0.6.0's operations compared
with what users of other docx tools and MCP servers ask for (python-docx, docxtpl and
GongRzhe Office-Word-MCP-Server issues; safe-docx, knorq-ai and SecurityRonin docx MCP
servers; legal-redline, translation and accessibility tooling). ★ marks the most-requested
features in that survey. Every row is `parked` until the owner picks one.

Already covered, so not listed here: tracked text edits, comments (add, reply, resolve,
filter), revision accept/reject, compare to a redline, diff, regex and accent-insensitive
find, styles and .dotx, recipes, hyperlink text in reads, undo and history, the live engine
on LibreOffice and on Word (Windows).

## Groups

### A. Content doc-control cannot reach yet
- ★ Footnotes and endnotes: read their text, find in them, tracked edits, add a note at an address (academics, lawyers, novelists' notes; python-docx #1)  (I-1)
- ★ Headers and footers: read them per section, make tracked edits in them (publishers, business; python-docx headers issue)  (I-2)
- Tables as tables: a row/column grid with cell addresses, insert or delete rows as tracked changes, and a header-row flag (reports, contracts; python-docx #174)  (I-3)
- Images: list them with size and alt text, and set alt text (needed for accessibility; python-docx #159, #981)  (I-4)
- ★ Lists: read the number as Word displays it ("3.2.a") and the list level, apply or restart numbering (contracts cite clause numbers; python-docx #25)  (I-5)
- Hyperlinks: add a link, or change its target, as a tracked change (python-docx #74, its top open issue)  (I-6)
- Content controls (`w:sdt`): read and fill them (forms, legal templates; python-docx #155)  (I-7)
- Equations read as linear text, so a find or a read does not skip them (academics; python-docx #320)  (I-8)

### B. Authoring and assembly
- Create a new .docx from Markdown or an outline, styled with a styleset (agents drafting from nothing)  (I-9)
- Fill a template: placeholders or content controls from JSON, with repeated blocks (legal ops, business; docxtpl)  (I-10)
- Split a document into chapters or files, and assemble files into one (publishers, literary-critic per-chapter work)  (I-11)
- Insert a page break, a section break, a table or an image at an address (I-12)

### C. Review workflows
- A revision report: who changed what, counted per chapter and per author, for an editor's summary (I-13)
- A review export: the document with its tracked changes and comments as HTML or Markdown, for readers without Word (the "review page" the spec leaves for later)  (I-14)
- ★ Prepare to send: remove personal information (revision and comment authors, rsids, document properties, hidden text) and report what was removed (lawyers, publishers)  (I-15)
- Restrict editing: read and set "tracked changes forced on" or read-only protection (legal, enterprise)  (I-16)
- Moved text in compare and diff: show a moved passage as a move, not a delete plus an insert (legal redlines)  (I-17)

### D. Output and interchange
- Export to Markdown or plain text, with headings, lists and footnotes kept (RAG, agents, literary-critic)  (I-18)
- Export to PDF through the installed LibreOffice (everyone; python-docx and GongRzhe issues)  (I-19)
- Translation round-trip: export segments with inline tags merged (XLIFF or a bilingual table), import the translation back into the same layout (translators; ProZ, Okapi #484)  (I-20)
- Write .odt as well as read it (LibreOffice-only writers)  (I-21)

### E. Checks the agent can act on (reports only, the judgment stays with the agent)
- Accessibility check: heading order, missing alt text, table header rows, document language (public sector, universities)  (I-22)
- Language tagging: read and set `w:lang` per paragraph or range, so Word's spellchecker uses the right language (multilingual manuscripts)  (I-23)
- Fields: list them (TOC, PAGE, REF, citations such as Zotero), and update the TOC and page references through LibreOffice (academics, publishers)  (I-24)
- Document properties: read and write title, author, keywords and custom properties (I-25)
- Structure check: validate the package (unique ids, paraIds, relationships) on any file, not only after our own writes (SecurityRonin's "paraId validation")  (I-26)

### F. Live engine
- Word on Mac (D-2 in the spec)  (I-27)
- More live operations: format, styles, and revision accept/reject in the open window (I-28)
- Live events: tell the agent when the writer edits, saves or moves the selection, instead of polling (I-29)
- Google Docs as a third engine (named out of scope in the spec)  (I-30)

### G. Distribution and reach
- A `.mcpb` bundle for one-click install in Claude Desktop (later spec)  (I-31)
- A Claude Skill for the chat code sandbox, using the file engine only (later spec)  (I-32)
- Position doc-control as the maintained alternative: GongRzhe's server (2.1k★) was archived in March 2026; the README could say how to move from it (ties to #89)  (I-33)

### H. Page layout and long-document structure (owner, 2026-10-14)
- Sections and page numbering as one controllable model: where numbering starts (none on the front matter, epigraph and dedication; page 1 at the first chapter), restarts, a part page counted with or without a visible number, number format (i, ii, … then 1, 2, …)  (I-34)
- Two-sided printing: mirror margins, different odd/even headers and footers, first page different, recto starts for chapters (blank versos inserted)  (I-35)
- Building front matter for consumers such as literary-critic: title page, epigraph or quotes page, dedication, each in its own section with the right header/footer and numbering  (I-36)
- Table of contents, index and lists of figures/tables with page references: insert, update, and read them back (with I-24's field update through LibreOffice)  (I-37)
- Captions and cross-references for tables, figures and equations ("see Table 3", "page 12"), kept in step when items move (theses, reports)  (I-38)
- Bibliography and citations as fields (Word's own sources, Zotero/Mendeley fields): read, keep intact on edit, refresh (theses)  (I-39)
- Text boxes and shapes: read and edit their paragraphs (skipped today)  (I-40)
- Table columns: insert/delete columns, merge cells, widths (rows came first in S-1)  (I-41)
- Live engine in notes and headers: read and edit footnotes and headers in the open window (S-1 is file engine only)  (I-42)

## Sub-projects

| ID  | Name                                   | Groups      | Status | Spec section |
|-----|----------------------------------------|-------------|--------|--------------|
| S-1 | Notes, headers, tables (I-1 to I-3)    | A           | specified | arsenal/project/STORIES/spec.md |
| S-2 | Lists, links, images (I-4 to I-8)      | A           | parked |              |
| S-3 | Authoring and assembly                 | B           | parked |              |
| S-4 | Review and send (reports, scrub, lock) | C           | parked |              |
| S-5 | Export and translation                 | D           | parked |              |
| S-6 | Checks                                 | E           | parked |              |
| S-7 | Live engine, next                      | F           | parked |              |
| S-8 | Packaging and reach                    | G           | parked |              |
| S-9 | Page layout and long documents         | H           | specified | arsenal/project/LAYOUT/spec.md |

Suggested order, for the owner to confirm: S-1 first, because the file engine is blind to
footnotes and headers today, which literary-critic and every other consumer hit. Then I-15
from S-4 (cheap, and it is what lawyers ask for). Then S-5's I-18, Markdown export, which
literary-critic and RAG use.

## Decision log

| ID  | Decision | Status | Date | Sub-project |
|-----|----------|--------|------|-------------|
| D-1 | Which sub-project to explore first: S-1 (notes, headers, tables) | agreed | 2026-10-14 | S-1 |
| D-2 | S-1 reads each section's page setup and header/footer kinds (first, even, default; numbering start and format) so S-9 can build on it; changing them is S-9 | agreed | 2026-10-14 | S-1 |
