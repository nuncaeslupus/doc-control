"""Entry point of the frozen build: the `doc-control` console script as a plain script.

The frozen `doc-control.exe` runs the same `doc_control.cli:main` as the pip install, so
the CLI envelope and `doc-control mcp` are unchanged (PACKAGE spec §5).

One addition, for the installer only: `doc-control.exe --remove-uvx-entry [options]` runs
`scripts/remove_uvx_entry.py` (frozen alongside, PACKAGE T14, D-P9), so the installer's
finish page can remove an old `uvx doc-control` config entry on a machine without Python.
"""

import multiprocessing
import sys

from doc_control.cli import main

REMOVE_UVX_ENTRY = "--remove-uvx-entry"

if __name__ == "__main__":
    # The regex worker is a spawned child; frozen, it re-runs this exe and must stop here.
    multiprocessing.freeze_support()
    if sys.argv[1:2] == [REMOVE_UVX_ENTRY]:
        from remove_uvx_entry import main as remove_uvx_entry

        sys.exit(remove_uvx_entry(sys.argv[2:]))
    sys.exit(main())
