#!/usr/bin/env python3
"""CI: structural lint (and optional LibreOffice round trip) of generated documents.

Validates every synthetic corpus kind plus every .docx found under the given
directories (the packages the tool's own tests wrote, kept by tests/conftest.py
when ``DOC_CONTROL_KEEP_DOCS=<dir>`` is set). A kept ``<name>.docx`` with a
sibling ``<name>.before`` is judged on the errors it *introduced* (problems
already in the input do not count). Stdlib only; messages name ids only.

    python scripts/ci_validate_docs.py [--roundtrip] [DIR ...]

Prints ``docs_checked=<n>`` and ``docs_failing_validator=<n>``; exit 1 if the
latter is above zero. ``--roundtrip`` also runs the LibreOffice round trip and
fails when soffice is missing, so the job cannot pass by skipping.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.corpus.generator import KINDS, generate
from tests.corpus.structural import validate_structure
from tests.corpus.validator import validate_documents


def collect(dirs: list[str]) -> tuple[dict[str, bytes], dict[str, bytes], dict[str, str]]:
    inputs: dict[str, bytes] = {}
    origins: dict[str, str] = {}
    docs = {f"corpus-{kind}": generate(kind, 0) for kind in KINDS}
    for directory in dirs:
        for path in sorted(Path(directory).rglob("*.docx")):
            if path.name.startswith("~$") or path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            doc_id = f"test-{hashlib.sha256(data).hexdigest()[:12]}"
            docs[doc_id] = data
            source = path.with_suffix(".test")
            if source.is_file():
                origins[doc_id] = source.read_text(encoding="utf-8")
            before = path.with_suffix(".before")
            if before.is_file():
                inputs[doc_id] = before.read_bytes()
    return docs, inputs, origins


def main(argv: list[str]) -> int:
    roundtrip = "--roundtrip" in argv
    docs, inputs, origins = collect([a for a in argv[1:] if not a.startswith("--")])
    if roundtrip:
        os.environ["DOC_CONTROL_REQUIRE_SOFFICE"] = "1"
        with tempfile.TemporaryDirectory() as work:
            results = validate_documents(docs, Path(work))
    else:
        results = {doc_id: validate_structure(data) for doc_id, data in docs.items()}
    for doc_id, before in inputs.items():
        existing = set(validate_structure(before))
        results[doc_id] = [p for p in results[doc_id] if p not in existing]
    failing = 0
    for doc_id, problems in sorted(results.items()):
        if problems:
            failing += 1
            extra = f" (+{len(problems) - 1} more)" if problems[1:] else ""
            print(f"FAIL {doc_id} [{origins.get(doc_id, 'corpus')}]: {problems[0]}{extra}")
    print(f"docs_checked={len(docs)}")
    print(f"docs_failing_validator={failing}")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
