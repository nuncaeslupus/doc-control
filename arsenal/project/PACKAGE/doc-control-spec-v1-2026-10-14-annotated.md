# Doc Control — Specification (annotated edition)

> Generated 2026-10-14. This is the document with a **note slot** after every section that has text of its own. Read it in any Markdown app. To annotate, replace the `_(your notes…)_` placeholder under any section. When done, send the file back — notes are acted on.

---

# Spec

## Preamble & scope

**Date**: 2026-10-14
**Ticket / PR**: — (workspace `PACKAGE`; answers literary-critic Q-2, unblocks their T6)
**Author**: Claude (doc-control session), for the owner
**Revision**: 1
**Status**: draft
**Revision log**:
- r1 — first draft; the owner answered "yes, write it" to literary-critic's Q-2

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

What should be true: a writer downloads one file, double-clicks, and Claude Desktop (or the command line) can use doc-control. Windows gets Word live control. Mac gets the file engine and, as far as the platform allows, live control.

Code signing is not required. Unsigned installs show a warning that the user can pass: on Windows, SmartScreen offers "More info → Run anyway". On macOS, "Privacy & Security → Open Anyway". Paid signing (Windows certificate; Apple Developer Program, notarization) removes those warnings and is optional.

**Success criteria (measurable)**:

- [ ] `frozen_smoke_failures == 0` on `windows-latest` and `macos-latest`: the frozen binary runs `capabilities`, a file-engine `find` on a fixture `.docx`, and an MCP `initialize` plus `tools/list` over stdio, with no Python on `PATH`.
- [ ] `live_word_failures == 0` for the frozen Windows exe: `scripts/live_word_check.py` run against it on the owner's Windows machine (manual evidence).
- [ ] `install_steps_windows <= 2` (download, double-click) for the `.mcpb` bundle and `<= 3` for the plain installer; `install_steps_mac <= 3` including "Open Anyway". Counted from a clean machine, recorded in the plan's evidence log.
- [ ] `frozen_exe_size_mb <= 40` per OS (measured on the CI artifact).
- [ ] `core_runtime_dependencies == 0`: `pyproject.toml` `dependencies` stays empty. Packaging tools appear only in a build dependency group and the workflow.
- [ ] `release_artifacts_per_tag == expected`: a tagged release attaches the Windows exe, the Mac binary, and the `.mcpb` file(s), each with a SHA-256 in the release notes.
- [ ] `frozen_vs_python_result_diffs == 0` on the shared fixture corpus: `capabilities` and `find` output of the binary equals the pip-installed version.
- [ ] Judged, not measured: a writer with no Python installs it from the README alone and Claude Desktop lists the doc-control tools.

<!-- -->

> **✎ Notes** · `SPEC §1`
> _(your notes here — replace this line)_

## §2 Systems & Impact

| System | Type | Role | Needs changes? | Impact | Severity |
|--------|------|------|----------------|--------|----------|
| `live/word_com.py` | Primary (Windows) | Drives Word over COM | Small | Freezer hints for `pythoncom`, `win32com.client`, `winreg`, `pywintypes`; frozen error text must not say "pip install" | Med |
| `live/lo_uno.py` | Primary (Mac, Windows, Linux) | Drives LibreOffice | Option B/C: Yes | Cannot import `uno` from a frozen interpreter; needs a bridge | High |
| `cli.py`, `mcp.py`, `__main__.py` | Dependent | Entry points | Small | One frozen entry point serving `mcp` and CLI | Low |
| New `packaging/` (spec file, `.mcpb` manifest, icons, installer script) | Primary | Build inputs | Yes (new) | Build-time only | Med |
| `.github/workflows/` (new `package.yml`) | Infrastructure | Build, smoke test, attach artifacts | Yes | Runs on `windows-latest` and `macos-latest` | Med |
| `release.yml` | Dependent | PyPI release on a version bump | Maybe | Packages build for the same tag | Low |
| README, `docs/` | Docs | Install instructions | Yes | Screenshots-free numbered steps | Low |
| Claude Desktop | Consumer | Installs `.mcpb`, runs the binary | No | Type `binary` manifest | Med |
| literary-critic | Consumer | Task T6 waits on this | No | Unblocked at phase 1 | Med |
| Word (Windows), LibreOffice (Mac) | Shared resources | The apps driven | No | Must be installed by the writer | Low |

**Impact**:
- **User**: a writer installs without Python. The first run shows an OS warning (unsigned).
- **Operational**: two more CI jobs and a release step. Binaries are rebuilt per release.
- **Performance**: a one-file exe unpacks on start; expect 1–3 seconds of startup. A one-folder build avoids it.
- **API and data**: none.
- **Risk of inaction**: literary-critic's writers must install Python or use computer-use clicks. T6 stays blocked.

<!-- -->

> **✎ Notes** · `SPEC §2`
> _(your notes here — replace this line)_

## §3 Options

### Phases (shared by the options)

