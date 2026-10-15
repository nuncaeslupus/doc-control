---
id: t-3ccbc888
title: "PACKAGE T9: install without Python docs"
label: "install without Python docs"
priority: 10
deps: [t-b45708fd]
workspace: PACKAGE
status: merged
---

## Acceptance gate
install_steps_windows_mcpb <= 2 — from the PACKAGE plan (`arsenal/project/PACKAGE/plan.md`), spec `arsenal/project/PACKAGE/spec.md` §1 and §5.

```bash
uv run pytest -q tests/test_docs.py -k test_readme_windows_install_steps_are_numbered_and_bounded
```

## Tests
`test_readme_windows_install_steps_are_numbered_and_bounded` in `tests/test_docs.py` — the `.mcpb` list has at most 2 numbered steps and the installer list at most 3, each besides the SmartScreen step, and every asset name it links matches the T8 asset pattern

## Location
docs

## Description
Docs: README "Install without Python (Windows)" with numbered steps for the `.mcpb` and the installer, the SmartScreen "More info → Run anyway" step, the checksum check and uninstall; `docs/install-windows.md`; clean-machine step count recorded in the evidence log (surface: windows-ci + manual on a clean Windows machine)

## Hand-test findings (owner's PC, Claude Desktop, 0.6.4, 2026-10-15)
- `.mcpb`: double-clicking it on Windows asks which program to open it with (no file
  association). It installs by dragging the file onto Claude Desktop Settings > Extensions
  (or Advanced settings > Install Extension). So step 1 is "drag it there", not "double-click".
- The extension page shows a red warning that installing grants the extension access to
  everything on the computer. Then: Install, an Enabled toggle, Configure, "All requirements
  met", the tool list, author, Apache-2.0, version, Documentation and Support links. Say in
  the docs that the warning is expected, so a writer does not stop there.
- Installing the `.mcpb` does not touch `claude_desktop_config.json`. Remove any older
  `uvx doc-control` entry from it by hand, or the tools appear twice.
- Installer: per user into `%LOCALAPPDATA%\Programs\doc-control`. It appends one user `PATH`
  entry, so a *new* terminal is needed before `doc-control capabilities` works. Uninstall
  (Settings > Apps, "doc-control <version>") removes the folder, the `PATH` entry and the
  registry entry.
