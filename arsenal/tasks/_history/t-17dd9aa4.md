---
id: t-17dd9aa4
title: "PACKAGE T2: frozen Windows build"
label: "frozen build"
priority: 5
deps: [t-329d1bc9]
workspace: PACKAGE
status: merged
---

## Acceptance gate
frozen_exe_size_mb <= 40 — from the PACKAGE plan (`arsenal/project/PACKAGE/plan.md`), spec `arsenal/project/PACKAGE/spec.md` §1 and §5.

```bash
uv run pytest -q tests/live/test_word_com.py -k test_word_error_when_frozen_names_no_pip
```

## Tests
`test_word_error_when_frozen_names_no_pip` in `tests/live/test_word_com.py` — with `sys.frozen` set and `win32com` unimportable, the error text has no "pip install"; `test_core_dependencies_stay_empty_with_package_group` in `tests/test_package.py` — `[project] dependencies` is `[]` and the freezer appears only in the `package` group

## Location
packaging, live, ci

## Description
Frozen build with the T1 freezer: `packaging/entry.py`, the build file (hidden imports `pythoncom`, `pywintypes`, `win32com.client`, `winreg`, every `doc_control.ops.*`; data `recipes/bundled/*.json`; excludes `tkinter` and tests), `package` dependency group, frozen-only `pywin32` error text in `live/word_com.py`, additive `capabilities.build`; `.github/workflows/package.yml` (`workflow_call`) builds and checks the uncompressed folder size, called from `ci.yml` with a paths filter (surface: windows-ci)
