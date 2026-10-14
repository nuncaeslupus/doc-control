# Doc Control — Specification (annotated edition)

> Generated 2026-10-14. This is the document with a **note slot** after every section that has text of its own. Read it in any Markdown app. To annotate, replace the `_(your notes…)_` placeholder under any section. When done, send the file back — notes are acted on.

---

# Spec

## Preamble & scope

**Date**: 2026-10-14
**Ticket / PR**: — (workspace `PACKAGE`; answers literary-critic Q-2, unblocks their T6)
**Author**: Claude (doc-control session), for the owner
**Revision**: 2
**Status**: approved (2026-10-14, revision 2)
**Revision log**:
- r1 — first draft; the owner answered "yes, write it" to literary-critic's Q-2
- r2 — owner's answers of 2026-10-14 applied (chat; no notes export): Option B chosen, Windows only for the first release, unsigned, two channels (D-P1..D-P5)

> Review record. Each round of reviewer notes makes a new revision: bump
> **Revision**, add a log line naming the export it applied, and commit that
> export beside this file in the same commit. On approval set **Status** to
> `approved (YYYY-MM-DD, revision N)` and, when the reviewer sent no notes for
> that revision, add `— without annotations`.

<!-- -->

> **✎ Notes** · `SPEC · intro`
> _(your notes here — replace this line)_

## §1 Problem statement

literary-critic asked (Q-2): can the live engine be packaged so a writer installs it with a double-click, with no Python, on Windows and Mac? The owner said yes. Today a writer needs Python, `uv` or `pip`, and the `doc-control[live]` extra to drive Word. That blocks non-technical writers, and it blocks literary-critic's task T6.

Facts from the code:

- `live/word_com.py` imports only the standard library at module level. It loads `pythoncom` and `win32com.client` (from `pywin32`) with `importlib.import_module` on the first live call, and reads the registry through `winreg`. A freezer cannot see those imports, so they must be named explicitly.
- `live/lo_uno.py` is also standard library (`ctypes`, `socket`, `subprocess`). It imports `uno` lazily. `uno` ships with LibreOffice, not PyPI, and needs the Python build that LibreOffice names (`libpyuno`). A frozen binary cannot load it from its own bundled Python.
- Word on Mac has no COM. The Mac live engine is therefore LibreOffice only.
- The core has no dependencies (`dependencies = []`). The MCP server is `doc-control mcp` over stdio; the CLI is `doc-control`.
- Releases go to PyPI from `.github/workflows/release.yml`. CI already runs on `ubuntu-latest` and `windows-latest`.

What should be true: a writer downloads one file, double-clicks, and Claude Desktop (or the command line) can use doc-control. The first release is Windows only (D-P2) and gives Word live control. Mac (the file engine, the LibreOffice bridge, and Word for Mac) moves to later phases and is out of scope here; the analysis of it is kept below for those phases.

Code signing is not required, and the first release ships unsigned (D-P3). The Windows warning can be passed: SmartScreen offers "More info → Run anyway", and the README documents that step. (For the later Mac phase, the equivalent is "Privacy & Security → Open Anyway".) What signing would need is in section 3, for a future decision.

**Success criteria (measurable)**:

