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
