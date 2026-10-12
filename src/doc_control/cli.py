"""Command-line front door. Operations are added by the registry (T2)."""

from __future__ import annotations

import json
import sys

from doc_control import API_VERSION, __version__


def main(argv: list[str] | None = None) -> int:
    """Print the version envelope; the operation registry replaces this in T2."""
    args = sys.argv[1:] if argv is None else argv
    envelope = {"api": API_VERSION, "ok": True, "op": "version", "result": {"version": __version__}}
    if args and args[0] not in ("version", "--version"):
        envelope = {
            "api": API_VERSION,
            "ok": False,
            "op": args[0],
            "error": {
                "code": "SCHEMA_INVALID",
                "message": f"unknown operation: {args[0]}",
                "hint": "Run `doc-control version`; more operations arrive with the registry.",
            },
        }
    print(json.dumps(envelope, ensure_ascii=False))
    return 0 if envelope["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
