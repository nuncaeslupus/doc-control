#!/usr/bin/env python3
"""Private-corpus identifier scan over tracked files (stdlib only).

Fails when a tracked file's *content or path* contains a denylisted term. The
denylist is a text file kept outside the repo, named by DOC_CONTROL_HYGIENE_DENYLIST
(one term per line, ``#`` comments, case-insensitive). Built-in rules need no
file: no absolute home-directory path (``/home/x/``, ``/Users/x/``, ``C:\\Users\\x\\``)
and no tracked file named like an office document outside tests/fixtures.

Output never echoes a term or the matched text, only ``path:line`` and the rule
number, so a failing CI log cannot leak the very thing it guards.
Prints ``private_corpus_identifiers_in_repo=<n>``; exit 1 when n > 0.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ENV_VAR = "DOC_CONTROL_HYGIENE_DENYLIST"
# Placeholders such as `/home/user/` or `C:\Users\you\` in docs are not paths of a real person.
HOME_PATH = re.compile(
    r"(?:/home/|/Users/|[A-Za-z]:\\Users\\)(?!(?:user|you|name|<)\b)[\w.-]+[/\\]"
)
SKIP_PREFIXES = ("claude-arsenal/", "arsenal/", ".claude/")
SKIP_NAMES = ("uv.lock", "scripts/hygiene_denylist.py", "tests/ci/test_hygiene.py")


def load_terms(path: str | None) -> list[str]:
    if not path:
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [t.strip().lower() for t in lines if t.strip() and not t.lstrip().startswith("#")]


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def scan(root: Path, terms: list[str]) -> list[str]:
    """Return ``path[:line] rule-N`` findings (never the matched text)."""
    findings: list[str] = []
    for rel in tracked_files(root):
        low = rel.lower()
        for n, term in enumerate(terms, 1):
            if term in low:
                findings.append(f"{rel} denylist-{n} (path)")
        if rel.startswith(SKIP_PREFIXES) or rel in SKIP_NAMES:
            continue
        path = root / rel
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            continue
        for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            low_line = line.lower()
            for n, term in enumerate(terms, 1):
                if term in low_line:
                    findings.append(f"{rel}:{lineno} denylist-{n}")
            if HOME_PATH.search(line):
                findings.append(f"{rel}:{lineno} home-directory-path")
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    terms = load_terms(os.environ.get(ENV_VAR))
    findings = scan(root, terms)
    for finding in findings:
        print(f"private identifier: {finding}")
    print(f"private_corpus_identifiers_in_repo={len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