- [ ] `frozen_smoke_failures == 0` on `windows-latest`: the frozen binary runs `capabilities`, a file-engine `find` on a fixture `.docx`, and an MCP `initialize` plus `tools/list` over stdio, with no Python on `PATH`.
- [ ] `live_word_failures == 0` for the frozen Windows exe: `scripts/live_word_check.py` run against it on the owner's Windows machine (manual evidence).
- [ ] `install_steps_windows <= 2` (download, double-click) for the `.mcpb` bundle and `<= 3` for the plain installer, plus the documented SmartScreen "More info → Run anyway" step. Counted from a clean machine, recorded in the plan's evidence log.
- [ ] `frozen_exe_size_mb <= 40` for the Windows build (measured on the CI artifact).
- [ ] `core_runtime_dependencies == 0`: `pyproject.toml` `dependencies` stays empty. Packaging tools appear only in a build dependency group and the workflow.
- [ ] `release_artifacts_per_tag == expected`: a tagged release attaches the Windows exe, the installer and the `.mcpb` file, each with a SHA-256 in the release notes (GitHub Release assets, D-P5). Mac artifacts are not expected until their phase.
- [ ] `directory_submission_ready == true`: the `.mcpb` passes the manifest validator, and the listing checklist in section 3 ("Claude Desktop directory listing") is complete: manifest fields, icon, privacy and tool descriptions, review submission. Whether the directory accepts it is judged at submission, not measured (D-P5).
- [ ] `frozen_vs_python_result_diffs == 0` on the shared fixture corpus: `capabilities` and `find` output of the Windows binary equals the pip-installed version.
- [ ] Judged, not measured: a writer with no Python installs it from the README alone and Claude Desktop lists the doc-control tools.

<!-- -->

> **✎ Notes** · `SPEC §1`
> _(your notes here — replace this line)_

## §2 Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| `live/word_com.py` | Primary (Windows) | Drives Word over COM | Small | Freezer hints for `pythoncom`, `win32com.client`, `winreg`, `pywintypes`; frozen error text must not say "pip install" | Med |
| `live/lo_uno.py` | Primary (later phase: Mac, Windows, Linux) | Drives LibreOffice | Not in the first release | Cannot import `uno` from a frozen interpreter; needs a bridge (phase 2) | High |
| `cli.py`, `mcp.py`, `__main__.py` | Dependent | Entry points | Small | One frozen entry point serving `mcp` and CLI | Low |
| New `packaging/` (spec file, `.mcpb` manifest, icons, installer script) | Primary | Build inputs | Yes (new) | Build-time only | Med |
| `.github/workflows/` (new `package.yml`) | Infrastructure | Build, smoke test, attach artifacts | Yes | Runs on `windows-latest`; `macos-latest` joins in phase 2 | Med |
| `release.yml` | Dependent | PyPI release on a version bump | Maybe | Packages build for the same tag | Low |
| README, `docs/` | Docs | Install instructions | Yes | Screenshots-free numbered steps | Low |
| Claude Desktop | Consumer | Installs `.mcpb`, runs the binary; extensions directory lists it | No | Type `binary` manifest; directory submission (D-P5) | Med |
| literary-critic | Consumer | Task T6 waits on this | No | Unblocked at phase 1 | Med |
| Word (Windows), LibreOffice (Mac) | Shared resources | The apps driven | No | Must be installed by the writer | Low |

**Impact**:
- **User**: a writer installs without Python. The first run shows an OS warning (unsigned).
- **Operational**: one more CI job (a second for Mac in phase 2) and a release step. Binaries are rebuilt per release. A directory listing adds a review submission per release to confirm.
- **Performance**: a one-file exe unpacks on start; expect 1–3 seconds of startup. A one-folder build avoids it.
- **API and data**: none.
- **Risk of inaction**: literary-critic's writers must install Python or use computer-use clicks. T6 stays blocked.

<!-- -->

> **✎ Notes** · `SPEC §2`
> _(your notes here — replace this line)_

## §3 Options

### Phases (shared by the options)

1. **Phase 1, Windows (the first release, in scope)**: frozen exe (PyInstaller or Nuitka) with `pywin32`, the `.mcpb` bundle (server type `binary`), a plain installer, CI smoke test, GitHub Release assets and the Claude Desktop directory listing.
2. **Phase 2, Mac file engine (later, out of scope here)**: a frozen Mac binary (Apple silicon and Intel, or universal2), `.mcpb` for Mac, "Open Anyway" instructions. Live control is LibreOffice only, with the bridge below.
3. **Phase 3, Mac Word (later, out of scope here)**: a new Word-for-Mac engine through AppleScript or JXA (`osascript`). It is a separate spec.

