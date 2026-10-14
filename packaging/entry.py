"""Entry point of the frozen build: the `doc-control` console script as a plain script.

The frozen `doc-control.exe` runs the same `doc_control.cli:main` as the pip install, so
the CLI envelope and `doc-control mcp` are unchanged (PACKAGE spec §5).
"""

import sys

from doc_control.cli import main

if __name__ == "__main__":
    sys.exit(main())
