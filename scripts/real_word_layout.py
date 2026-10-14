"""LAYOUT T9: check doc-control's page layout (``layout.apply``, ``toc.insert``) in real Word.

Two steps, run from a clone of the repository (``uv run python scripts/real_word_layout.py``):

    build  OUT   write the test documents and the expected results into OUT, print the steps
    verify OUT   read back what you saved from Word and report every mismatch

Follow the "Layout" section of ``docs/real-word-check.md`` between the two. Nothing here
needs Word itself; you open, look, accept and reject by hand. Every document is the corpus
novel (a title, then chapters ``Capítulo 1`` to ``Capítulo 40``) with a footer printing the
page number, given one page plan from the LAYOUT spec (§1):

    front_matter       an unnumbered title page, then page 1 at Capítulo 1
    roman_then_arabic  i, ii, … up to Capítulo 2, then 1, 2, … (tracked changes only)
    part_page          Capítulo 2's heading alone on a counted page that shows no number
    restart_per_part   the count restarts at 1 at Capítulo 3 and at Capítulo 5
    recto_chapters     Capítulo 2 and Capítulo 3 start on a right-hand (odd) page
    two_sided          the README's plan: mirrored margins, the chapter title in the odd
                       headers and the book title in the even ones
    toc                a table of contents after the title, filled by ``fields.update``
                       (built only when LibreOffice is installed)
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
from doc_control.ooxml.soffice import soffice_path  # noqa: E402
from tests.corpus.generator import generate  # noqa: E402

AUTHOR = "doc-control LAYOUT"
#: The document that is also rejected: its plan is reached with tracked changes alone.
REJECTED = "roman_then_arabic"
#: What to look for in Word, per document (also printed by ``build``).
LOOK = {
    "front_matter": "the title page shows no number; the first page of Capítulo 1 shows 1",
    "roman_then_arabic": "the pages before Capítulo 2 show i, ii, iii, …; Capítulo 2 starts at 1",
    "part_page": "Capítulo 2's heading is alone on its page, which shows no number; the next "
    "page shows the count, two or more past the page before the heading",
    "restart_per_part": "the count starts at 1 on the title, again at Capítulo 3 and at Capítulo 5",
    "recto_chapters": "Capítulo 2 and Capítulo 3 each start on an odd page number (Word leaves "
    "a blank even page before them when needed)",
    "two_sided": "odd pages show the chapter title in the header, even pages 'La casa del río'; "
    "the title page shows neither; the inner margins are mirrored (View → Multiple Pages)",
    "toc": "a 'Contents' list after the title names every chapter with a page number; "
    "right-click it → Update Field → Update entire table must not change those numbers",
}


def _headings(path: Path) -> list[str]:
    with api.open(path) as doc:
        return [h["at"] for h in doc.run("outline", {})["headings"]]


def _paragraph_after(path: Path, at: str) -> str:
    with api.open(path) as doc:
        rows = doc.run("paragraphs.list", {})["paragraphs"]
    found = [i for i, row in enumerate(rows) if row["at"] == at]
    return str(rows[found[0] + 1]["at"])


def _plan(name: str, path: Path) -> tuple[dict[str, Any], bool]:
    """The page plan for document ``name`` and whether it needs ``untracked``."""
    with api.open(path) as doc:
        first = doc.run("paragraphs.list", {})["paragraphs"][0]["at"]
    ch = dict(enumerate(_headings(path), start=1))  # chapter number -> heading address
    if name == "front_matter":
        sections = [
            {"from": first, "numbering": {"shown": False}, "footer": {"default": ""}},
            {"from": ch[1], "break": "nextPage", "numbering": {"start": 1, "format": "decimal"}},
        ]
        return {"version": 1, "sections": sections}, True
    if name == "roman_then_arabic":
        sections = [
            {"from": first, "numbering": {"start": 1, "format": "lowerRoman"}},
            {"from": ch[2], "break": "nextPage", "numbering": {"start": 1, "format": "decimal"}},
        ]
        return {"version": 1, "sections": sections}, False
    if name == "part_page":
        sections = [
            {"from": first, "numbering": {"start": 1}},
            {
                "from": ch[2],
                "break": "oddPage",
                "numbering": {"shown": False},
                "footer": {"default": ""},
            },
            {
                "from": _paragraph_after(path, ch[2]),
                "break": "nextPage",
                "footer": {"default": "{PAGE}"},
            },
        ]
        return {"version": 1, "sections": sections}, True
    if name == "restart_per_part":
        sections = [
            {"from": at, "break": "nextPage", "numbering": {"start": 1}}
            for at in (first, ch[3], ch[5])
        ]
        return {"version": 1, "sections": sections}, False
    if name == "recto_chapters":
        sections = [{"from": first, "numbering": {"start": 1}}]
        sections += [{"from": ch[k], "break": "oddPage"} for k in (2, 3)]
        return {"version": 1, "sections": sections}, False
    if name == "two_sided":
        readme = (ROOT / "README.md").read_text("utf-8")
        start = readme.index("doc-control layout.apply --json '") + len(
            "doc-control layout.apply --json '"
        )
        request = json.loads(readme[start : readme.index("'", start)])
        return request["plan"], True
    raise ValueError(name)


def _base(path: Path, store: Path) -> None:
    """The corpus novel with a footer printing the page number, every change accepted."""
    path.write_bytes(generate("novel", 1))
    with api.open(path, store=store) as doc:
        first = doc.run("paragraphs.list", {})["paragraphs"][0]["at"]
        plan = {"version": 1, "sections": [{"from": first, "footer": {"default": "{PAGE}"}}]}
        doc.run("layout.apply", {"plan": plan, "untracked": True, "author": AUTHOR})
        doc.run("batch", {"ops": [{"op": "revision.accept", "all": True}]})


def _layout(path: Path) -> dict[str, Any]:
    with api.open(path) as doc:
        plan: dict[str, Any] = doc.run("layout.get", {})
    return plan


def _remaining(path: Path) -> int:
    with api.open(path) as doc:
        return len(doc.run("revisions.list", {"stories": ["body", "headers"]})["revisions"])


def _tocs(path: Path) -> list[str]:
    with api.open(path) as doc:
        rows = doc.run("fields.list", {"stories": ["body"], "type": "TOC"})["fields"]
    return [" ".join(str(row["result"]).split()) for row in rows]


def names(with_toc: bool) -> list[str]:
    return [name for name in LOOK if with_toc or name != "toc"]


def build(out: Path) -> None:
    if out.exists():
        sys.exit(f"{out} exists; pass a new folder")
    out.mkdir(parents=True)
    store = out / ".store"
    base = out / "base.docx"
    _base(base, store)
    with_toc = soffice_path() is not None
    expected: dict[str, Any] = {"original": _layout(base), "documents": {}}
    for name in names(with_toc):
        path = out / f"{name}.docx"
        shutil.copyfile(base, path)
        entry: dict[str, Any] = {"look": LOOK[name]}
        with api.open(path, store=store) as doc:
            if name == "toc":
                first = doc.run("paragraphs.list", {})["paragraphs"][0]["at"]
                doc.run("toc.insert", {"after": first, "levels": "1", "author": AUTHOR})
                doc.run("fields.update", {"author": AUTHOR})
            else:
                plan, untracked = _plan(name, path)
                doc.run("layout.apply", {"plan": plan, "untracked": untracked, "author": AUTHOR})
        entry["plan"] = _layout(path)
        if name == "toc":
            entry["tocs"] = _tocs(path)
        if not _remaining(path):
            sys.exit(f"{name}: no tracked change was made; nothing to check in Word")
        expected["documents"][name] = entry
    base.unlink()
    shutil.rmtree(store)
    (out / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=1), "utf-8")
    print(f"Wrote {len(expected['documents'])} documents to {out}.")
    if not with_toc:
        print("LibreOffice was not found, so toc.docx was not built (install it to check a TOC).")
    for name, entry in expected["documents"].items():
        print(f"  {name}.docx: {entry['look']}")
    print("Now follow docs/real-word-check.md (Layout).")


def mismatches(want: Any, got: Any, path: str = "plan") -> list[str]:
    """Where ``got`` differs from ``want``. A section's ``from`` is compared by its paragraph
    hash, as Word's own paragraphs may renumber what follows a section break."""
    if isinstance(want, dict) and isinstance(got, dict):
        keys = list(want) + [k for k in got if k not in want]
        return [d for k in keys for d in mismatches(want.get(k), got.get(k), f"{path}.{k}")]
    if isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            return [f"{path}: {len(got)} entries, not {len(want)}"]
        return [
            d
            for i, (w, g) in enumerate(zip(want, got, strict=True))
            for d in mismatches(w, g, f"{path}[{i}]")
        ]
    if path.endswith(".from") and isinstance(want, str) and isinstance(got, str):
        same = want.partition("#")[2] == got.partition("#")[2]
        return [] if same else [f"{path}: {got!r}, not {want!r}"]
    return [] if want == got else [f"{path}: {got!r}, not {want!r}"]


