# Installing doc-control on Windows without Python

doc-control ships as a ready-made Windows program, so you do not need Python, `pip` or
`uv`. Pick one of two files from the
[latest release](https://github.com/nuncaeslupus/doc-control/releases/latest):

| You want | Download | Steps |
|----------|----------|-------|
| doc-control's tools in Claude Desktop | `doc-control-X.Y.Z-windows-x64.mcpb` | 2 |
| The `doc-control` command in a terminal (and, by hand, in Claude Desktop) | `doc-control-X.Y.Z-windows-x64-setup.exe` | 3, plus the SmartScreen step |

`X.Y.Z` is the release's version number. A third file, `doc-control-X.Y.Z-windows-x64.zip`,
holds the same program for people who prefer to unzip it anywhere and manage it by hand.

**What it covers.** Both install for the Windows user who runs them, without admin rights:
another user on the same PC installs it again for themselves. The extension works with any
Claude account in Claude Desktop, the free plan included, because it runs on your own
machine. It drives the Microsoft Word you have open (Windows, 64-bit) and edits `.docx`
files directly. Live control of LibreOffice Writer is not in this build; it needs the
Python install (`pip install doc-control`, see the [README](../README.md#usage)).

**Privacy.** Everything runs on your machine. The one exception is opt-in: the extension's
"Check for updates" setting, off by default (see [Updating](#updating)).

## Claude Desktop extension (`.mcpb`)

1. Download `doc-control-X.Y.Z-windows-x64.mcpb` from the [latest release](https://github.com/nuncaeslupus/doc-control/releases/latest).
2. Drag the file onto Claude Desktop's **Settings > Extensions** page and click **Install**.

What you will see:

- A red warning that installing grants the extension access to everything on your
  computer. Claude Desktop shows it for every local extension. It is expected; go on.
- After **Install**: an **Enabled** toggle, **Configure**, "All requirements met", the list
  of doc-control's tools, the author, the Apache-2.0 license, the version, and
  Documentation and Support links.

Double-clicking the `.mcpb` does not install it: Claude Desktop registers no file type on
Windows, so Windows only asks which program should open the file. If dragging is awkward,
**Settings > Extensions > Advanced settings > Install Extension** opens a file picker for
the same file.

To check it worked, start a new chat and ask Claude to list the doc-control tools, or to
show its capabilities (the `capabilities` tool).

## Installer (`-setup.exe`)

1. Download `doc-control-X.Y.Z-windows-x64-setup.exe` from the [latest release](https://github.com/nuncaeslupus/doc-control/releases/latest).
2. Double-click it.
3. If Windows SmartScreen says "Windows protected your PC", click **More info**, then **Run anyway**. The installer is not code-signed, so Windows does not yet know its publisher.
4. Click **Install**, **Next** and **Finish**, and open a new terminal: `doc-control server.capabilities` works there.

The installer puts the program in `%LOCALAPPDATA%\Programs\doc-control` and adds that
folder to your user `PATH`. A terminal that was already open keeps the old `PATH`, which is
why step 4 says *new* terminal.

The finish page offers, unticked:

- **Add to Claude Desktop**: opens Claude Desktop's extension dialog with the `.mcpb` the
  installer ships, so you only click **Install** there. If Claude Desktop is not found, the
  option instead shows the `.mcpb` in File Explorer, to drag onto **Settings > Extensions**.
- **Remove the old uvx entry**: shown only when your Claude Desktop config still runs
  doc-control through `uvx` (see [Moving from `uvx doc-control`](#moving-from-uvx-doc-control)).

The page before it shows a `claude_desktop_config.json` snippet that points Claude Desktop
at the installed `doc-control.exe`. Use it only if you want Claude Desktop to run the
installed copy instead of the extension; do not do both, or every tool appears twice.

## Checking the download (optional)

Each release lists a SHA-256 checksum for every file, under **Checksums** in the release
notes and in the attached `SHA256SUMS.txt`. In PowerShell, in your Downloads folder:

```powershell
Get-FileHash .\doc-control-X.Y.Z-windows-x64.mcpb -Algorithm SHA256
```

The `Hash` it prints must equal the line for that file name (letter case does not matter).
If it differs, delete the file and download it again.

## Moving from `uvx doc-control`

If your `claude_desktop_config.json` already runs doc-control through `uvx` (the Python
route), installing the extension leaves that entry in place, and Claude Desktop then lists
every tool twice. The installer's **Remove the old uvx entry** option does this for you: it
lists the entries, asks first, and keeps a `.bak` of the config. By hand: quit Claude
Desktop, open **Settings > Developer > Edit Config**, delete the `doc-control` entry under
`mcpServers`, save, and start Claude Desktop again.

## Updating

Neither file updates itself.

- **Extension**: download the newer `.mcpb` and install it the same way. If Claude Desktop
  does not offer to replace the installed version, uninstall the old one first.
- **Installer**: run the newer `-setup.exe`. It finds the installed copy and replaces it in
  place; your `PATH` entry stays as it is.
- **Being told about new versions**: in **Settings > Extensions > doc-control > Configure**,
  switch on **Check for updates**. doc-control then asks GitHub for the latest version at
  most once a day and reports it in `server.capabilities` and `live.status` (with the `.mcpb`
  link). It never downloads or installs anything. Off, it makes no request at all. With
  the installer's copy, set the environment variable `DOC_CONTROL_UPDATE_CHECK=1` instead.
- **winget**: a winget package, so that `winget upgrade` handles it, is coming.

## Uninstalling

- **Extension**: **Settings > Extensions**, open doc-control, and uninstall it.
- **Installer**: Windows **Settings > Apps > Installed apps**, find "doc-control X.Y.Z",
  and uninstall it. That removes the program folder, the `PATH` entry and the uninstall
  entry.

Two things stay behind on purpose, because neither installer created them:

- a `doc-control` entry you added by hand to `claude_desktop_config.json`: delete it as in
  [Moving from `uvx doc-control`](#moving-from-uvx-doc-control);
- the update-check cache in `%LOCALAPPDATA%\doc-control`, present only if you switched
  "Check for updates" on: delete that folder.

Your documents are never touched by an install or an uninstall.
