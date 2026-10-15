"""`para.locate` phrases are pinned by a golden file (FIND T1: direction b stays untouched).

The golden file holds, per corpus kind, every paragraph's generated phrases as `para.locate` returns
them: `phrase`, `find_phrase`, `find_phrase_word`, `find_phrase_across` and the fields that
explain a missing one. The novel is long, so it is pinned by a digest of the same rows.
Regenerate on purpose with ``uv run python -m tests.ops.test_locate_golden``.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from doc_control.cli import main
from tests.conftest import write_corpus_doc
from tests.corpus.generator import KINDS

GOLDEN = Path(__file__).parent / "data" / "locate_golden.json"
SEED = 1
#: Kinds pinned by digest rather than row by row.
DIGESTED = frozenset({"novel"})
FIELDS = (
    "at",
    "phrase",
    "find_phrase",
    "find_phrase_unique",
    "find_phrase_word",
    "find_phrase_words",
    "find_phrase_across",
    "find_phrase_reason",
    "context",
)


def locate_page(request: dict[str, Any]) -> dict[str, Any]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["para.locate", "--json", json.dumps(request)])
    assert code == 0, out.getvalue()
    reply: dict[str, Any] = json.loads(out.getvalue())
    return reply


def locate_rows(path: Path) -> list[dict[str, Any]]:
    """Every row `para.locate` returns for ``path``, paged through the CLI, cut to ``FIELDS``."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        request: dict[str, Any] = {"doc": str(path)}
        if cursor:
            request["cursor"] = cursor
        reply = locate_page(request)
        rows += [{k: r[k] for k in FIELDS if k in r} for r in reply["result"]["paragraphs"]]
        cursor = reply["next"]
        if cursor is None:
            return rows


def entry(rows: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    if kind in DIGESTED:
        text = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {"paragraphs": len(rows), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    return {"paragraphs": len(rows), "rows": rows}


def snapshot() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        return {
            kind: entry(locate_rows(write_corpus_doc(Path(tmp), kind, SEED)), kind)
            for kind in KINDS
        }


def render(golden: dict[str, Any]) -> str:
    return json.dumps(golden, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def test_locate_output_matches_golden() -> None:
    """`para.locate` on every corpus kind is byte-identical to the golden file."""
    assert render(snapshot()) == GOLDEN.read_text(encoding="utf-8")


def test_golden_covers_every_kind() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(golden) == sorted(KINDS)
    assert all(golden[kind]["paragraphs"] > 0 for kind in KINDS)


if __name__ == "__main__":
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(render(snapshot()), encoding="utf-8")
    sys.stderr.write(f"wrote {GOLDEN}\n")