def verify(out: Path) -> int:
    expected = json.loads((out / "expected.json").read_text("utf-8"))
    problems: list[str] = []
    checks = [(name, "accepted", entry) for name, entry in expected["documents"].items()]
    if REJECTED in expected["documents"]:
        checks.append((REJECTED, "rejected", {"plan": expected["original"]}))
    for name, suffix, entry in checks:
        file = f"{name}-{suffix}.docx"
        path = out / file
        if not path.exists():
            problems.append(f"{file}: missing (save it from Word as described)")
            continue
        if (left := _remaining(path)) > 0:
            step = "Accept All" if suffix == "accepted" else "Reject All"
            problems.append(f"{file}: {left} tracked changes left; was {step} done?")
        problems += [f"{file}: {d}" for d in mismatches(entry["plan"], _layout(path))]
        if "tocs" in entry and (got := " | ".join(_tocs(path))) != (
            want := " | ".join(entry["tocs"])
        ):
            at = next((i for i, (a, b) in enumerate(zip(got, want, strict=False)) if a != b), 0)
            problems.append(
                f"{file}: the TOC Word updated differs from character {at}: "
                f"{got[at : at + 60]!r}, not {want[at : at + 60]!r}"
            )
    for line in problems:
        print("MISMATCH", line)
    print(f"real_word_layout_mismatches == {len(problems)}")
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
