---
id: t-3ccbc888
title: "PACKAGE T9: install without Python docs"
label: "install without Python docs"
priority: 10
deps: [t-b45708fd]
workspace: PACKAGE
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
