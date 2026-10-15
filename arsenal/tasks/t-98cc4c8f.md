---
id: t-98cc4c8f
title: "PACKAGE T14: installer hands off the .mcpb"
label: "installer .mcpb handoff"
priority: 5
deps: [t-b45708fd]
workspace: PACKAGE
---

## Acceptance gate
installer_handoff_failures == 0 — from the PACKAGE plan (`arsenal/project/PACKAGE/plan.md`), spec `arsenal/project/PACKAGE/spec.md` §1 and §3 (r3).

```bash
uv run pytest -q tests/packaging/test_installer_handoff.py
```

## Tests
`test_iss_ships_the_mcpb_and_offers_handoff` in `tests/packaging/test_installer_handoff.py` — the `.iss` file installs the `.mcpb` and its finish page has an unchecked-by-default "Add to Claude Desktop" run entry; `test_uvx_entry_removal_backs_up_and_asks` in the same file — `scripts/remove_uvx_entry.py` on a fake config with a `uvx doc-control` server writes `claude_desktop_config.json.bak` and removes only that entry, and with `--dry-run` changes nothing

## Location
packaging/installer, scripts

## Description
Installer handoff (D-P9): Claude Desktop has no `.mcpb` file association on Windows. The installer ships the `.mcpb` and its finish page offers "Add to Claude Desktop": launch `claude-desktop.exe "<mcpb>"` if the owner's 2026-10-15 test showed that opens the install dialog, otherwise `explorer /select,"<mcpb>"` with the text "drag it onto Claude Desktop > Settings > Extensions". A second, unchecked option removes an old `uvx doc-control` entry from `claude_desktop_config.json` after asking, with a `.bak` backup, and tells the user to restart Claude Desktop. Record the launch-test result in this file before starting.