1. **Phase 1, Windows**: frozen exe (PyInstaller or Nuitka) with `pywin32`, the `.mcpb` bundle (server type `binary`), a plain installer, CI smoke test.
2. **Phase 2, Mac file engine**: a frozen Mac binary (Apple silicon and Intel, or universal2), `.mcpb` for Mac, "Open Anyway" instructions. Live control is LibreOffice only, with the bridge below.
3. **Phase 3, Mac Word**: a new Word-for-Mac engine through AppleScript or JXA (`osascript`). It is a separate spec and not part of this one.

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

### Option B: A and B-phases with LibreOffice live control on Mac through its own Python (Recommended)

- **Description**: everything in Option A, plus Mac live control through LibreOffice. The frozen binary cannot import `uno`, so a small helper script runs in LibreOffice's bundled Python (`/Applications/LibreOffice.app/Contents/Resources/python`) and speaks the same protocol over a local socket or stdio. The frozen binary starts it when `uno` is not importable. The same route works on Windows and Linux for LibreOffice users. The file engine and MCP server stay in the frozen binary.
- **Scope**: Option A plus a `lo_uno` bridge process (`live/lo_bridge.py`, standard library only, runs under LibreOffice's Python), tests with a fake bridge, and Mac CI smoke.
- **Effort**: Medium–Large.
- **Tradeoffs**: Mac writers get live control with LibreOffice and no Python. Adds a process and a protocol to keep in step with `live/protocol.py`. It does not give Word on Mac (phase 3).
- **Compatibility**: additive. With a Python that already has `uno`, today's in-process path wins.

<!-- -->

> **✎ Notes** · `SPEC › Option B: A and B-phases with LibreOffice live control on Mac through its own Python (Recommended)`
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
- **`.mcpb`**: a zip with `manifest.json` (server type `binary`, `entry_point` the exe, `mcp_config` command and args `mcp`) and the binary. Claude Desktop installs it by double-click. Windows and Mac get separate bundles because the binaries differ.
- **Plain installer**: Windows `.exe` installer (Inno Setup) copies the exe, adds it to `PATH` and prints the `claude_desktop_config.json` snippet. Mac gets a `.pkg` or a signed-less zip with `doc-control` to drop on `/usr/local/bin`.
- **CLI use**: the same binary. `doc-control capabilities` works from a terminal.
- **Mac Word**: AppleScript/JXA through `osascript` is the route to Word on Mac. Phase 3, its own spec.
- **Signing**: not required. Unsigned downloads show SmartScreen "More info → Run anyway" on Windows and "Privacy & Security → Open Anyway" on macOS. Windows code signing costs a yearly certificate (cloud signing services are cheaper than hardware tokens); Apple's Developer Program is a yearly fee and adds notarization. Both remove the warnings. Add them later without changing the build, by signing in the workflow when secrets exist.
- **CI**: a `package.yml` workflow on `windows-latest` and `macos-latest` (and `macos-13` for Intel, if needed). Build, run the smoke test on the built binary with `PATH` stripped of Python, upload artifacts, and on a release tag attach them to the GitHub release with checksums. Actions pins follow the existing workflows (commit SHAs).
- **Size**: the standard-library-only core freezes small. Budget 40 MB per OS; `pywin32` on Windows is the largest part.
- **Stdlib-only rule**: PyInstaller or Nuitka, hooks and installer tools are build-time only. They go in a `package` dependency group, never in `[project] dependencies`. The runtime stays `dependencies = []`; `pywin32` stays the `live` extra for pip users and is bundled in the Windows build only.

<!-- -->

> **✎ Notes** · `SPEC › Packaging details`
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

**Recommended option**: Option B, built in phases. Phase 1 (Windows, Option A) delivers what literary-critic needs for T6 and can ship alone. Phase 2 adds the Mac binary and the LibreOffice bridge. Phase 3 (Word on Mac) stays a separate spec.

**Immediate next action**: after approval, `design` the plan, starting with a throwaway PyInstaller build in CI on `windows-latest` that runs the smoke test (`capabilities`), to settle PyInstaller against Nuitka by measured size and startup.

**Open questions**:
- [ ] Q1. PyInstaller or Nuitka for Windows? Recommended: start with PyInstaller one-folder; switch only if antivirus flags or size miss the 40 MB budget.
- [ ] Q2. Is Mac in scope for the first release, or Windows only (Option A) with Mac after? Recommended: Windows first, Mac in the same plan as phase 2.
- [ ] Q3. Is shipping unsigned acceptable for the first release? Recommended: yes; revisit signing if writers drop out at the warning.
- [ ] Q4. Should the LibreOffice bridge on Mac be part of this spec, or deferred with Word-for-Mac? Recommended: include it (Option B).
- [ ] Q5. Which distribution channel: GitHub release assets only, or also a listing in Claude Desktop's extension directory? Recommended: GitHub release assets first.

**Decisions log**: none (the work did not start in `explore-idea`). Constraints carried: no signing required; the core stays stdlib-only; the owner answered Q-2 with yes.


> Sections 5–6 (contracts, risks) are appended by `design`.

<!-- -->

> **✎ Notes** · `SPEC §4`
> _(your notes here — replace this line)_