The owner moved Mac to later phases (D-P2). The Mac analysis below is kept so those phases start from it.

<!-- -->

> **✎ Notes** · `SPEC › Phases (shared by the options)`
> _(your notes here — replace this line)_

### Option A: Windows frozen exe plus .mcpb, Mac file engine only (Conservative)

- **Description**: build one exe with PyInstaller (one-file or one-folder). Bundle `pywin32`'s `pythoncom`, `win32com` and `pywintypes` via hidden imports and a hook. Ship a `.mcpb` with `"server": {"type": "binary"}` that points to the exe, a plain installer (NSIS or Inno Setup) that puts it on `PATH`, and a zip for CLI use. On Mac, ship a frozen binary of the file engine; `lo_uno` reports `ENGINE_UNAVAILABLE` unless the writer has a Python with `uno`.
- **Scope**: `packaging/`, workflow, README, small frozen-mode text changes in `word_com.py`.
- **Effort**: Medium.
- **Tradeoffs**: unblocks Windows writers, the main literary-critic audience. Mac live control is not offered. Fastest to evidence.
- **Compatibility**: additive. The PyPI package is unchanged.

<!-- -->

> **✎ Notes** · `SPEC › Option A: Windows frozen exe plus .mcpb, Mac file engine only (Conservative)`
> _(your notes here — replace this line)_

### Option B: Windows first, then Mac with LibreOffice live control through its own Python (Recommended; chosen by the owner)

