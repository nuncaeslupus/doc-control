"""Measure frozen doc-control builds for the freezer spike (PACKAGE T1, Q1).

Usage:
    python measure.py --exe doc-control.exe [--runs 10] [--build-minutes NAME=MIN ...]
                      [--out report.json] NAME=DIST_FOLDER [NAME=DIST_FOLDER ...]

For each candidate (a one-folder build) it records:
  dist_mb            size of the whole folder, in MiB
  startup_ms_median  median wall time of `<exe> capabilities` over --runs runs
  smoke_failures     failing smokes among `capabilities` and MCP `tools/list` over stdio
  build_minutes      as passed with --build-minutes, else null

Prints the JSON report on stdout and writes it to --out. Standard library only; runs on
any OS. Exit 0 when every candidate was measured, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

TIMEOUT_S = 120
MIB = 1024 * 1024


def _pair(text: str) -> tuple[str, str]:
    name, sep, value = text.partition("=")
    if not sep or not name or not value:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {text!r}")
    return name, value


def folder_mb(folder: Path) -> float:
    total = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink())
    return round(total / MIB, 2)


def startup_ms_median(exe: Path, runs: int) -> float:
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        subprocess.run(
            [str(exe), "capabilities"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=TIMEOUT_S,
            check=False,
        )
        samples.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(samples), 1)


def smoke_capabilities(exe: Path) -> str | None:
    """None when `capabilities` prints an ok envelope and exits 0; else the reason."""
    proc = subprocess.run(
        [str(exe), "capabilities"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        return f"exit {proc.returncode}: {proc.stderr.decode(errors='replace')[-400:]}"
    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        return f"stdout is not JSON: {proc.stdout[:200]!r}"
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        return f"envelope not ok: {str(envelope)[:200]}"
    return None


def smoke_tools_list(exe: Path) -> str | None:
    """None when `mcp` answers `initialize` then a non-empty `tools/list`; else the reason."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "freeze-spike", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = b"".join(json.dumps(m).encode() + b"\n" for m in messages)
    proc = subprocess.run(
        [str(exe), "mcp"], input=payload, capture_output=True, timeout=TIMEOUT_S, check=False
    )
    replies: dict[Any, Any] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            reply = json.loads(line)
        except ValueError:
            return f"non-JSON line on stdout: {line[:200]!r}"
        if isinstance(reply, dict):
            replies[reply.get("id")] = reply
    tools = replies.get(2, {}).get("result", {}).get("tools")
    if not isinstance(tools, list) or not tools:
        err = proc.stderr.decode(errors="replace")[-400:]
        return f"no tools in tools/list reply (exit {proc.returncode}): {err}"
    return None


def measure(dist: Path, exe_name: str, runs: int, build_minutes: float | None) -> dict[str, Any]:
    exe = dist / exe_name
    row: dict[str, Any] = {
        "dist": str(dist),
        "dist_mb": folder_mb(dist) if dist.is_dir() else None,
        "startup_ms_median": None,
        "smoke_failures": 2,
        "smokes": {},
        "build_minutes": build_minutes,
    }
    if not exe.is_file():
        row["smokes"] = {"capabilities": f"missing {exe}", "tools/list": f"missing {exe}"}
        return row
    smokes = {}
    for name, check in (("capabilities", smoke_capabilities), ("tools/list", smoke_tools_list)):
        try:
            smokes[name] = check(exe)
        except (OSError, subprocess.TimeoutExpired) as err:
            smokes[name] = f"{type(err).__name__}: {err}"
    row["smokes"] = {name: reason or "ok" for name, reason in smokes.items()}
    row["smoke_failures"] = sum(reason is not None for reason in smokes.values())
    try:
        row["startup_ms_median"] = startup_ms_median(exe, runs)
    except (OSError, subprocess.TimeoutExpired) as err:
        row["startup_error"] = f"{type(err).__name__}: {err}"
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure frozen doc-control builds.")
    parser.add_argument("candidates", nargs="+", type=_pair, metavar="NAME=DIST_FOLDER")
    parser.add_argument("--exe", required=True, help="the exe's path relative to each folder")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--build-minutes", type=_pair, action="append", default=[])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    minutes = {name: float(value) for name, value in args.build_minutes}
    candidates = {
        name: measure(Path(folder), args.exe, args.runs, minutes.get(name))
        for name, folder in args.candidates
    }
    measured = sum(
        row["dist_mb"] is not None and row["startup_ms_median"] is not None
        for row in candidates.values()
    )
    report = {
        "exe": args.exe,
        "runs": args.runs,
        "platform": sys.platform,
        "candidates": candidates,
        "freezer_candidates_measured": measured,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if measured == len(candidates) else 1


if __name__ == "__main__":
    sys.exit(main())
