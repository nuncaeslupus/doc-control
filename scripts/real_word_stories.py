"""STORIES T11: check doc-control's edits to notes, headers and tables in real Microsoft Word.

Two steps, run from a clone of the repository (``uv run python scripts/real_word_stories.py``):

    build  OUT   write the test documents and the expected results into OUT, print the steps
    verify OUT   read back what you saved from Word and report every mismatch

Follow the "Stories" section of ``docs/real-word-check.md`` between the two. Nothing here
needs Word itself; you open, accept and reject by hand. The documents:

    notes.docx    a tracked footnote and a tracked endnote added with ``note.add``, and a
                  tracked edit inside an existing footnote
    headers.docx  a tracked edit inside a running header
    tables.docx   a tracked row inserted into a table and a tracked row deleted from it
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from doc_control import api  # noqa: E402
from tests.corpus.generator import generate  # noqa: E402

AUTHOR = "doc-control STORIES"
NAMES = ("notes", "headers", "tables")
ALL_STORIES = ["body", "notes", "headers"]


def _rows(path: Path) -> list[dict[str, Any]]:
    """Every paragraph of every story (the body, notes, headers and footers)."""
    with api.open(path) as doc:
        result = doc.run("paragraphs.list", {"stories": ALL_STORIES})
    rows: list[dict[str, Any]] = result["paragraphs"]
    return rows


def _texts(path: Path) -> list[str]:
    return [row["text"] for row in _rows(path)]


def _remaining(path: Path) -> int:
    with api.open(path) as doc:
        return len(doc.run("revisions.list", {"stories": ALL_STORIES})["revisions"])


def _first(rows: list[dict[str, Any]], story: str, words: int = 1) -> dict[str, Any]:
    for row in rows:
        if row.get("story") == story and len(row["text"].split()) >= words:
            return row
    sys.exit(f"the corpus has no {story} paragraph of {words}+ words")


def _body(rows: list[dict[str, Any]], skip: int = 0) -> dict[str, Any]:
    found = [r for r in rows if "story" not in r and len(r["text"].split()) >= 6]
    return found[skip]


def _first_word(text: str) -> tuple[int, int]:
    """Start and end offsets of the first word, past any leading spaces."""
    lead = len(text) - len(text.lstrip())
    return lead, lead + len(text.split()[0])


def _edit(path: Path, name: str, store: Path) -> tuple[list[str], list[str]]:
    """Make the tracked edits; return the paragraph texts they remove and add once accepted."""
    rows = _rows(path)
    removed: list[str] = []
    added: list[str] = []
    with api.open(path, store=store) as doc:
        if name == "notes":
            note = _first(rows, "footnote")
            lead, word = _first_word(note["text"])
            removed.append(note["text"])
            added.append(note["text"][:lead] + "EDITED" + note["text"][word:])
            doc.run(
                "batch",
                {
                    "ops": [
                        {
                            "op": "text.replace",
                            "at": f"{note['at']}:{lead}-{word}",
                            "text": "EDITED",
                        }
                    ],
                    "author": AUTHOR,
                },
            )
            for skip, kind in ((0, "footnote"), (1, "endnote")):
                body = _body(_rows(path), skip)
                end = len(body["text"])
                # note.add puts a space before the text, inside the tracked insertion.
                added.append(f" A tracked {kind} added by doc-control.")
                doc.run(
                    "note.add",
                    {
                        "at": f"{body['at']}:{end}-{end}",
                        "kind": kind,
                        "text": f"A tracked {kind} added by doc-control.",
                        "author": AUTHOR,
                    },
                )
        elif name == "headers":
            header = _first(rows, "header", 2)
            lead, word = _first_word(header["text"])
            removed.append(header["text"])
            added.append(header["text"][:lead] + "EDITED" + header["text"][word:])
            doc.run(
                "batch",
                {
                    "ops": [
                        {
                            "op": "text.replace",
                            "at": f"{header['at']}:{lead}-{word}",
                            "text": "EDITED",
                        }
                    ],
                    "author": AUTHOR,
                },
            )
        else:
            table = doc.run("table.get", {"table": 1})["rows"]
            cells = ["Inserted row"] * len(table[0])
            by_address = {r["at"]: r["text"] for r in rows}
            removed += [by_address[a] for cell in table[-1] for a in cell["paragraphs"]]
            added += cells
            ops = [
                {"op": "table.row.insert", "table": 1, "after_row": 1, "cells": cells},
                {"op": "table.row.delete", "table": 1, "row": len(table)},
            ]
            doc.run("batch", {"ops": ops, "author": AUTHOR})
    return removed, added


def build(out: Path) -> None:
    if out.exists():
        sys.exit(f"{out} exists; pass a new folder")
    out.mkdir(parents=True)
    store = out / ".store"
    expected: dict[str, Any] = {}
    for k, name in enumerate(NAMES, start=1):
        path = out / f"{name}.docx"
        path.write_bytes(generate(name, k))
        original = _texts(path)
        removed, added = _edit(path, name, store)
        accepted = Counter(original) - Counter(removed) + Counter(added)
        expected[name] = {
            "original": original,
            "accepted": sorted(accepted.elements()),
            "revisions": _remaining(path),
        }
        if not expected[name]["revisions"]:
            sys.exit(f"{name}: the edits left no tracked change; nothing to check in Word")
    shutil.rmtree(store)
    (out / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=1))
    print(f"Wrote {len(NAMES)} documents to {out}. Now follow docs/real-word-check.md (Stories).")


def _compare(name: str, got: list[str], want: list[str]) -> str | None:
    if got == want:
        return None
    first = next(
        (k for k, (x, y) in enumerate(zip(got, want, strict=False)) if x != y),
        min(len(got), len(want)),
    )
    return f"{name}: {len(got)} paragraphs vs {len(want)} expected; first difference at {first + 1}"


def _sample(texts: Counter[str]) -> str:
    shown = [repr(t[:40]) for t in texts.elements()][:3]
    return ", ".join(shown) + (" ..." if sum(texts.values()) > 3 else "") if shown else "nothing"


def verify(out: Path) -> int:
    expected = json.loads((out / "expected.json").read_text())
    problems: list[str] = []
    for name in NAMES:
        for suffix, key, step in (
            ("accepted", "accepted", "Accept All"),
            ("rejected", "original", "Reject All"),
        ):
            file = f"{name}-{suffix}.docx"
            path = out / file
            if not path.exists():
                problems.append(f"{file}: missing (save it from Word as described)")
                continue
            if (left := _remaining(path)) > 0:
                problems.append(f"{file}: {left} tracked changes left; was {step} done?")
            got = _texts(path)
            if suffix == "accepted":  # compare the content; where Word puts a row is its own
                missing = Counter(expected[name][key]) - Counter(got)
                extra = Counter(got) - Counter(expected[name][key])
                if missing or extra:
                    problems.append(
                        f"{file}: expected but not found: {_sample(missing)}; "
                        f"found but not expected: {_sample(extra)}"
                    )
            elif (bad := _compare(file, got, expected[name][key])) is not None:
                problems.append(bad)
    for line in problems:
        print("MISMATCH", line)
    print(f"real_word_stories_mismatches == {len(problems)}")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=("build", "verify"))
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    if args.step == "build":
        build(args.out)
        return 0
    return verify(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
