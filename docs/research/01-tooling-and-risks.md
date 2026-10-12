# Initial research: tooling and risks

Status: research input for the spec, gathered 2026-10-12. Not decisions.
Points marked "unverified" or [GK] (general knowledge, not confirmed by a source) need a test on real Word before the spec relies on them.

## 0. Summary: what this means for the decisions

| Topic | Finding | Suggested direction |
|---|---|---|
| File engine XML | `xml.etree` silently drops namespace declarations listed only in `mc:Ignorable` → Word reports unreadable content (reproduced on Python 3.13). | Read with ElementTree; write by splicing changed paragraphs into the original part bytes. Copy untouched zip members in their original order. |
| File engine speed | Synthetic 100k-word parse + replace + zip ≈ 0.4 s in pure Python. | Stdlib-only file engine is feasible. |
| Tracked changes | `w:ins`/`w:del`/`w:delText`, unique `w:id`, author+date, never across field/hyperlink boundaries. Moves are complex. | v1: moves = delete + insert. |
| Comments | Up to 5 parts (comments, commentsExtended, commentsIds, commentsExtensible, people). Replies/resolved since Word 2013. | Core must keep all parts consistent. python-docx 1.2 has no replies/resolved/tracked changes → not enough. |
| Stable addresses | `w14:paraId` useful but unreliable (Word may regenerate, some files lack it). | paraId + content hash, re-verified before every edit. |
| Pages when closed | `lastRenderedPageBreak` is stale/misplaced; LibreOffice headless within a few % if fonts present. | "Estimated" pages; optional LibreOffice. |
| Windows live | COM via pywin32 (late-bound, one STA thread) covers everything. Pitfalls: RPC_E_CALL_REJECTED on modal dialogs, Protected View, OneDrive AutoSave, focus rules, Find limited to 255 chars. Centering needs our own code. | Windows live engine = COM. Retry/backoff + "Word busy" state. `UndoRecord` so one batch = one undo. |
| Office.js | Perpetual Word 2016/2019/2021 (Windows) only supports WordApi ≤1.3; comments/track changes need 1.4+, accept/reject 1.6. On Mac it fills AppleScript gaps on current builds. | Not viable as the main engine on Windows; possible gap-filler on Mac later. |
| Mac versions | Office 2016 Mac unsupported; Office 2019 Mac read-only since July 2026; 2021 supported only until 13 Oct 2026. | "Any Word version" on Mac realistically = M365 / 2024 (16.x builds). |
| Mac live | AppleScript (not JXA: it can't create comments) covers ~85%. Missing: comment replies, resolve, scroll/centre. Automation permission prompt + error -1743 if the host lacks the Apple Events entitlement. | Mac second; signed+notarized helper with stable bundle ID; batch one script per call. |
| Distribution | `.mcpb` desktop extensions support an (experimental) `uv` type: Desktop fetches uv and builds the venv. Cowork/Claude Code use plugins (`.mcp.json`). No `.exe` → no SmartScreen/notarization for the core. | MCP via `.mcpb` (uv) + plugin; Skill zip for chat sandbox (stdlib file engine). |
| Chat sandbox | Python 3.11, no network (or account-dependent). | Stdlib-only core, Python ≥3.11. |
| API | Freeze one JSON envelope, stable error codes, `schema_version`, a capabilities command (like MCP's handshake). | Contract-first, semver. |
| Anchoring | Hypothesis model: quote + prefix/suffix + position, exact then fuzzy, weighted scoring, no minimum score. | Same, plus minimum score and `ambiguous`/`not_found` codes; narrow by paragraph hash before fuzzy search (difflib too slow on a whole book). |
| Segmentation | Own stdlib Spanish segmenter is reasonable; port pySBD / pragmatic_segmenter abbreviation lists and tests (MIT). | Own segmenter + borrowed test corpus. |
| Prior art | Office-Word-MCP-Server archived (Mar 2026). Safe Docx (Apache-2.0, TS), docx-editor, docx-revisions, python-redlines (MIT), word-mcp-live (COM+JXA). Nobody offers stable quote anchors, Spanish segmentation or a versioned contract. | Borrow ideas; check licenses before reusing code. |
| License | Apache-2.0 allows borrowing MIT and Apache code and includes a patent grant. | **Apache-2.0** (added as `LICENSE`). |

## 1. File engine (OOXML)

Note: WebFetch could not reach most hosts (DNS failure), so most answers rest on search summaries, ECMA-376 mirrors, docx4j source fetched raw from GitHub, and two local experiments on Python 3.13 (ElementTree round trip, 100k-word benchmark). Points marked [GK] are general knowledge that was not checked against a source.

### Q1.1 Tracked changes representation
- `w:ins` / `w:del` wrap runs. Deleted text must be in `w:delText`, not `w:t`; deleted field code goes in `w:delInstrText`.
- Formatting changes: `w:rPrChange` (inside `w:rPr`) and `w:pPrChange` (inside `w:pPr`) hold the *old* properties.
- Deleted paragraph mark: `<w:pPr><w:rPr><w:del .../></w:rPr></w:pPr>`. The paragraph then joins the next one, but its contents are not automatically marked deleted. An inserted paragraph mark uses `w:ins` in the same place.
- Moves: `w:moveFrom` / `w:moveTo` around runs, bracketed by `w:moveFromRangeStart/End` and `w:moveToRangeStart/End`, which are paired by `w:name`. A `moveFrom` with no matching destination makes the document non-conformant.
- Attributes: `w:id` is required, and `w:author` and `w:date` (ISO 8601, `Z`) should always be set. Keep `w:id` unique across every revision; [GK] the safe choice is one counter shared with comment and bookmark ids.
- What corrupts or breaks a file:
  - `w:t` inside `w:del`, which breaks Reject;
  - `w:ins` crossing a hyperlink or field boundary (seen in a LibreOffice bug);
  - nesting `w:ins` inside `w:ins`;
  - elements in the wrong schema order inside `w:rPr` / `w:pPr`.

Risk/decision for us: generate revisions only through a small set of validated templates, use one global id allocator (max existing + 1), and never split a field or hyperlink with a revision wrapper. Moves are out of scope for v1: a move is written as del+ins.

Sources: https://www.vaquill.ai/engineering/word-tracked-changes-in-python ; https://docs.unidoc.io/unioffice/guides/document/comments-and-revisions/track-changes/ ; https://bugs.libreoffice.org/show_bug.cgi?id=147892 ; https://webapp.docx4java.org/OnlineDemo/ecma376/WordML/del_2.html ; https://webapp.docx4java.org/OnlineDemo/ecma376/WordML/moveToRangeStart.html ; https://learn.microsoft.com/en-us/previous-versions/office/developer/office-2010/cc802017(v=office.14) ; https://gitlab.com/okapiframework/okapi/-/issues/847

### Q1.2 Comments, replies, resolved state
- **comments.xml** holds `w:comment` elements with `w:id`, `w:author`, `w:date` and `w:initials`. Each comment's paragraphs carry `w14:paraId`.
- **document.xml** anchors a comment with `w:commentRangeStart` / `w:commentRangeEnd` plus a run containing `w:commentReference w:id`.
- **commentsExtended.xml** (w15, Word 2013+) has `w15:commentEx` with `w15:paraId` (required), `w15:paraIdParent` (the reply link) and `w15:done="1"` (resolved). `paraId` is the paraId of the comment's last paragraph.
- **commentsIds.xml** (w16cid, Word 2016+) maps `paraId` to `durableId`.
- **commentsExtensible.xml** (w16cex, Word 365 "modern comments") holds `durableId` and `dateUtc`.
- **people.xml** (w15) holds `w15:person` with `w15:presenceInfo`.
- Older Word versions ignore the w15/w16 parts. Replies then show up as flat comments, and the resolved state is lost.
- In modern comments, setting `done` on a reply has no visible effect: resolution belongs to the thread. Libraries report "done in XML but not shown as resolved" when the parts are inconsistent.
- Content types and relationship types (verified in docx4j source):
  - comments: `...wordprocessingml.comments+xml`, rel `http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments`
  - commentsExtended: `...commentsExtended+xml`, rel `http://schemas.microsoft.com/office/2011/relationships/commentsExtended`
  - commentsIds: `...commentsIds+xml`, rel `http://schemas.microsoft.com/office/2016/09/relationships/commentsIds`
  - people: `...people+xml`, rel `http://schemas.microsoft.com/office/2011/relationships/people`
  - [GK] commentsExtensible: `...commentsExtensible+xml`, rel `http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible`

Risk/decision for us: on write, keep all four comment parts consistent (create the missing ones, and add an Override plus a document relationship for each). Put replies on the parent's range, and set `done` on the root comment.

Sources: https://raw.githubusercontent.com/python-openxml/python-docx/master/docs/dev/analysis/features/comments.rst ; https://raw.githubusercontent.com/plutext/docx4j/master/docx4j-core/src/main/java/org/docx4j/openpackaging/contenttype/ContentTypes.java ; https://raw.githubusercontent.com/plutext/docx4j/master/docx4j-core/src/main/java/org/docx4j/openpackaging/parts/relationships/Namespaces.java ; https://javadoc.io/static/org.docx4j/docx4j/6.1.2/org/docx4j/w15/CTCommentEx.html ; https://learn.microsoft.com/en-us/office/vba/api/word.comment.done ; https://forum.aspose.com/t/comment-addreply-sometimes-does-not-show-as-indented-reply-in-word/248990

### Q1.3 Finding text across runs
- Word splits text unpredictably: rsid boundaries, `w:proofErr`, `w:bookmarkStart/End`, `w:commentRangeStart`, `w:lastRenderedPageBreak`, field runs (`w:fldChar` / `w:instrText`, `w:fldSimple`), `w:hyperlink`, `w:smartTag`, `w:sdt` and existing `w:ins` / `w:del`.
- Best practice is to build a per-paragraph *text map*: the concatenated visible text plus a list of (run, `w:t`, offset) entries.
  - Skip `w:delText`, field instructions and zero-width markers.
  - Treat `w:tab` and `w:br` as characters.
- Match on the map. Then split only the boundary runs at match edges, cloning their `w:rPr`, and wrap the covered runs in `w:del` with a new `w:ins` run. The new run copies the `rPr` of the first consumed character, as paper-docx does.
- Never rewrite `paragraph.text`: that drops run formatting.
- It is safe to drop `w:proofErr`. Optionally merge adjacent runs with identical `rPr`, ignoring rsid attributes.
- Refuse or restrict matches that cross a field, hyperlink, content-control or paragraph boundary.

Risk/decision for us: implement a text map with explicit "atomic" containers (fields, hyperlinks, sdt). Matches that cross them fail with a clear error instead of producing a broken structure.

Sources: https://pypi.org/project/python-docx-replace ; https://libraries.io/pypi/paper-docx ; https://github.com/pablospe/docx-editor/ ; https://bugs.libreoffice.org/show_bug.cgi?id=147892

### Q1.4 Stable paragraph IDs (w14:paraId / w14:textId)
- `paraId` is an 8-hex-digit value greater than 0 and less than 0x80000000. It must be unique across document.xml, headers, footers, footnotes, endnotes and comments.
- Word keeps existing ids across saves, but replaced every tool-generated paraId on the first save in one Open XML SDK report. They were stable afterwards.
- Duplicates (for example from cloning) cause real bugs, such as duplicated rendering under sensitivity labels.
- `textId` is a text-revision hash. `77777777` is a common placeholder.
- Files from LibreOffice and older generators often have no paraIds at all. Some tools drop them on save.

Risk/decision for us: use paraId as the primary address when present, and add new unique paraIds to paragraphs we create or split. Back it with a content hash plus an ordinal, like docx-editor's `P12#a7b2`, and re-verify the hash before every edit. Do not assume ids survive a save by Word or another tool.

Sources: https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.features.iparagraphidgeneratorfeature.createuniqueparagraphid?view=openxml-3.0.1 ; https://github.com/OfficeDev/Open-XML-SDK/issues/925 ; https://forum.aspose.com/t/deepclone-copies-w14-paraid/280402 ; https://glama.ai/mcp/servers/h4x0r/docx-mcp/tools/validate_paraids ; https://www.docx4java.org/forums/docx-java-f6/docx4j-generates-invalid-paraid-and-textid-t2178.html ; https://github.com/tensorbee/rdocx/issues/130

### Q1.5 Zip rewrite and XML serialization
**Zip.**
- `zipfile` has no public raw-copy API (a stdlib proposal exists).
- Option 1: copy each untouched member's decompressed bytes into a new archive, reusing its `ZipInfo` so name, date_time, order and compress_type stay the same. The CRC is then identical and the part is byte-identical *after decompression*. This is cheap because zlib is C code.
- Option 2: get a truly raw copy by reading the local header and compressed bytes at `header_offset` and writing a new central directory, in about 100 lines of stdlib code.
- Word does not require `[Content_Types].xml` to come first, but some third-party readers do. Keep the original order.
- Write to a temporary file, then rename it atomically.

**XML: a major pitfall, confirmed locally on Python 3.13.**
- `xml.etree` renames prefixes to `ns0:`, `ns1:` unless you call `register_namespace`.
- Even after registration, it emits only namespaces that are *used by elements or attributes*. It drops `xmlns:w15` and similar declarations that are referenced only inside the `mc:Ignorable="w14 w15"` attribute value. Word then reports unreadable content.
- It also drops `standalone="yes"`, writes the declaration with single quotes, and loses comments and processing instructions (unless you use a special parser).

**Workarounds:**
- Re-inject the root start tag verbatim. Keep the original bytes of the root element's opening tag and splice the serialized children in after it.
- Or patch strings at the part level: edit only the byte ranges of changed paragraphs and leave everything else untouched. This fits "touch only what changed" best.
- lxml preserves prefixes and declarations, but it is not stdlib.

Risk/decision for us: parse with ElementTree for reading and addressing. Write by splicing each modified paragraph's serialized XML into the original part bytes, with all prefixes registered from the source's root declarations. Check that every `mc:Ignorable` prefix is still declared.

Sources: https://discuss.python.org/t/add-copy-between-zipfiles-without-recompressing-files/67544 ; https://isc.sans.edu/diary/26662 ; https://gitlab.com/okapiframework/okapi/-/issues/291 ; https://www.docx4java.org/forums/docx-java-f6/updating-text-in-header-footer-results-in-corrupt-docx-file-t2668.html ; https://javadoc.io/static/org.docx4j/docx4j/6.1.2/org/docx4j/jaxb/package-use.html ; https://www.python.org/pipermail/python-list/2005-June/317606.html

### Q1.6 Estimating page numbers without Word
- **`w:lastRenderedPageBreak`** (written by Word at the last layout): free and fairly good if the document was last saved by Word and the edits are small. Eric White (Open-XML-PowerTools) says it is often misplaced, though. It goes stale after edits and is absent in files from non-Word generators.
- **Explicit breaks**: `w:br w:type="page"`, `w:pageBreakBefore` and section breaks give hard lower bounds.
- **LibreOffice headless** (`soffice --convert-to pdf`, then `pdfinfo`): typically within a few percent of Word if the same or metric-compatible fonts are installed. One reported case was 267 vs 275 pages. Gaps come from missing fonts, tables, and dropped blank pages. It takes seconds per document and is a heavy dependency.
- **docx2pdf** needs Word itself (on Windows or macOS), so it does not fit.
- **Heuristics** (characters per line from page width, margins and font size; lines per page; tables and images estimated): roughly ±10-20%. [GK] No benchmarks were found.

Risk/decision for us: return "estimated page", taken from lastRenderedPageBreak when present and from a heuristic otherwise, and always label it as approximate. Offer an optional LibreOffice-based "precise" mode when it is installed.

Sources: https://www.ericwhite.com/blog/?p=4043 ; https://www.ericwhite.com/blog/?p=4164 ; https://ask.libreoffice.org/t/docx-has-a-different-page-count-in-libreoffice/23816 ; https://pdfa.org/wp-content/uploads/2020/07/Automate-office-files-to-PDF.pdf ; https://www.libreoffice.org/bugzilla/show_bug.cgi?id=132496 ; https://hub.docker.com/r/ofthemachine/libreoffice

### Q1.7 Verifying "opens in Word" without Word
- **mikeebowen/OOXML-Validator** is a .NET CLI around the Open XML SDK validator. It reports errors as JSON and is what pandoc uses in CI. It catches more than XSD validation does.
- **Clippit.Cli** (`word verify`) checks schema and relationships.
- **xmllint plus the ECMA-376 XSDs** is weaker: it does not understand the MCE extensions.
- **LibreOffice round trip** (convert, then check the exit code and output) catches gross breakage only; LibreOffice is more lenient than Word.
- A clean SDK validation does not guarantee Word opens the file.

Our own cheap checks:
- every part is well-formed;
- `[Content_Types]` covers every part;
- every relationship target exists;
- revision and comment ids are unique, and comment ranges and references are paired;
- `mc:Ignorable` prefixes are declared;
- paraIds are unique and in range.

Risk/decision for us: build a stdlib "structural lint" into the engine and run it after every save. Use OOXML-Validator plus a LibreOffice round trip in CI on a corpus of documents, with real Word spot checks before a release.

Sources: https://github.com/mikeebowen/OOXML-Validator ; https://feed.nuget.org/packages/Clippit.Cli ; https://pgit.metala.org/github.com/jgm/pandoc/log/tools/validate-docx2.sh?showmsg=1 ; https://learn.microsoft.com/en-us/answers/questions/1191888/what-is-invalid-about-this-docx

### Q1.8 Existing libraries and limits
- **python-docx 1.2** added comments (`add_comment`, rich content) but has no replies, no resolved state and no tracked-changes API (the issue and PRs are unmerged). It needs lxml.
- **docx4j** (Java) and the **Open XML SDK** (.NET) are full, lossless typed models. The SDK includes the validator and a paraId generator. Both are good references for part, relationship and type constants.
- **Aspose.Words** is commercial and complete, with tracked changes, comments and a layout engine for page numbers. It has paraId duplication bugs on clone.
- **Pandoc** reads tracked changes (`--track-changes=accept|reject|all`) [GK]. It is lossy, not an editor.
- **docx-editor** (pablospe) is the closest analogue: pure Python plus defusedxml. It does tracked replace/insert/delete, comments with reply/resolve, accept/reject, cross-run find, and `P#hash` ids. It does not handle tables, headers or footers.
- Others: **safe-docx** (UseJunior) has good engineering notes on revisions and moves; **msdocx** and **scitex-msword** have small `w:ins`/`w:del` helpers; **python-redlines** wraps the .NET WmlComparer [GK].

Risk/decision for us: no stdlib-only library covers our scope. Borrow ideas from docx-editor (addressing, batch validation, rationale comments), safe-docx (edge cases) and docx4j/SDK (constants, schema order), and write our own core.

Sources: https://python-docx.readthedocs.io/en/stable/api/comments.html ; https://raw.githubusercontent.com/python-openxml/python-docx/master/docs/dev/analysis/features/comments.rst ; https://github.com/pablospe/docx-editor/ ; https://www.vaquill.ai/engineering/word-tracked-changes-in-python ; https://pypi.org/project/msdocx/ ; https://scitex-python.readthedocs.io/en/stable/_modules/scitex_msword/track_changes.html ; https://www.usejunior.ai/engineering/safe-docx/accept-tracked-changes/accept-moves ; https://forum.aspose.com/t/deepclone-copies-w14-paraid/280402

### Q1.9 Performance
I measured this locally (Python 3.13, ElementTree) on a synthetic 100k-word document.xml: 2.1 MB, 5,000 paragraphs, 20,000 runs.

| Step | Time |
|---|---|
| Parse | 0.22 s |
| Replace 20,000 matches | 0.01 s |
| Serialize | 0.18 s |
| Deflate into a zip | 0.01 s |
| Plain string replace (for comparison) | 0.02 s |

A full rename across the whole document therefore takes well under 1 s, before tracked-change markup. Wrapping each hit in `w:del`/`w:ins` with run splits adds an estimated 2-5x, which is still a few seconds at most. Real documents with heavy run fragmentation, comments and several parts may be 2-3x larger.

Risk/decision for us: pure stdlib is feasible. Keep the parsed tree for each part cached across operations in a session, and re-serialize only the touched parts.

Sources: local benchmark (this session); https://discuss.python.org/t/add-copy-between-zipfiles-without-recompressing-files/67544

## 2. Live engine: Windows

### Q2.1 COM from Python: pywin32 vs comtypes vs PowerShell/.NET vs C# helper
**pywin32** is the de-facto choice, and every live Word MCP project we found uses it. You can use it late-bound (`win32com.client.dynamic.Dispatch`, no typelib, works with any Word version) or early-bound (`gencache.EnsureDispatch` writes a `gen_py` cache). In a PyInstaller build the cache has to be redirected to a temp dir (PyInstaller's runtime hook does this), and you must not ship a stale `gen_py`. Late binding avoids the problem entirely. **comtypes** is pure Python and also works, but its community is smaller. **PowerShell** costs about 1 s to start per call and has quoting problems, so it is only good for prototypes. **A C# helper** gives the best typing, and implementing `IMessageFilter` is easy there, but it adds a second runtime and an IPC hop. Every property access is a cross-process round trip with no caching (Hammond), so cache objects and keep the number of calls low. Threading: Word is an STA server. Call `CoInitialize` in each thread and create the object in that same thread, or marshal it with `CoMarshalInterThreadInterfaceInStream`. Simplest is one dedicated COM thread. To attach, use `GetActiveObject("Word.Application")`. It can return an empty instance, so fall back to scanning the ROT for document monikers and then use `doc.Application`, as word-mcp-live does. Never start a new Word with `Dispatch` if the writer's own instance is already running.

Risk/decision for us: use pywin32, late-bound, with one STA worker thread. Attach through GetActiveObject, falling back to a ROT scan that matches the doc by FullName.

Sources: https://py2exe.org/index.cgi/UsingEnsureDispatch · https://opensource.netapp.com/Inventory_Collect_Tool/3.4.0/pyinstaller-2.1-py2.7.egg/PyInstaller/loader/rthooks/pyi_rth_win32comgenpy.py · https://www.python.org/pipermail/python-win32/2012-May/012307.html · https://lists.zope.org/pipermail/zope/2003-May/136437.html · https://learn.microsoft.com/en-us/dotnet/api/system.runtime.interopservices.marshal.getactiveobject · https://support.microsoft.com/help/316126 · https://github.com/ykarapazar/word-mcp-live (core/word_com.py)

### Q2.2 Object model coverage per operation
Every needed operation is in the Word object model and has existed since Word 2010 or earlier:
- **Find / select:** `Range.Find.Execute`. The find text is limited to 255 characters, so for longer passages search a unique prefix and check the rest. Then `Range.Select`.
- **Scroll:** `Window.ScrollIntoView(rng, Start)` aligns the range top-left or bottom-right only and has no centering option. To center, follow it with `SmallScroll` or compute a position with `GetPoint`. It does not work in Outline view.
- **Front:** `Application.Activate` / `Window.Activate`.
- **Track Changes:** `Document.TrackRevisions`. Replacing text while it is True produces a real revision.
- **Revisions:** `Revisions.AcceptAll` / `RejectAll`, `Revision.Accept` / `Reject`.
- **Comments:** `Comments.Add(rng, text)`. `Comment.Replies` (whose `.Add` creates a reply) and `Comment.Done` arrived with the Word 2013 threaded-comment redesign, so they are absent in 2010.
- **Selection:** `Selection.Range.Text`, `.Start`, `.End`.
- **Page number:** `Range.Information(wdActiveEndPageNumber=3)`. This forces repagination and is slow on large docs.
- **Dirty check:** `Document.Saved`.
- **Save / close / reopen:** `Save`, `Close`, `Documents.Open`, then restore the stored Start offset.

**Modern comments** (M365 only, so not perpetual 2016–2021): the API still works. Setting `Done` on a single reply has no visible effect, though it is still written to the file. `Options.CommentsColor` and `CommandBars("Comments")` customization no longer work.

Risk/decision for us: full coverage on 2013 and later. Resolve the root comment, not individual replies. Write our own centering and the long-passage workaround for Find.

Sources: https://learn.microsoft.com/en-us/office/vba/api/Word.Comment · https://learn.microsoft.com/en-us/office/vba/api/word.comment.done · https://learn.microsoft.com/pt-br/archive/blogs/officedevdocs/working-with-tracked-changes-and-comments-programmatically-in-word-2013 · https://devblogs.microsoft.com/microsoft365dev/word-comments-vba-deprecation · https://learn.microsoft.com/pl-pl/office/vba/api/word.window.scrollintoview · https://forum.uipath.com/t/string-parameter-too-long-when-replacing-text/2449/6

### Q2.3 Pitfalls
- **Busy Word:** when Word has a modal dialog open, is in-place editing, or is busy, calls fail with `RPC_E_CALL_REJECTED` (0x80010001) or "retry later". Fix: register an `IMessageFilter` whose `RetryRejectedCall` retries for about 30 s. In pywin32 that is awkward, so a retry/backoff wrapper around `com_error` is the usual substitute. A modal dialog blocks us until the user closes it, so we have to report "Word is busy".
- **Multiple instances:** only one is registered as `Word.Application` in the ROT, so enumerate the ROT and match the document.
- **Protected View:** these docs are not in `Documents`. They show up in `ProtectedViewWindows`, and `.Edit()` returns a Document only after the user agrees.
- **AutoSave/OneDrive:** `AutoSaveOn` is True by default for cloud files, so `Saved` is rarely False and every edit is saved immediately. `FullName` is an https URL. BeforeSave fires periodically.
- **Focus:** Windows restricts `SetForegroundWindow`. Our process needs foreground rights, then calls `AllowSetForegroundWindow(wordPID)`. Otherwise the taskbar button just flashes.
- **Undo:** wrap each batch in `Application.UndoRecord.StartCustomRecord(name)` / `EndCustomRecord` (Word 2010+, name max 64 characters, check `IsRecordingCustomRecord`). Do not switch documents inside a record.

Risk/decision for us: a retry wrapper plus a "Word busy" state are mandatory, as are a ROT scan, a Protected View check, and an AutoSave warning. Every agent action runs inside one UndoRecord.

Sources: https://learn.microsoft.com/en-us/windows/win32/api/objidl/nf-objidl-imessagefilter-retryrejectedcall · https://windows-hexerror.linestarve.com/q/so28704807-avoiding-vsto-word-automation-timing-rpcecallrejected-errors · https://learn.microsoft.com/en-us/office/vba/api/word.protectedviewwindow · https://learn.microsoft.com/en-us/office/vba/api/word.document.autosaveon · https://learn.microsoft.com/hu-hu/office/vba/library-reference/concepts/how-autosave-impacts-addins-and-macros · https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow · https://learn.microsoft.com/en-us/windows/win32/api/objbase/nf-objbase-coallowsetforegroundwindow · https://learn.microsoft.com/id-id/office/vba/word/concepts/working-with-word/working-with-the-undorecord-object

### Q2.4 Office.js add-in as an alternative
Requirement sets supported by perpetual Office on Windows (Microsoft table):

| Version | WordApi sets |
|---|---|
| 2016 | 1.1 only |
| 2019 | up to 1.3 |
| 2021 volume-licensed/LTSC | 1.1–1.3 |
| 2021 retail and 2024 | 1.4 and later |

Comments and change-tracking mode need WordApi 1.4, and tracked-change accept/reject needs 1.6. So on 2016, 2019 and 2021 LTSC an add-in cannot do comments or Track Changes. It can still select (`range.select()` scrolls the range into view) and read the selection. Perpetual versions up to 2019 also host add-ins in IE11 (Trident) or EdgeHTML webviews, which means old JavaScript. To reach the add-in, an external agent needs a local `wss://localhost` server with a trusted certificate. The add-in polls or connects to it; examples are @office-agents/bridge and enhanced-word-mcp-server-live. Non-technical users would have to sideload the manifest (shared-folder catalog) or get it through admin deployment. Pros: cross-platform (Mac, web) and sandboxed. Cons: version gaps, certificate and sideloading friction, and the task pane must be open.

Risk/decision for us: use COM as the primary engine. Office.js is optional later, for Mac/web on M365 only.

Sources: https://github.com/OfficeDev/office-js-docs-reference/blob/live/docs/requirement-sets/word/word-api-requirement-sets.md · https://raw.githubusercontent.com/OfficeDev/office-js-docs-reference/live/docs/requirement-sets/requirement-sets.md · https://learn.microsoft.com/en-us/javascript/api/requirement-sets/word/word-api-1-4-requirement-set · https://learn.microsoft.com/en-us/javascript/api/requirement-sets/word/word-api-1-6-requirement-set · https://learn.microsoft.com/en-us/office/dev/add-ins/concepts/browsers-used-by-office-web-add-ins · https://learn.microsoft.com/en-gb/answers/questions/234390/officejs-add-in-communicate-with-external-local-na · https://cdn.jsdelivr.net/npm/@office-agents/bridge@0.0.2/README.md

### Q2.5 Performance in a 100k-word document
We found no published benchmark. The cost is driven by how many cross-process calls we make, not by document size. A single `Range.Find.Execute` runs inside Word and is fast, typically sub-second even on large documents (to be verified on our test doc). Iterating `Paragraphs` from Python costs about 2–3 COM calls per paragraph, i.e. thousands of round trips, and is orders of magnitude slower. Tips:
- Use Find, or pull `Content.Text` once, locate the passage in Python, then build `doc.Range(start, end)`. Beware that character offsets drift because of fields, hidden text and tables, so verify with Find.
- Cache object references.
- Turn off `ScreenUpdating` during multi-step edits and back on before Select/scroll.
- Avoid `Information(wdActiveEndPageNumber)` in loops (it forces repagination).
- Visible pagination slows bulk work (anecdotal: about 2x slower).

Risk/decision for us: benchmark on a real 100k-word file early. Locate with Find or the text dump, then do one Select and one scroll call.

Sources: https://www.python.org/pipermail/python-win32/2012-May/012307.html · https://developer.ds.hcl-software.com/t/a-solution-to-word-automation-running-slow/114067 · https://answers.microsoft.com/en-us/msoffice/forum/all/speed-up-a-macro/355c663d-653c-48fb-9d9b-4a9deff5f633 · https://msdn.microsoft.com/en-us/library/ms954148.aspx

### Q2.6 Existing open-source projects
- **ykarapazar/word-mcp-live** (PyPI `word-mcp-live`) is the most complete. It uses pywin32 COM on Windows and JXA on Mac, with about 44 live tools: find/replace, tracked changes under the user's name, comments with reply and resolve, revisions, and one undo step per AI action via `UndoRecord`. Worth copying: `GetActiveObject`, then a ROT scan for `.docx` and OneDrive monikers when it returns an empty instance (an M365/OneDrive quirk). We saw no retry, busy handling or scroll support.
- **kennypeh85/word-mcp** is a small COM attach-to-running server with about 20 tools.
- **GongRzhe/Office-Word-MCP-Server** has about 1.9k stars but is file-based (python-docx), not live.
- **enhanced-word-mcp-server-live** follows the Office.js add-in + WebSocket pattern.
- **dsbissett/office-addin-mcp** drives add-ins in WebView2 via CDP.

Risk/decision for us: reuse the patterns from word-mcp-live (ROT fallback, UndoRecord wrapper) and check its license. Our differentiators are busy/retry handling, centered scrolling, focus handling, and position-preserving reopen.

Sources: https://github.com/ykarapazar/word-mcp-live · https://pypi.org/project/word-mcp-live/ · https://www.remoteopenclaw.com/mcp/kennypeh85/word-mcp · https://gittrend.io/repo/GongRzhe/Office-Word-MCP-Server · https://unpkg.com/enhanced-word-mcp-server-live@1.0.1/word-live-addin/README.md · https://claudemarketplaces.com/mcp/dsbissett/office-addin-mcp

## 3. Live engine: Mac

> Research note: learn.microsoft.com and macscripter.net could not be fetched directly from this sandbox, so claims about them come from search-result extracts. Office.js data comes from Microsoft's docs source on GitHub (raw). The most concrete evidence is the source of `word-mcp-live` 1.6.21 (PyPI), a working Word-for-Mac live MCP server. "Unverified" marks claims we must test on a real Mac.

**Version reality check (matters for every Q):** Office 2016 for Mac is long out of support. Since 13 Jul 2026, Office 2019 for Mac has been in read-only "reduced functionality mode" (a certificate change) and needs a build ≥16.83 that it cannot reach. Office 2021 for Mac works only when updated to ≥16.83 (macOS 12+), and its support ends 13 Oct 2026. In practice the targets are **M365 plus updated 2021/2024**: all of them run the same 16.x binary line. Sources: https://heise.de/-11316619 , https://appleinsider.com/articles/26/05/28/microsoft-is-killing-office-2019-for-mac-and-iphone-and-you-cant-do-much-about-it , https://support.microsoft.com/office/update-microsoft-365-or-office-on-your-macos-or-ios-device-f418ae5d-bb5f-4078-b3d9-9340f5dd084e , https://feedbagel.com/post/microsoft-to-end-support-for-office-2021-this-year

### Q3.1 Bridge choice: AppleScript vs JXA vs osascript vs ScriptingBridge/PyObjC vs appscript
All of these options send the same Apple Events, so the real differences are reliability and per-call overhead.
- **AppleScript** is the reference language for Word's dictionary. Every forum example and Microsoft's only official reference (*Word 2004 AppleScript Reference*) use it. The dictionary looks frozen at the Word 2004/2011 object model: it mirrors old VBA names, has had no new features (no threaded comments), and Microsoft has published no new documentation.
- **JXA** works for most of Word. word-mcp-live drives 33 tools through `osascript -l JavaScript`. It has gaps, though: `make` fails for Word comments, so they fall back to AppleScript; colours must be 16-bit RGB lists; assigning a style name is silently ignored. JXA is widely seen as abandoned and buggy.
- **osascript per call** is simple but adds roughly 0.1–0.3 s of process start each time.
- **appscript** (py-appscript 1.4.0, Oct 2025) and **PyObjC ScriptingBridge** (12.2.2, Aug 2026) are both maintained and run in-process, which is faster. ScriptingBridge's lazy evaluation is known to be fragile with Office.

**Risk/decision for us:** Generate AppleScript (not JXA) as the primary language. Run it in-process through NSAppleScript/OSAKit (PyObjC) or a small Swift helper, which avoids osascript start-up cost. Keep osascript as the fallback.
Sources: word-mcp-live `core/word_mac.py` (https://pypi.org/project/word-mcp-live/ , https://github.com/ykarapazar/word-mcp-live), https://developer.apple.com/forums/thread/103309 , https://sharedcontext.ai/skills/external/iBz-04/automating-word , https://pypi.org/project/appscript/ , https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/ScriptingBridgeConcepts/ImproveScriptingBridgePerf/ImproveScriptingBridgePerf.html , https://forum.latenightsw.com/t/fastscripts-3-speed-issues-with-asobjc-scripts/3953/16

### Q3.2 Coverage of the needed operations in Word's dictionary
Each operation below was checked against word-mcp-live code or forum examples.
- **Find:** works. Use the selection's `find object` with `execute find`, then read `selection start/end`. `create range doc start s end e` builds a range.
- **Select:** works, by setting `selection start/end` or calling `select`.
- **Scroll into view:** missing. No `ScrollIntoView` equivalent was found; the forum workaround is to select, then send an arrow key through UI scripting. Centring is not available anywhere (unverified: `vertical percent scrolled` of window).
- **Bring Word to front:** works with `activate`.
- **Track Changes:** works. Toggle it with `track revisions of document`; replace while it is on.
- **Revisions:** works. `revisions`, `accept`/`reject` on each revision, `accept all revisions`/`reject all revisions`.
- **Comments:** adding works (`make new Word comment ... with properties {comment text, scope}`), as do listing and deleting. **Replies and resolve/unresolve are not available**; word-mcp-live marks them "Windows only".
- **Read selection:** works with `content of selection`.
- **Unsaved changes:** works through the `saved` property.
- **Page number:** works with `get range information r information type active end page number` (returns text) and `number of pages in document`.
- **Save/close/open:** works. Reopening at a position means opening the file, then calling `create range` and `select`.

**Risk/decision for us:** About 85% is covered natively. The real gaps are comment reply/resolve and scroll/centre; fill them with VBA (Q3.3) or Office.js (Q3.6).
Sources: word-mcp-live `word_mac.py` and README feature table (https://github.com/ykarapazar/word-mcp-live), https://www.macscripter.net/t/place-cursor-at-beginning-of-paragraph-n/70853 , https://macscripter.net/viewtopic.php?pid=209484 , https://www.macscripter.net/t/ms-word-collect-all-tracked-changes-metadata-into-a-csv-file/66587 , https://www.macscripter.net/t/return-the-number-of-pages-in-microsoft-word/72522

### Q3.3 `run VB macro`, VBA gap-filling, sandbox
- **How it works:** Word's dictionary still has `run VB macro macro name "Module.Macro"` (also `"Normal.NewMacros.X"`). A Keyboard Maestro user confirmed it works on recent Word. The old `do Visual Basic` command is reported dead since Word 2004.
- **Where the macro must live:** the macro has to exist in an open document, Normal.dotm, or a loaded global template. Startup templates go in `~/Library/Group Containers/UBF8T346G9.Office/User Content/Startup/Word`. Loading a template by script (`add addin`) returns `missing value` in Word 2016+, so the template must sit in Startup.
- **Prompts:** macros in Normal or Startup templates do not trigger the macro-enable prompt; macros in documents do.
- **Arguments:** no parameter passing was confirmed, so pass data through document variables or a file in the Group Container.
- **VBA coverage:** Windows VBA has `Comment.Replies.Add`, `Comment.Done` and `Window.ScrollIntoView`; whether Mac VBA has them is unverified.
- **Sandbox:** Word is sandboxed. VBA can reach outside apps only through `AppleScriptTask` with scripts in `~/Library/Application Scripts/com.microsoft.Word/`, because `MacScript` is deprecated and blocked. File access outside the container triggers "Grant File Access" prompts. Zotero uses this pattern and has reported Word freezes on AppleScriptTask.

**Risk/decision for us:** We can install an optional signed `.dotm` into Startup to fill the gaps: reply, resolve and ScrollIntoView. That install is intrusive (it touches the writer's Word setup), so make it opt-in and test that Mac VBA has these members.
Sources: https://forum.keyboardmaestro.com/t/easy-way-to-run-vba-macros-in-ms-word/38297 , https://www.macscripter.net/t/applescript-to-run-a-vb-macro-in-folders-and-sub-folders/68751 , https://www.macscripter.net/t/applescript-and-loading-ms-word-2016-template-containing-vba-macros/69904 , https://wordmvp.com/Mac/Applescript-VBA.html , https://learn.microsoft.com/en-us/office/vba/office-mac/applescripttask , https://forums.zotero.org/discussion/comment/373567/ , https://learn.microsoft.com/en-us/office/vba/api/word.window.verticalpercentscrolled

### Q3.4 macOS permissions (TCC)
- **Automation consent:** on the first event, macOS shows one prompt, "X wants access to control Microsoft Word". It is charged to the **responsible process**, the app at the top of the process tree, not to osascript or python. From Terminal or iTerm, the prompt names Terminal. From an MCP server spawned by Claude Desktop or Claude Code, it names that host app.
- **When the host lacks the entitlement:** if the host app is hardened-runtime signed without `com.apple.security.automation.apple-events`, events fail silently with -1743 and no prompt appears. Claude Code (issue 52712, closed Jun 2026) and Claude Desktop's terminal (issue 78582, fixed Aug 2026) both had this bug.
- **Repeated prompts:** TCC keys consent to the code-signing identity, so versioned or auto-updating binaries can trigger the prompt again (claude-code #66216).
- **A helper binary of our own** needs Hardened Runtime, the apple-events entitlement, `NSAppleEventsUsageDescription` in its Info.plist, and notarization. It must also be launched so that it is its own responsible process; otherwise the host app is blamed.
- **Accessibility permission** is not needed unless we fall back to UI scripting (for example, arrow keys to scroll).
- **Reset:** `tccutil reset AppleEvents`.

**Risk/decision for us:** Ship a signed, notarized helper app with a stable bundle ID and our own usage string. That gives users one clear prompt that survives host-app updates. Document the -1743 cases.
Sources: https://qt.io/blog/the-curious-case-of-the-responsible-process , https://community.jamf.com/t5/jamf-pro/outset-and-pppc/m-p/136010 , https://steipete.me/posts/2025/applescript-cli-macos-complete-guide , https://claudeissues.com/issue/52712-macos-hardened-runtime-blocks-apple-events-from-child-processes-missing-com-appl , https://claudeissues.com/issue/78582-bug-claude-desktops-built-in-terminal-can-never-get-apple-events-automation-perm , https://github.com/anthropics/claude-code/issues/66216 , https://developer.apple.com/forums/thread/675363 , https://lists.cyberduck.io/pipermail/cyberduck-trac/2018-September/039241.html

### Q3.5 Performance and hangs
- **Latency:** no published per-event figures for Word. Expect a few ms to tens of ms for each in-process event, plus 0.1–0.3 s for each osascript launch. Every property read is a round trip, so bulk-read with `content of text object`, or iterate inside one script rather than issuing one event per item.
- **Large documents:** scripts that work on short documents hit "AppleEvent timed out" (-1712, default timeout about 2 min). This also happened to Zotero on 400+ page documents, and Word could stay frozen until restarted. Use `with timeout`, split the work into chunks, and avoid looping over paragraphs one by one.
- **Busy Word:** Word blocks events while a modal dialog is open (save, grant access) or while busy, and Word for Mac had general freeze regressions in 2024.
- **Find scope:** a selection-based find moves the user's visible selection. Save and restore it, or use a range find.

**Risk/decision for us:** Batch work into one script per tool call, set explicit timeouts, detect modal or busy state, and prefer range-based find so we don't hijack the writer's cursor.
Sources: https://macscripter.net/viewtopic.php?id=48159 , https://forums.zotero.org/discussion/comment/305876/ , https://help.claris.com/archive/help/12/fmp/en/html/create_script.13.16.html , https://9to5mac.com/2024/05/23/microsoft-working-on-word-for-mac-freeze-fix/ , https://lists.apple.com/archives/Cocoa-dev/2004/Jan/msg01168.html

### Q3.6 Office.js add-in as the alternative
Office.js is supported on Mac. Minimum Mac builds for each requirement set:

| Requirement set | Mac build | What it gives us |
|---|---|---|
| WordApi 1.1 | 15.19 | `Range.select()` (selects and scrolls the UI to the range), `Document.saved`, `Document.save` |
| WordApi 1.3 | 15.32 | |
| WordApi 1.4 | 16.64 | comments with `reply()` and `resolved`, `changeTrackingMode` |
| WordApi 1.6 | 16.76 | `TrackedChange.accept`/`reject`, `acceptAll`/`rejectAll` |
| WordApi 1.9 | 16.91 | |
| WordApiDesktop 1.2 | 16.94 | `Range.pages` and `Page.index` (page number), `Pane.pagesEnclosingViewport` |
| WordApiDesktop 1.4 | 16.100 | `Document.activate` |

So Office.js covers the AppleScript gaps (reply, resolve) on current builds, but no API gives "centre". Office 2016 for Mac (16.16) gets only up to WordApi 1.3.

Sideloading: copy the manifest to `~/Library/Containers/com.microsoft.Word/Data/Documents/wef`, restart Word, then go to Home > Add-ins. The add-in must be opened per document (a task pane) and needs a bridge from the task pane to our agent, such as a localhost WebSocket or a cloud relay.

**Risk/decision for us:** This is the best fallback for reply and resolve. Consider a hybrid: AppleScript for control (activate, open/save, selection) and Office.js for comments and page numbers. The cost is install friction and the need to keep the task pane open.
Sources: https://github.com/OfficeDev/office-js-docs-reference/blob/live/docs/requirement-sets/word/word-api-requirement-sets.md (includes `word-1-4.md`, `word-1-6.md`, `word-desktop-1-2.md`), https://learn.microsoft.com/en-us/javascript/api/requirement-sets/word/word-api-requirement-sets , https://github.com/OfficeDev/office-js-docs-pr/blob/main/docs/testing/sideload-an-office-add-in-on-mac.md

### Q3.7 Existing open-source projects and lessons
- **word-mcp-live** (ykarapazar, PyPI 1.6.21) is the closest prior art. It is a Python MCP server using COM on Windows and JXA through osascript on Mac (33 of 41 live tools). Lessons from its code:
  - It falls back to AppleScript for comments.
  - It wraps edits as `prev = trackRevisions; set true; …; restore`.
  - Its find is selection-based and moves the cursor.
  - It finds page boundaries by binary search over `get range information`.
  - It cannot do reply, resolve or undo history on Mac.
  - Some setters are silently ignored (colours, style names), so it verifies writes by reading them back.
  - Some operations, such as row insert, work only on the active document, so it activates the document and then restores the previous one.
- **vAirpower/macos-office365-mcp-server:** an AppleScript proof of concept for creating documents, not live review.
- **Generic osascript/AppleScript MCP servers** (adamrdrew/applescript-mcp and similar) show the TCC friction in practice.

**Risk/decision for us:** Don't fork word-mcp-live wholesale. Reuse its findings, add range-based find, a helper app for TCC, and an Office.js or VBA path for comment threads.
Sources: https://github.com/ykarapazar/word-mcp-live , https://pypi.org/project/word-mcp-live/ , https://mcp.so/servers/macos-office365-mcp-server , https://glama.ai/mcp/servers/adamrdrew/applescript-mcp

## 4. Distribution, API design, anchoring, prior art

### Q4.1 MCP desktop extensions (.mcpb, formerly .dxt)
A `.mcpb` file is a zip with a `manifest.json` in it. The spec is labelled v0.3, but its `uv` example already uses 0.4. Server types are `node`, `python`, `binary` and `uv` (0.4+, marked experimental).
- **`python` type:** all dependencies must be bundled in `server/lib` or a full `server/venv`. Compiled wheels don't work across platforms.
- **`uv` type:** dependencies are declared in `pyproject.toml`. Claude Desktop downloads uv and builds the venv itself (a July 2026 bug log shows "UV 0.9.7 downloaded, venv created").
- **Node:** ships inside Claude Desktop, which is why the repo still recommends Node.
- **Manifest fields:** `compatibility.platforms` (darwin/win32/linux) and runtime constraints. `user_config` holds settings such as a `directory` the user picks.
- **Install and updates:** install by drag-and-drop in Settings > Extensions, then preview and Install. A curated gallery also exists. The repo says auto-updates are supported but gives no detail.
- **Signing:** `mcpb sign/verify` with X.509 or `--self-signed`. Admins can require signatures (`isDesktopExtensionSignatureRequired`).
- **Cowork:** uses plugins (a `.mcp.json` with stdio `command`, which may point to `${CLAUDE_PLUGIN_ROOT}`). Plugins are shared with Claude Code. Fetching happens on the host; Cowork itself runs in a VM.
- **Limits:** extensions run unsandboxed, with the user's permissions. Nothing documented forbids launching Word or using Apple Events. macOS TCC Automation prompts would be attributed to Claude.app (my inference).

Risk/decision for us: ship a `uv`-type `.mcpb` (stdlib core means a tiny bundle) plus a Claude plugin for Code/Cowork. Keep a `python`-type fallback, since `uv` is still experimental and buggy.
Sources: https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md · https://github.com/modelcontextprotocol/mcpb · https://claude.com/docs/connectors/custom/add-unlisted · https://claude.com/docs/connectors/overview.md · https://claude.com/docs/third-party/claude-desktop/extensions · https://github.com/modelcontextprotocol/mcpb/pull/158 · https://claudeissues.com/issue/77793-bug-extensions-tab-permanently-breaks-after-installing-a-manifest-version-0-4-uv · https://tessl.io/registry/skills/github/seanGSISG/claude-depot/build-mcpb

### Q4.2 Claude.ai code sandbox and Skills
- **API code-execution tool:** Python 3.11, 5 GiB RAM, 5 GiB disk, 1 CPU, no internet. Preinstalled packages include python-docx, python-pptx, openpyxl and pypdf. Uploaded files are supported.
- **claude.ai chat sandbox:** egress is set per organization or account. Options are off, package managers only (PyPI, npm, GitHub; the Team default) or extra domains. New Enterprise organizations default to off.
- **Running our code there:** a stdlib-only package can always run, uploaded as a zip/wheel or embedded in a Skill, even with egress off. That is a strong argument for the stdlib core.
- **Skills:** a Skill is a folder with `SKILL.md` that may include scripts. It is uploaded as a ZIP via Customize > Skills (paid plans), installed via a plugin marketplace in Claude Code, and uploaded via the Skills API. A Skill is the natural channel to ship the file engine plus usage instructions to chat users.
- **Anthropic's own docx skill:** licensed as "Proprietary"/source-available, so we can't vendor it. It edits raw XML via unzip/zip, writes `w:ins`/`w:del` by hand, and uses `comment.py`. It has no stable anchors.

Risk/decision for us: publish a "docx-engine" Skill that bundles the pure-Python core and CLI. Never depend on pip or network in that sandbox.
Sources: https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool · https://support.claude.com/en/articles/12111783 · https://github.com/anthropics/skills · https://github.com/anthropics/skills/blob/main/skills/docx/SKILL.md · https://platform.claude.com/docs/en/build-with-claude/skills-guide

### Q4.3 Python distribution for non-technical users
- **Frozen binaries:** PyInstaller and Nuitka produce standalone binaries, and Briefcase produces native installers. All of them must be signed or users get warnings.
- **uv:** `uv` with python-build-standalone gives a managed interpreter with no admin rights. This is what MCPB `uv` uses.
- **Windows:** EV signing no longer bypasses SmartScreen (removed in 2024). EV and OV now build reputation per file hash in the same way, so a new build starts at zero.
- **Azure Artifact Signing (formerly Trusted Signing):** about US$9.99/month. Organizations in US/CA/EU/UK are eligible; individuals only in US/CA.
- **macOS:** Apple Developer ID costs US$99/year, and `notarytool` notarization is mandatory for downloaded binaries.
- **The key point:** `.mcpb`, Skills and plugins avoid OS gatekeeping entirely, because Claude Desktop executes our Python/uv code and the user never runs an unsigned `.exe`.

Risk/decision for us: avoid shipping frozen executables in v1. Distribute as `.mcpb` (uv) plus PyPI (`uvx word-control`). Revisit signing only if a standalone installer becomes necessary.
Sources: https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options · https://www.todesktop.com/blog/posts/windows-apps-psa-ev-certs-do-not-grant-immediate-reputation-anymore · https://azure.microsoft.com/de-de/pricing/details/artifact-signing/ · https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution · https://docs.astral.sh/uv/guides/install-python/

### Q4.4 CLI/JSON API design for agents
Practitioner conventions agree on these points:
- JSON on stdout and human text on stderr. Default to JSON when stdout isn't a TTY.
- One stable envelope for success and error: `{ok, data, error:{code, message, hint}, warnings, next_cursor}`.
- Documented, deterministic exit codes per category, with stable string error codes (e.g. `ANCHOR_NOT_FOUND`, `ANCHOR_AMBIGUOUS`, `STALE_REVISION`).
- `--dry-run` for mutations. Reject unknown flags.
- A `schema`/`describe` command that returns the JSON Schema for every operation.
- Batch operations sent as a JSON document.
- For LLM use, add `--max-chars`/token budgets with truncation markers, cursors for paging long documents, and an `etag`/revision hash so stale edits fail.

Versioning:
- semver for the package.
- A `schema_version` / `api_version` field in every response.
- Additive-only changes within a major version.
- A `capabilities` command that lists features, modelled on MCP's `initialize` (protocol version string plus capability objects).
- Downstream repos pin a major version and test against published JSON Schemas.

Risk/decision for us: freeze the envelope and error codes before v0.1 and publish the schemas in the repo. The MCP tools should be thin wrappers over the same operations.
Sources: https://blog.arcjet.com/designing-a-cli-for-ai-agents/ · https://tessl.io/registry/skills/github/popey/claude-code-skills/ai-native-cli · https://skills.sh/ggonzalez94/skills/agent-cli-design · https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle · https://semver.org/

### Q4.5 Robust anchoring
**W3C selectors:** TextQuoteSelector is `{exact, prefix, suffix}` and TextPositionSelector is `{start, end}`. Clients store both.

**How Hypothesis re-anchors:**
1. Try the position plus exact quote.
2. Otherwise run `matchQuote`: exact `indexOf` first, then approximate search with the `approx-string-match` library (Myers bit-parallel), allowing at most min(256, len/2) errors.
3. Rank candidates by weighted score: quote 50, prefix 20, suffix 20, position hint 2.
4. No minimum threshold is applied in code, which is a risk; we should add one.

**Other approaches:** Google Docs named ranges are server-side ranges that shift with edits. Paragraph fingerprints (hash of normalized text) plus Word's `w14:paraId` are cheap first-pass keys, but Word regenerates paraIds.

**Stdlib feasibility:**
- `difflib.SequenceMatcher` is roughly quadratic and too slow over a whole novel.
- But we can narrow first: exact `str.find`, then paragraph-hash lookup, then fuzzy search only within candidate paragraphs. A pure-Python Myers/bitap with k errors fits in about 100 lines and is fine at paragraph scale.

Risk/decision for us:
- Anchor = `{exact, prefix, suffix (~32 chars), para_hint (index + paraId + hash), position}`.
- Resolve in tiers and return a confidence score.
- Return `ANCHOR_AMBIGUOUS` or `ANCHOR_NOT_FOUND` below a threshold. Never silently pick a low-score match.
Sources: https://www.w3.org/TR/annotation-model/#text-quote-selector · https://github.com/hypothesis/client/blob/main/src/annotator/anchoring/match-quote.ts · https://github.com/robertknight/approx-string-match-js · https://developers.google.com/docs/api/how-tos/named-ranges · https://docs.python.org/3/library/difflib.html

### Q4.6 Sentence segmentation
| Library | License | Dependencies | Spanish |
|---|---|---|---|
| pySBD (port of Ruby pragmatic_segmenter) | MIT | pure Python, no deps | yes, has a Spanish module; 22 languages |
| syntok | MIT | needs the `regex` C extension | designed for Spanish, English, German |
| segtok | MIT | — | European languages |
| NLTK punkt | Apache-2.0 | heavy | pretrained Spanish model |
| spaCy sentencizer | MIT | heavy install | rule-based on punctuation; too heavy for us |

pySBD and syntok rules cover only standard punctuation. Fiction-specific Spanish rules need our own code:
- Abbreviations: Sr., Sra., Dr., Ud., etc., pág., EE. UU.
- Ordinals: 1.º
- Opening ¿¡ marks a sentence start.
- Ellipsis "…", which may or may not end a sentence.
- «» and nested “” quotes.
- Raya dialogue: "—Hola —dijo Juan—. ¿Vienes?" The narrator's tag inside rayas does not split; ".", "?" or "!" after a closing raya does.

A stdlib rule engine (regex plus abbreviation lists plus a raya state machine) is very reasonable, at about 300–500 LOC with golden tests.

Risk/decision for us: write our own stdlib segmenter with pluggable per-language rule tables. Port the pragmatic_segmenter/pySBD Spanish abbreviation lists and golden rules, which MIT allows with attribution. Optionally use pySBD as a cross-check in tests.
Sources: https://github.com/nipunsadvilkar/pySBD · https://arxiv.org/abs/2010.09657 · https://github.com/fnl/syntok · https://github.com/fnl/segtok · https://www.nltk.org/api/nltk.tokenize.punkt.html · https://www.rae.es/dpd/raya

### Q4.7 Existing overlapping projects
- **Office-Word-MCP-Server (GongRzhe, MIT, python-docx):** about 2.1k stars, but archived since Mar 3, 2026. Comments are read-only and it has no tracked changes.
- **Safe Docx (UseJunior, Apache-2.0, TypeScript/npm):** local MCP server plus CLI. It produces clean or tracked output, compares documents, and its README has a Spanish translation. It is the closest competitor; its anchoring model is undocumented.
- **SecurityRonin/docx-mcp (MIT, Python):** writes `w:ins`/`w:del` and comments. Small project.
- **word-mcp-live (MIT):** drives live Word via COM on Windows and JXA on macOS.
- **docx-revisions (MIT, extends python-docx, v0.1.5 Apr 2026):** tracked insertions and deletions, accept/reject.
- **python-redlines (MIT):** two-document comparison to a redline. Its engine is an embedded .NET binary.
- **Anthropic docx skill:** proprietary; raw XML plus scripts.
- **Copilot in Word (2026):** agent mode, tracked changes by default, comment threads. It is closed, Microsoft 365-only and has no external API for agents.

Gaps none of these fill: stdlib-only core, stable quote anchors, language-aware segmentation, a versioned JSON contract for downstream tools, and runs in all three Claude surfaces.

Risk/decision for us: differentiate on anchors, the contract and segmentation. Study Safe Docx and docx-revisions (both permissive) for OOXML edge cases.
Sources: https://github.com/GongRzhe/Office-Word-MCP-Server · https://github.com/UseJunior/safe-docx · https://enterprisedna.co/directories/mcp/securityronin-docx-mcp · https://socket.dev/pypi/package/word-mcp-live · https://pypi.org/project/docx-revisions/ · https://github.com/JSv4/Python-Redlines · https://the-decoder.com/microsoft-copilot-in-word-can-now-track-changes-and-manage-comments/

### Q4.8 License choice
| License | Adoption | Patents | Borrowing code | Fit for us |
|---|---|---|---|---|
| MIT | widest | no explicit patent grant | can vendor MIT code (keep notices); cannot vendor Apache-2.0 code without carrying Apache terms (in practice the whole project goes Apache-compatible) | ok |
| Apache-2.0 | broad, incl. corporate | explicit grant plus patent-retaliation clause | can take both MIT and Apache code (Safe Docx, Hypothesis client is BSD-2, NLTK); GPLv3-compatible though not GPLv2-compatible; costs a NOTICE file and stated changes | best |
| MPL-2.0 | narrower | — | file-level copyleft would complicate vendoring | adds friction |
| GPL/AGPL | low; downstream toolkits and corporate users won't adopt | — | — | worst |

Risk/decision for us: **Apache-2.0**. It allows borrowing from both MIT and Apache code and gives users a patent grant. Keep a THIRD_PARTY_NOTICES file, and never vendor Anthropic's proprietary skill code or AGPL engines (e.g. jubarte-redlines).
Sources: https://www.apache.org/licenses/LICENSE-2.0 · https://www.apache.org/legal/resolved.html · https://www.gnu.org/licenses/license-list.html#apache2 · https://choosealicense.com/licenses/ · https://libraries.io/pypi/jubarte-redlines
