# Real-Word check (T30)

Checks that what doc-control writes opens cleanly in Microsoft Word and behaves like
Word's own tracked changes and comments. About 20 minutes. You need Windows with Word
2016 or later (repeat with Microsoft 365 if you have both), and `uv` and `git`.

The gate is `real_word_repair_prompts == 0` (step 2) and
`real_word_roundtrip_mismatches == 0` (step 6).

## 1. Build the test documents

If `git` or `uv` is missing, install them once in PowerShell, then close and reopen
PowerShell:

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
```

Then:

```powershell
git clone https://github.com/nuncaeslupus/doc-control
cd doc-control
uv run python scripts/real_word_check.py build C:\t30
```

`C:\t30` now holds 12 documents and `expected.json`. Use a new folder for each Word version.

## Steps 2 to 5 by script

`scripts/real_word_auto.ps1` does steps 2 to 5 in Word for you:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\real_word_auto.ps1 C:\t30
```

It opens each document in a Word window of its own and counts the ones that raise a
dialog instead of opening; before that it opens a deliberately broken copy, which must
raise a dialog, so a count of zero means something. Then it accepts all, rejects all,
replies and resolves through Word's automation interface, saves the three files step 6
reads, and prints `real_word_repair_prompts == <n>`. Other Word windows you have open are
left alone. Continue at step 6. What the script cannot judge is still yours to check by
eye: that each document looks normal (step 2) and how the tracked changes show (step 3).

## 2. Open every document

Open each `.docx` in `C:\t30` in Word, one by one, and note for each:

- Did Word show **"Word found unreadable content"** or offer to repair it? (Must be no.)
- Does it look like a normal document (headings, tables, footnotes, links where expected)?

Write down the name of any file that asked to repair. Close each one **without saving**.

## 3. Tracked edits: accept all

1. Open `edits.docx`. In **Review**, set the display to **All Markup** (in Word 2016:
   **Review → Display for Review → All Markup**).
2. Check that you see, marked as tracked changes by "doc-control T30":
   - a first word replaced by `REPLACED`;
   - a few characters deleted inside a paragraph;
   - an inserted paragraph "An inserted paragraph from doc-control.";
   - a deleted paragraph;
   - a moved paragraph: doc-control writes a move as the paragraph deleted in one place
     and the same text inserted in another (not Word's green "moved" markup);
   - a paragraph changed to Heading 1, and one made bold italic (formatting changes).
3. **Review → Accept → Accept All Changes**.
4. **File → Save As** `C:\t30\edits-accepted.docx` (Word Document format). Close.

## 4. Tracked edits: reject all

1. Open `edits.docx` again.
2. **Review → Reject → Reject All Changes**.
3. **Save As** `C:\t30\edits-rejected.docx`. Close.

## 5. Comments: reply and resolve

1. Open `commented.docx`. Two comments from "doc-control T30" should be visible.
2. On the comment that starts **"First"**, click **Reply** and type any text.
3. On the comment that starts **"Second"**, click **Resolve** (in Word 2016: right-click
   the comment → **Mark Comment Done**).
4. **Save As** `C:\t30\commented-word.docx`. Close.

## 6. Verify

```powershell
uv run python scripts/real_word_check.py verify C:\t30
```

It reads back what Word saved and prints every mismatch, then
`real_word_roundtrip_mismatches == <n>`.

## 7. Report

Send back, per Word version (e.g. "Word 2019, build …" from **File → Account**):

- the files that asked to repair in step 2 (or "none");
- anything in step 3 that did not show as described;
- the last line printed by step 6 and any `MISMATCH` lines.

## Stories: notes, headers and table rows

A second, shorter check for edits outside the body: a tracked footnote and endnote
(`note.add`), a tracked edit inside a footnote and inside a header, and a tracked row
inserted into and deleted from a table. Same setup as above (Windows, Word 2016 or later,
`uv`, `git`); about 10 minutes. The gate is `real_word_stories_mismatches == 0`.

```powershell
uv run python scripts/real_word_stories.py build C:\stories
```

`C:\stories` now holds `notes.docx`, `headers.docx`, `tables.docx` and `expected.json`.
For each of the three documents:

1. Open it in Word. Did Word offer to repair it? (Must be no; close without saving if so
   and write the name down.)
2. Check the markup, with **All Markup** on, as tracked changes by "doc-control STORIES":
   - `notes.docx`: the first footnote's first word changed to `EDITED`; a new footnote and
     a new endnote, each with its reference mark in the body (look in the **Reviewing
     Pane** if the note area is hidden);
   - `headers.docx`: a header's first word changed to `EDITED` (double-click the page top
     to see it);
   - `tables.docx`: a new row of `Inserted row` cells after the first row, and the last row
     struck through as deleted.
3. **Review → Accept → Accept All Changes** and **Save As** `<name>-accepted.docx` in
   `C:\stories` (for example `notes-accepted.docx`). Close.
