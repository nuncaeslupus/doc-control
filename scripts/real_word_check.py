"""T30: check doc-control's output in real Microsoft Word (Windows, Word 2016+ / Microsoft 365).

Two steps, run from a clone of the repository (``uv run python scripts/real_word_check.py``):

    build  OUT   write the test documents and the expected results into OUT, print the steps
    verify OUT   read back what you saved from Word and report every mismatch

Follow ``docs/real-word-check.md`` between the two. Nothing here needs Word itself; Word is
driven by you, by hand, or by ``scripts/real_word_auto.ps1``, to open, accept, reject, reply
and resolve.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from doc_control import api  # noqa: E402
from tests.corpus.generator import KINDS, generate  # noqa: E402

AUTHOR = "doc-control T30"


def _texts(path: Path) -> list[str]:
    with api.open(path) as doc:
        return [row["text"] for row in doc.paragraphs()]


def _rows(path: Path) -> list[dict[str, Any]]:
    with api.open(path) as doc:
        return [r for r in doc.paragraphs() if len(r["text"].split()) >= 6]


def _edits(path: Path) -> list[dict[str, Any]]:
    """Every kind of tracked edit doc-control writes, on distinct paragraphs."""
    rows = _rows(path)
    a, b, c, d, e, f, g = (rows[k]["at"] for k in range(7))
    first_word = len(rows[0]["text"].split()[0])
    return [
        {"op": "text.replace", "at": f"{a}:0-{first_word}", "text": "REPLACED"},
        {"op": "text.replace", "at": f"{b}:3-8", "text": ""},
        {"op": "para.insert", "after": c, "text": "An inserted paragraph from doc-control."},
        {"op": "para.delete", "at": d},
        {"op": "para.move", "at": e, "before": a},
        {"op": "style.apply", "at": f, "style": "Heading1"},
        {"op": "format.apply", "at": g, "bold": True, "italic": True},
    ]


def _comments(path: Path) -> list[dict[str, Any]]:
    rows = _rows(path)
    return [
        {
            "op": "comment.add",
            "at": f"{rows[0]['at']}:0-12",
            "text": "First comment: reply in Word.",
        },
        {"op": "comment.add", "at": rows[1]["at"], "text": "Second: resolve in Word."},
    ]


def build(out: Path) -> None:
    if out.exists():
        sys.exit(f"{out} exists; pass a new folder")
    out.mkdir(parents=True)
    store = out / ".store"
    expected: dict[str, Any] = {}
    for kind in KINDS:
        base = out / f"{kind}.docx"
        base.write_bytes(generate(kind, 1))
        expected[kind] = {"original": _texts(base)}

    edited = out / "edits.docx"
    edited.write_bytes(generate("novel", 2))
    original = _texts(edited)
    with api.open(edited, store=store) as doc:
        doc.run("batch", {"ops": _edits(edited), "author": AUTHOR})
    accepted = out / ".edits-accepted.docx"
    shutil.copyfile(edited, accepted)
    with api.open(accepted, store=store) as doc:
        doc.run("batch", {"ops": [{"op": "revision.accept", "all": True}]})
    expected["edits"] = {"original": original, "accepted": _texts(accepted)}
    accepted.unlink()

    commented = out / "comments.docx"
    commented.write_bytes(generate("novel", 3))
    with api.open(commented, store=store) as doc:
        doc.run("batch", {"ops": _comments(commented), "author": AUTHOR})
        threads = doc.run("comments.list")["comments"]
    expected["comments"] = {"count": len(threads), "texts": [t["text"] for t in threads]}
    shutil.rmtree(store)
    (out / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=1))
    print(f"Wrote {len(KINDS) + 2} documents to {out}. Now follow docs/real-word-check.md.")


def verify(out: Path) -> int:
    expected = json.loads((out / "expected.json").read_text())
    problems: list[str] = []

    def check(name: str, want: list[str]) -> None:
        path = out / name
        if not path.exists():
            problems.append(f"{name}: missing (save it from Word as described)")
        elif (got := _texts(path)) != want:
            first = next(
                (k for k, (x, y) in enumerate(zip(got, want, strict=False)) if x != y),
                min(len(got), len(want)),
            )
            problems.append(
                f"{name}: {len(got)} paragraphs vs {len(want)} expected; first difference "
                f"at paragraph {first + 1}"
            )

    check("edits-accepted.docx", expected["edits"]["accepted"])
    check("edits-rejected.docx", expected["edits"]["original"])
    steps = (("edits-accepted.docx", "Accept All"), ("edits-rejected.docx", "Reject All"))
    for name, step in steps:
        if (out / name).exists():
            with api.open(out / name) as doc:
                left = doc.run("revisions.list")["revisions"]
            if left:
                problems.append(f"{name}: {len(left)} tracked changes left; was {step} done?")
    replied = out / "comments-word.docx"
    if not replied.exists():
        problems.append("comments-word.docx: missing (save it from Word as described)")
    else:
        with api.open(replied) as doc:
            threads = doc.run("comments.list")["comments"]
        roots = {t["id"]: t for t in threads if "reply_to" not in t}
        first = next((t for t in roots.values() if t["text"].startswith("First")), None)
        second = next((t for t in roots.values() if t["text"].startswith("Second")), None)
        replied_to = {t["reply_to"] for t in threads if "reply_to" in t}
        if len(roots) != expected["comments"]["count"] or first is None or second is None:
            problems.append(f"comments-word.docx: {len(roots)} threads, expected First and Second")
        else:
            if first["id"] not in replied_to:
                problems.append("comments-word.docx: no reply read back on the First comment")
            if not second["resolved"]:
                problems.append("comments-word.docx: the Second comment reads as open")
            if first["resolved"]:
                problems.append("comments-word.docx: the First comment reads as resolved")
    for line in problems:
        print("MISMATCH", line)
    print(f"real_word_roundtrip_mismatches == {len(problems)}")
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
