# Plan: double-click install without Python

**Date**: 2026-10-14
**Specification**: `arsenal/project/PACKAGE/spec.md` (approved r3; contracts §5, risks §6)
**Author**: Claude (doc-control session), for the owner
**Revision**: 2
**Status**: approved (2026-10-15, revision 2) — without annotations
**Revision log**:
- r1 — first draft
- r2 — spec r3 applied (owner's chat answers of 2026-10-15, no notes export): T10 dropped, T11-T14 added

> Review record, kept the way the spec keeps its own: each round of notes bumps
> **Revision** with a log line naming the `<repo>-plan-v<N>-<date>-notes.md`
> export it applied, committed beside this file in the same commit. Approval is
> `approved (YYYY-MM-DD, revision N)`, plus `— without annotations` when there
> were none.

---

## Technical solution

### Architecture overview

This plan covers phase 1 only: Windows, unsigned, published as GitHub Release assets (D-P1, D-P2, D-P3, D-P5), with an opt-in update notice, a winget manifest and an installer that hands the `.mcpb` to Claude Desktop (spec r3: D-P7, D-P8, D-P9). It adds a build around the unchanged package, in four layers:

1. **Frozen build** (`packaging/`): one entry script that calls `doc_control.cli:main`, plus the freezer's build file with hidden imports for `pywin32` and the `ops` modules the registry loads by name. The output is a one-folder build, `doc-control/doc-control.exe`. T1 measures PyInstaller against Nuitka and picks one (Q1).
2. **Distributions** made from that folder:
   - a zip;
   - an Inno Setup per-user installer;
   - a `.mcpb` bundle (server type `binary`) whose tool list comes from the registry.
3. **Checks that drive the exe from outside**, never in-process:
   - `frozen_smoke.py` (CLI and MCP, with Python stripped from `PATH`);
   - `frozen_parity.py` (exe output against the pip install);
   - `live_word_frozen_check.py`, which runs the `word_suite` scenarios through an MCP client talking to the exe.
4. **Workflows**: `package.yml` is a reusable workflow (`on: workflow_call`) on `windows-latest`. `ci.yml` calls it on pull requests that touch packaging inputs; `release.yml` calls it in the same run that creates the tag, then attaches the assets. A separate tag-triggered workflow would never fire, because `release.yml` creates the tag with `GITHUB_TOKEN`.

### Data flow

- **Pull request**: `ci.yml` → `package.yml` (build → size check → smoke → parity → zip, installer, `.mcpb` → `mcpb validate` → upload artifact `windows-packages`).
- **Release**:
  1. `release.yml` `build` decides `release=true` and publishes to PyPI as today.
  2. Its `package` job calls `package.yml` with `version` and `attach: true`.
  3. `tag` creates `vX.Y.Z` and the GitHub release (branch path only).
  4. `attach` downloads `windows-packages`, creates the release if it is missing (hand-pushed tag path), uploads the four assets with `--clobber`, and writes `## Checksums` into the notes.
  5. `check_release_assets.py --tag vX.Y.Z` confirms them.
- **Directory listing**: dropped in spec r3 (D-P5): the directory no longer accepts `.mcpb` local servers, so T10 (listing readiness) is gone from the table; its task file is archived as `arsenal/tasks/_history/t-3a9a949d.md`. Updates reach users through the opt-in notice (T11) and winget (T12).

### State changes

| Service | Database | Change | Description |
|---------|----------|--------|-------------|
| GitHub Releases | — | UPDATE | Four assets and a `## Checksums` section per tag |
| Actions artifacts | — | CREATE | `windows-packages` per run (default retention) |
| Writer's machine | — | CREATE | Installer: `%LOCALAPPDATA%\Programs\doc-control` and a user `PATH` entry, removed by the uninstaller. `.mcpb`: Claude Desktop's own extension folder |

### Technology choices

| Choice | Justification |
|--------|--------------|
| PyInstaller or Nuitka, settled by T1's measurements | Q1. PyInstaller has mature `pywin32` hooks; Nuitka can be smaller and faster but needs a C compiler and longer builds. Decided by size, start-up and smoke results, not by preference |
| One-folder build, not one-file | No unpack on each start, fewer antivirus flags; the zip and `.mcpb` carry the folder anyway |
| Inno Setup, per-user | Free, scriptable from CI, installs without admin rights; NSIS would also work but adds nothing here |
| `@anthropic-ai/mcpb` CLI, pinned, in CI only | The arbiter of the manifest schema; not a Python dependency |
| Reusable workflow (`workflow_call`) | One build definition for PRs and releases; runs inside `release.yml`'s run, which the `GITHUB_TOKEN` rule cannot block |
| `package` dependency group | Keeps freezer tools out of `[project] dependencies` (`core_runtime_dependencies == 0`) |

### Out of scope

- Mac in every form: the frozen Mac binary, the LibreOffice bridge (`live/lo_bridge.py`), Word for Mac (D-P2, D-P4).
- LibreOffice live control from the frozen binary (`uno` cannot load in it; that is the phase 2 bridge).
- Code signing (D-P3). "What signing would need" in the spec stays the reference.
- Auto-update inside the app.
- Any change to the PyPI package's behaviour, apart from the frozen-only error text and the additive `capabilities.build` object.

---

## Implementation tasks

Every task builds or tests Windows artifacts on `windows-latest` in CI (`surface: windows-ci`). Tasks marked `manual` also need evidence from the owner's Windows machine.

| T# | Description | Service | Size | Depends | Gate | Tests |
|----|-------------|---------|------|---------|------|-------|
| T1 | Q1 spike, measured: a throwaway `workflow_dispatch` job `.github/workflows/freeze-spike.yml` on `windows-latest` builds a one-folder exe with PyInstaller and with Nuitka; `packaging/spike/measure.py` records for each `dist_mb`, `startup_ms_median` (10 runs of `capabilities`), `smoke_failures` (`capabilities`, `tools/list`) and build minutes; the numbers go in the evidence log and the choice in the spec's decisions log as D-P6; the spike workflow is deleted in the same PR (surface: windows-ci) | packaging | S | — | `freezer_candidates_measured >= 2` | `test_measure_reports_every_metric_per_candidate` in `tests/packaging/test_measure.py` — given two fake build folders whose exe is a stub script, the report has `dist_mb`, `startup_ms_median` and `smoke_failures` for each candidate |
| T2 | Frozen build with the T1 freezer: `packaging/entry.py`, the build file (hidden imports `pythoncom`, `pywintypes`, `win32com.client`, `winreg`, every `doc_control.ops.*`; data `recipes/bundled/*.json`; excludes `tkinter` and tests), `package` dependency group, frozen-only `pywin32` error text in `live/word_com.py`, additive `capabilities.build`; `.github/workflows/package.yml` (`workflow_call`) builds and checks the uncompressed folder size, called from `ci.yml` with a paths filter (surface: windows-ci) | packaging, live, ci | M | T1 | `frozen_exe_size_mb <= 40` | `test_word_error_when_frozen_names_no_pip` in `tests/live/test_word_com.py` — with `sys.frozen` set and `win32com` unimportable, the error text has no "pip install"; `test_core_dependencies_stay_empty_with_package_group` in `tests/test_package.py` — `[project] dependencies` is `[]` and the freezer appears only in the `package` group |
| T3 | `scripts/frozen_smoke.py --exe PATH --strip-python-path`: `capabilities`, file-engine `find` on a corpus fixture, MCP `initialize` plus `tools/list` plus one `tools/call` per op family, with every Python directory removed from `PATH`; a step in `package.yml` (surface: windows-ci) | scripts, ci | M | T2 | `frozen_smoke_failures == 0` | `test_frozen_smoke_counts_each_failing_check` in `tests/packaging/test_frozen_smoke.py` — against a stub exe that fails `tools/list`, the script prints `frozen_smoke_failures == 1` and exits 1; `test_strip_python_path_removes_every_python_dir` in the same file — no entry of the child `PATH` holds a `python.exe` |
| T4 | `scripts/frozen_parity.py --exe PATH`: `capabilities` (minus `build`) and `find` over the shared corpus (`tests/corpus/generator.py`, every kind) from the exe against in-process `doc_control`; a step in `package.yml` (surface: windows-ci) | scripts, ci | S | T3 | `frozen_vs_python_result_diffs == 0` | `test_parity_reports_a_changed_find_result` in `tests/packaging/test_frozen_parity.py` — with `python -m doc_control` as the "exe" and one result altered by a stub, the script prints `frozen_vs_python_result_diffs == 1` |
| T5 | Live-Word gate over stdio: a `Runner` in `tests/live/word_suite.py` (`InProcess` default, `McpExe` that spawns `doc-control.exe mcp`, sends `tools/call` and maps error envelopes back to `DocControlError`); every scenario's doc-control call, including the `api.open` reads, goes through it; `scripts/live_word_frozen_check.py --exe PATH` requires no Word running, lets the exe start Word with `live.open`, attaches to that instance for the writer's actions, prints `live_word_failures == N` (surface: windows-ci + manual on the owner's Word) | tests, scripts | M | T3 | `live_word_failures == 0` | `test_mcp_runner_maps_error_envelope_to_doc_control_error` in `tests/live/test_runner.py` — with `python -m doc_control mcp` as the exe, a stale-address call raises `DocControlError` with `STALE_ADDRESS`; `test_word_suite_calls_doc_control_only_through_runner` in the same file — an AST scan finds no `api.run` or `api.open` in `word_suite.py` outside the `InProcess` runner |
| T6 | `.mcpb` bundle: `packaging/mcpb/manifest.json` template and `icon.png`, `scripts/build_mcpb.py` (stdlib `zipfile`; version from `pyproject.toml`; tools from the registry; platforms `win32`); `package.yml` runs the pinned `mcpb validate` and installs the bundle's exe path into the smoke run (surface: windows-ci) | packaging, scripts, ci | M | T3 | `mcpb_manifest_errors == 0` | `test_manifest_tools_equal_mcp_tools_list` in `tests/packaging/test_build_mcpb.py` — the bundle's `tools` names equal `mcp.handle(…, tools/list)` names; `test_manifest_version_matches_pyproject` in the same file — `version` equals `pyproject.toml`'s |
| T7 | Inno Setup installer: `packaging/installer/doc-control.iss` (per-user, `PrivilegesRequired=lowest`, user `PATH` entry, config snippet page, uninstaller); `package.yml` installs pinned Inno Setup, builds it, then silently installs, runs `doc-control capabilities` from a new shell, uninstalls and checks the folder and `PATH` entry are gone (surface: windows-ci) | packaging, ci | M | T3 | `installer_smoke_failures == 0` | `test_iss_version_define_matches_pyproject` in `tests/packaging/test_installer.py` — the build passes `pyproject.toml`'s version as `AppVersion`; `test_iss_installs_per_user_without_admin` in the same file — the script sets `PrivilegesRequired=lowest` and a `{localappdata}` install dir |
| T8 | Release wiring: `release.yml` gains `package` (calls `package.yml` with `attach: true`, `needs: build`, `if: release == 'true'`) and `attach` (`needs: [package, tag]`, runs when `tag` succeeded or was skipped; creates the release if absent; uploads zip, installer, `.mcpb`, `SHA256SUMS.txt` with `--clobber`; writes `## Checksums`); `scripts/check_release_assets.py --tag` (surface: windows-ci) | ci, scripts | M | T4, T6, T7 | `release_artifacts_missing == 0` | `test_only_release_yml_triggers_on_tags` in `tests/ci/test_release_workflow.py` — a text scan of `.github/workflows/*.yml` finds `tags:` under `on.push` in `release.yml` only, and `package.yml` has `workflow_call` and no `push`; `test_check_release_assets_flags_missing_and_unchecksummed` in `tests/ci/test_check_release_assets.py` — given a fake `gh release view` JSON missing the `.mcpb` and one checksum line, it prints `release_artifacts_missing == 2` |
| T9 | Docs: README "Install without Python (Windows)" with numbered steps for the `.mcpb` and the installer, the SmartScreen "More info → Run anyway" step, the checksum check and uninstall; `docs/install-windows.md`; clean-machine step count recorded in the evidence log (surface: windows-ci + manual on a clean Windows machine) | docs | S | T8 | `install_steps_windows_mcpb <= 2` | `test_readme_windows_install_steps_are_numbered_and_bounded` in `tests/test_docs.py` — the `.mcpb` list has at most 2 numbered steps and the installer list at most 3, each besides the SmartScreen step, and every asset name it links matches the T8 asset pattern |
| T11 | Opt-in update notice: `doc_control/update_check.py` (stdlib `urllib`, 3 s timeout), on with `DOC_CONTROL_UPDATE_CHECK=1` from the manifest's boolean `user_config.update_check` (default false); at most one GitHub Releases request a day, cached under the user's local app data; `capabilities` and `live.status` add `update: {current, latest, mcpb_url, checked_at}`; privacy text in the manifest and README names the exception (D-P7) | src, packaging, docs | M | — | `update_check_requests_when_off == 0` | `test_no_request_when_off`, `test_second_check_within_a_day_uses_cache`, `test_failed_check_is_quiet` in `tests/test_update_check.py` |
| T12 | winget manifest: `scripts/build_winget.py --tag` writes version, installer and defaultLocale files (`InstallerType: inno`, `Scope: user`, URL and SHA-256 from the release); submission to `microsoft/winget-pkgs` by hand with the owner's agreement (D-P8) | packaging, scripts | S | T8 | `winget_manifest_errors == 0` | `test_winget_manifest_fields_match_release`, `test_winget_manifest_validates` in `tests/packaging/test_winget.py` |
| T13 | Clean uninstall check: `scripts/clean_uninstall_check.py`, report-only, prints `uninstall_leftovers == N` over the install folder, `PATH`, the Claude Desktop extension folder, settings and installations entry, config entries and the app-data store; linked from `docs/install-windows.md` | scripts, docs | S | T9 | `uninstall_leftovers` reported, nothing deleted | `test_reports_each_leftover_and_deletes_nothing`, `test_clean_tree_reports_zero` in `tests/packaging/test_clean_uninstall_check.py` |
| T14 | Installer handoff: the installer ships the `.mcpb`; its finish page offers "Add to Claude Desktop" (launches Claude Desktop with the file, or opens Explorer with it selected, per the owner's launch test) and, unchecked, removal of an old `uvx doc-control` config entry with a backup (`scripts/remove_uvx_entry.py`) (D-P9) | packaging, scripts | M | T8 | `installer_handoff_failures == 0` | `test_iss_ships_the_mcpb_and_offers_handoff`, `test_uvx_entry_removal_backs_up_and_asks` in `tests/packaging/test_installer_handoff.py` |

**Status legend**: ☐ not started · ◐ in progress · ☑ merged

**Merge order**:
1. T1 (the spike) first; its decision (D-P6) is recorded before T2 starts.
2. T2, then T3.
3. T4, T5, T6 and T7 in parallel.
4. T8.
5. T9, T12 and T14 after T8; T13 after T9. T11 has no dependency and can go any time. T10 is dropped.

**Branch pattern**: `package-T<N>-description` from the default branch.

### Dependency graph

```drawspec
{"version": 1, "kind": "flow", "title": "Task dependencies",
 "nodes": [
  {"id": "T1", "text": "T1\nQ1 spike, measured: a throwaway wor…"},
  {"id": "T2", "text": "T2\nFrozen build with the T1 freezer: p…"},
  {"id": "T3", "text": "T3\nscripts/frozensmoke.py --exe PATH -…"},
  {"id": "T4", "text": "T4\nscripts/frozenparity.py --exe PATH:…"},
  {"id": "T5", "text": "T5\nLive-Word gate over stdio: a Runner…"},
  {"id": "T6", "text": "T6\n.mcpb bundle: packaging/mcpb/manife…"},
  {"id": "T7", "text": "T7\nInno Setup installer: packaging/ins…"},
  {"id": "T8", "text": "T8\nRelease wiring: release.yml gains p…"},
  {"id": "T9", "text": "T9\nDocs: README \"Install without Pytho…"},
  {"id": "T11", "text": "T11\nOpt-in update notice: doccontrol/up…"},
  {"id": "T12", "text": "T12\nwinget manifest: scripts/buildwinge…"},
  {"id": "T13", "text": "T13\nClean uninstall check: scripts/clea…"},
  {"id": "T14", "text": "T14\nInstaller handoff: the installer sh…"}
 ],
 "edges": [
  {"from": "T1", "to": "T2"},
  {"from": "T2", "to": "T3"},
  {"from": "T3", "to": "T4"},
  {"from": "T3", "to": "T5"},
  {"from": "T3", "to": "T6"},
  {"from": "T3", "to": "T7"},
  {"from": "T4", "to": "T8"},
  {"from": "T6", "to": "T8"},
  {"from": "T7", "to": "T8"},
  {"from": "T8", "to": "T9"},
  {"from": "T8", "to": "T12"},
  {"from": "T9", "to": "T13"},
  {"from": "T8", "to": "T14"}
 ]}
```

## Evidence log

| T# | Gate | Measured | Command | SHA | Env | Date |
|----|------|----------|---------|-----|-----|------|
| T1 | `freezer_candidates_measured >= 2` | 2 (summed over the two matrix jobs, one candidate each) — PyInstaller: dist_mb 29.32, startup_ms_median 307.0 (181.6 on the earlier run), smoke_failures 0, build_minutes 0.2; Nuitka: dist_mb 40.24, startup_ms_median 245.3, smoke_failures 0, build_minutes 7.7 | `.github/workflows/freeze-spike.yml` run 38631660565 (`packaging/spike/measure.py --runs 10`) | 84de605 | windows-latest, Python 3.12 | 2026-10-14 |
| T1 | first run (40aa765, run 38629330164) | `capabilities` smoke failed under both freezers: the CLI printed cp1252 into the pipe; fixed in this PR (`cli.py` writes UTF-8, `tests/test_cli_encoding.py`) | same workflow | 40aa765 | windows-latest | 2026-10-14 |
| T2 | `frozen_exe_size_mb <= 40` | 29.54 (uncompressed one-folder build, 110 files); `packaging/smoke.py --frozen` passed: capabilities frozen, regex find ok | `.github/workflows/package.yml` via ci.yml, run 38643198602 | 9b8d88d | windows-latest, Python 3.12, PyInstaller 6.22.3 | 2026-10-14 |
| T5 | `live_word_failures == 0` | 0 with the frozen exe (CI artifact `windows-frozen` of run 38667575655, 0.6.4, 82cdc33) as the MCP server; 0 with `python -m doc_control mcp`; the in-process suite (`scripts/live_word_check.py`) also 0 after the `match_level` fix in `deletions` | `scripts/live_word_frozen_check.py --exe doc-control.exe` | (this PR) | owner's PC, Windows 11, Word 16.0.17932 64-bit (no Word running before) | 2026-10-15 |

---

## Sign-off

- [ ] Plan reviewed by the owner
- [ ] Contracts agreed with literary-critic (install route for their T6)
- [ ] Ready for execution