4. Open the original again, **Reject All Changes**, and **Save As** `<name>-rejected.docx`.
   Close.

Then:

```powershell
uv run python scripts/real_word_stories.py verify C:\stories
```

It reads back the six files, checks that no tracked change is left and that every
paragraph of every story (body, notes, headers and footers) is what accepting or rejecting
should leave, prints each `MISMATCH`, then `real_word_stories_mismatches == <n>`. Report
it, and the names of any file that asked to repair, per Word version as in step 7.

## Layout: sections, page numbers, headers and a TOC

A third check, for `layout.apply` and `toc.insert`: the page plans of the LAYOUT spec, each
applied to the corpus novel (a title, then 40 chapters, with a footer printing the page
number). Same setup as above (Windows, Word 2016 or later, `uv`, `git`); about 20 minutes.
The gate is `real_word_layout_mismatches == 0`, with no repair prompt and every page
number as described below.

```powershell
uv run python scripts/real_word_layout.py build C:\layout
```

`C:\layout` now holds the documents below and `expected.json`; `build` prints what to
look for in each. `toc.docx` is built only when LibreOffice is installed (its page
numbers come from `field.update`); without it, the script says so and leaves it out.

| Document | What Word must show |
|---|---|
| `front_matter.docx` | the title page has no number; the first page of Capítulo 1 shows 1 |
| `roman_then_arabic.docx` | i, ii, iii, … up to Capítulo 2, which starts again at 1 |
| `part_page.docx` | Capítulo 2's heading alone on a page with no number; the next page shows the count, two or more past the page before the heading |
| `restart_per_part.docx` | the count starts at 1 on the title, again at Capítulo 3 and at Capítulo 5 |
| `recto_chapters.docx` | Capítulo 2 and Capítulo 3 start on odd page numbers (Word leaves a blank even page before them when needed) |
| `two_sided.docx` | the README's plan: odd pages show the chapter title in the header, even pages "La casa del río", the title page neither; mirrored margins (**View → Multiple Pages**) |
| `toc.docx` | a "Contents" list after the title, every chapter with a page number |

For each document:

1. Open it in Word. Did Word offer to repair it? (Must be no; write the name down.)
2. With **All Markup** on, the section breaks and page-number changes show as tracked
   formatting changes by "doc-control LAYOUT". Check the pages against the table (the
   number is in the footer; double-click it if the footer is hidden).
3. **Review → Accept → Accept All Changes**. For `toc.docx` only, then right-click the
   table of contents → **Update Field** → **Update entire table**: the page numbers must
   stay the same.
4. **Save As** `<name>-accepted.docx` in `C:\layout` (for example
   `front_matter-accepted.docx`). Close.

`roman_then_arabic.docx` is reached with tracked changes alone, so rejecting them must
give the original back: open it again, **Reject All Changes**, check that the pages show
1, 2, 3, … throughout, and **Save As** `roman_then_arabic-rejected.docx`. Close.

Then:

```powershell
uv run python scripts/real_word_layout.py verify C:\layout
```

It reads back each saved file, checks that no tracked change is left, that `layout.get`
reads the planned sections, breaks, numbering and header and footer text (and the
original plan for the rejected file), and that Word's updated TOC equals the one
doc-control wrote. It prints each `MISMATCH`, then `real_word_layout_mismatches == <n>`.
Report it, the names of any file that asked to repair and any page that did not show as
the table says, per Word version as in step 7.

## Find: the `--` dialogue case (FIND)

The owner's case that started the FIND workspace: a dialogue line typed with `--` (two
hyphens) where an em dash (`—`) or a hyphen (`-`) was meant. For matching, a `--` at the
start of, or inside, a dialogue line equals `—` or `-`: all three are punctuation and fold
to a word boundary at `match_level: "default"` (the default for `live.select` and
`anchor.resolve`), so a phrase sent with any of them, or with no dash at all, finds the
line. Judged, not measured; about 2 minutes.

1. In Word, make a document holding the paragraph `--¿Estás ahí? --dijo ella.`, save it
   as `C:\t30\dialogue.docx` and keep it open. Attach to it:

   ```powershell
   uv run doc-control live.open --json '{"doc": "C:\\t30\\dialogue.docx"}'
   ```

2. Each of these must select that paragraph and answer a `match_level` other than
   `exact`, with `matched_text`:

   ```powershell
   uv run doc-control live.select --json '{"doc": "dialogue.docx", "find_phrase": "Estas ahi"}'
   uv run doc-control live.select --json '{"doc": "dialogue.docx", "find_phrase": "—¿Estás ahí? —dijo ella"}'
   uv run doc-control live.select --json '{"doc": "dialogue.docx", "find_phrase": "-Estas ahi -dijo"}'
   ```

3. With `"match_level": "exact"` added, only a phrase typed with `--`, as in the
   document (`--¿Estás ahí? --dijo`), is found; the `—` and `-` forms answer `ANCHOR_NOT_FOUND`.

Report any call that picked another paragraph or found nothing, per Word version.