- **Description**: phase 1 is Option A for Windows only. The design then plans, for later phases, Mac live control through LibreOffice. The frozen binary cannot import `uno`, so a small helper script runs in LibreOffice's bundled Python (`/Applications/LibreOffice.app/Contents/Resources/python`) and speaks the same protocol over a local socket or stdio. The frozen binary starts it when `uno` is not importable. The same route works on Windows and Linux for LibreOffice users. The file engine and MCP server stay in the frozen binary.
- **Scope**: the first release is the Windows part of Option A. Later phases add a `lo_uno` bridge process (`live/lo_bridge.py`, standard library only, runs under LibreOffice's Python), tests with a fake bridge, and Mac CI smoke.
- **Effort**: Medium–Large.
- **Tradeoffs**: the first release carries no Mac or bridge work, so it ships sooner. Once the later phases land, Mac writers get live control with LibreOffice and no Python. The bridge adds a process and a protocol to keep in step with `live/protocol.py`. It does not give Word on Mac (phase 3).
- **Compatibility**: additive. With a Python that already has `uno`, today's in-process path wins.

<!-- -->

> **✎ Notes** · `SPEC › Option B: Windows first, then Mac with LibreOffice live control through its own Python (Recommended; chosen by the owner)`
> _(your notes here — replace this line)_

### Option C: Run the whole server inside LibreOffice's Python (Mac and Windows)

- **Description**: do not freeze on Mac. The installer drops the doc-control wheel's source into a folder and launches `doc-control mcp` with LibreOffice's own interpreter.
- **Scope**: installer scripts, an `.mcpb` with a launcher.
- **Effort**: Medium.
- **Tradeoffs**: no freezer for Mac and `uno` just works. But it requires LibreOffice for any use, even file edits, which hurts writers who only want the file engine; the launcher is fragile across LibreOffice versions. The `.mcpb` type would be `binary` pointing at a shell launcher, which needs execute bits and is harder to sign.
- **Compatibility**: additive.

<!-- -->

> **✎ Notes** · `SPEC › Option C: Run the whole server inside LibreOffice's Python (Mac and Windows)`
> _(your notes here — replace this line)_

### Packaging details

- **Windows exe**: PyInstaller is the default. It has mature `pywin32` hooks. Nuitka gives smaller, faster output but takes longer to build and needs a C compiler on the runner. Decide by measurement in phase 1 (size, startup, smoke test). Antivirus false positives are more common with one-file PyInstaller; one-folder or Nuitka reduces them.
- **`.mcpb`**: a zip with `manifest.json` (server type `binary`, `entry_point` the exe, `mcp_config` command and args `mcp`) and the binary. Claude Desktop installs it by double-click. Windows and (in phase 2) Mac get separate bundles because the binaries differ.
- **Plain installer**: Windows `.exe` installer (Inno Setup) copies the exe, adds it to `PATH` and prints the `claude_desktop_config.json` snippet. Mac gets a `.pkg` or a signed-less zip with `doc-control` to drop on `/usr/local/bin`.
- **CLI use**: the same binary. `doc-control capabilities` works from a terminal.
- **Mac Word**: AppleScript/JXA through `osascript` is the route to Word on Mac. Phase 3, its own spec.
- **Signing**: not required; the first release ships unsigned and documents the SmartScreen "More info → Run anyway" step (D-P3). See "What signing would need" below. Signing can be added later without changing the build, by signing in the workflow when secrets exist. (Mac, for its phase: "Privacy & Security → Open Anyway"; the Apple Developer Program is a yearly fee and adds notarization.)
- **CI**: a `package.yml` workflow on `windows-latest` (phase 2 adds `macos-latest`, and `macos-13` for Intel, if needed). Build, run the smoke test on the built binary with `PATH` stripped of Python, upload artifacts, and on a release tag attach them to the GitHub release with checksums. Actions pins follow the existing workflows (commit SHAs).
- **Size**: the standard-library-only core freezes small. Budget 40 MB for the Windows build; `pywin32` on Windows is the largest part.
- **Stdlib-only rule**: PyInstaller or Nuitka, hooks and installer tools are build-time only. They go in a `package` dependency group, never in `[project] dependencies`. The runtime stays `dependencies = []`; `pywin32` stays the `live` extra for pip users and is bundled in the Windows build only.

<!-- -->

> **✎ Notes** · `SPEC › Packaging details`
> _(your notes here — replace this line)_

### What signing would need (for a future decision)

Not part of the first release (D-P3). Recorded so the choice can be made later without new research. Prices and policies below move; confirm each before deciding.

- **Azure Trusted Signing** (Microsoft's managed signing service): about $10 a month. It needs identity validation of an individual or an organisation, and eligibility depends on region and account type (to confirm). It signs from GitHub Actions with a service principal, with no hardware token to handle. Likely the cheapest path.
- **OV and EV code-signing certificates from a certificate authority**: roughly a few hundred dollars a year, EV costing more than OV (to confirm with the CA). Since 2023 the private key must live on a hardware token or in an HSM or cloud HSM, so a file-based `.pfx` secret in CI is not allowed; the CA's cloud-signing offer or a token on a self-hosted runner is needed.
- **`signtool` in CI**: the workflow signs the exe (and the installer) after the build with `signtool sign` and a timestamp server, then verifies with `signtool verify`, before the smoke test and the release step. The `.mcpb` bundle may need its own signing step (to confirm).
- **SmartScreen reputation builds over time**, even for signed OV binaries: a new signed file can still warn until enough downloads accumulate. Signing changes the warning from "unknown publisher" to a named publisher; it does not remove the warning on day one.

<!-- -->

> **✎ Notes** · `SPEC › What signing would need (for a future decision)`
> _(your notes here — replace this line)_

### Claude Desktop directory listing (task-level requirement, D-P5)

Publishing to the Claude Desktop extensions directory, in addition to GitHub Release assets, needs the following. Anything the directory's own rules decide is marked "to confirm at submission"; this spec does not state exact policy.

- A valid `manifest.json` in the `.mcpb`: name, display name, version, description, author, server type `binary` with `entry_point` and `mcp_config`, the tool list, and the compatibility platforms (`win32` only for now). Exact required fields: to confirm at submission against the current manifest schema.
- An icon in the format and size the directory asks for (to confirm at submission).
- A privacy statement: what the tool reads and writes (local Word documents, nothing sent off the machine by doc-control itself), and a privacy policy link if the directory requires one (to confirm at submission).
- Tool descriptions: a plain-language description of each tool (read, edit, live control of Word) and whether it changes files, for the listing and the review.
- The review submission: the submission route, its review criteria, turnaround and re-review on each new version are all to confirm at submission. Plan for a listing that lags the GitHub release.
- The listing must not block the GitHub release: the release assets ship first (D-P5).

<!-- -->

> **✎ Notes** · `SPEC › Claude Desktop directory listing (task-level requirement, D-P5)`
> _(your notes here — replace this line)_

### Comparison

| | Option A | Option B | Option C |
|---|---|---|---|
| Effort | Medium | Medium–Large | Medium |
| Risk | Low | Medium (bridge process) | Medium (fragile launcher) |
| Windows live (Word) | Yes | Yes | Needs LibreOffice |
| Mac live | No | LibreOffice | LibreOffice |
| Mac file engine without LibreOffice | Yes | Yes | No |
| Compatibility | Additive | Additive | Additive |
| Maintenance | Low | Medium (bridge + protocol) | Medium (launcher per LO version) |

<!-- -->

> **✎ Notes** · `SPEC › Comparison`
> _(your notes here — replace this line)_

## §4 Recommendation

**Recommended option**: Option B, built in phases and chosen by the owner (D-P1). Phase 1 (Windows, Option A) is the whole first release: it delivers what literary-critic needs for T6, ships unsigned, and goes out as GitHub Release assets and a Claude Desktop directory listing. Phase 2 (the Mac binary and the LibreOffice bridge) and phase 3 (Word on Mac, a separate spec) come later and are out of scope here.

**Immediate next action**: after approval, `design` the plan, starting with a throwaway PyInstaller build in CI on `windows-latest` that runs the smoke test (`capabilities`), to settle PyInstaller against Nuitka by measured size and startup.

**Open questions**:
- [ ] Q1. PyInstaller or Nuitka for Windows? Not an owner question: the plan settles it by measurement in phase 1. Recommended: start with PyInstaller one-folder; switch only if antivirus flags or size miss the 40 MB budget.
- [x] Q2. Is Mac in scope for the first release? No: Windows only; Mac moves to later phases (D-P2).
- [x] Q3. Is shipping unsigned acceptable for the first release? Yes; document SmartScreen "More info → Run anyway" (D-P3).
- [x] Q4. Is the LibreOffice bridge on Mac part of this spec? Deferred with Mac to later phases; the analysis stays (D-P4).
- [x] Q5. Distribution channel? Both: GitHub Release assets and a Claude Desktop directory listing (D-P5).

**Decisions log**:

| ID | Decision | Status | Date |
|----|----------|--------|------|
| D-P1 | Option B, built in phases | agreed | 2026-10-14 |
| D-P2 | Windows only for the first release. Mac (the LibreOffice bridge, and Word for Mac) moves to later phases and is out of scope; the analysis is kept | agreed | 2026-10-14 |
| D-P3 | Ship unsigned; document the SmartScreen "More info → Run anyway" step. "What signing would need" is recorded for a future decision | agreed | 2026-10-14 |
| D-P4 | The LibreOffice bridge follows Mac to a later phase (consequence of D-P2) | agreed | 2026-10-14 |
| D-P5 | Publish as GitHub Release assets and as a listing in the Claude Desktop extensions directory; the listing requirements are a task-level requirement, policy details to confirm at submission | agreed | 2026-10-14 |

Constraints carried: no signing required; the core stays stdlib-only; the owner answered Q-2 with yes.


> Sections 5–6 (contracts, risks) are appended by `design`.

<!-- -->

> **✎ Notes** · `SPEC §4`
> _(your notes here — replace this line)_

