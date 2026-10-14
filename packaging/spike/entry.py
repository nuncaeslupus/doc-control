"""Freezer spike entry point: the `doc-control` console script as a plain script."""

import sys

from doc_control.cli import main

if __name__ == "__main__":
    sys.exit(main())
