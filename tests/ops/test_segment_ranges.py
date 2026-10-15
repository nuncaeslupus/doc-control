"""`doc.segment` sentences and dialogue over a paragraph run or a unit id (T43).

Gate: ``segment_range_union_mismatches == 0``. For every chapter and scene of a generated
novel, and for random runs of whole paragraphs, the sentences and dialogue lines that one
``doc.segment`` call returns for the unit equal the union of the per-paragraph calls.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from tests.corpus.generator import generate


def test_range_by_span_and_unit_id_equals_per_paragraph_union(tmp_path: Path) -> None:
    path = tmp_path / "novel.docx"
    path.write_bytes(generate("novel", 43))
    rng = random.Random(43)
    mismatches = checked = 0
    with api.open(path) as doc:
        paras = doc.paragraphs()
        per: dict[str, dict[int, list[Any]]] = {}
        for unit in ("sentences", "dialogue"):
            rows = doc.run("doc.segment", {"unit": unit})[unit]
            per[unit] = {}
            for row in rows:
                number = int(row["at"][1:].split("#")[0])
                per[unit].setdefault(number, []).append(row)
        chapters = doc.run("doc.segment", {})["chapters"]
        scenes = doc.run("doc.segment", {"unit": "scenes"})["scenes"]
        ranges: list[tuple[str, int, int]] = [(c["id"], c["first"], c["last"]) for c in chapters]
        ranges += [(s["id"], s["first"], s["last"]) for s in rng.sample(scenes, 25)]
        for _ in range(25):
            a = rng.randrange(len(paras))
            b = min(len(paras) - 1, a + rng.randrange(1, 60))
            ranges.append((f"{paras[a]['at']}..{paras[b]['at']}", a + 1, b + 1))
        for value, first, last in ranges:
            for unit in ("sentences", "dialogue"):
                got = doc.run("doc.segment", {"unit": unit, "range": value})[unit]
                want = [r for n in range(first, last + 1) for r in per[unit].get(n, [])]
                mismatches += got != want
                checked += 1
    print(f"segment_range_union_mismatches == {mismatches} (of {checked} calls)")
    assert len(chapters) > 3 and mismatches == 0


def test_unknown_unit_id_and_backwards_run_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "novel.docx"
    path.write_bytes(generate("novel", 43))
    with api.open(path) as doc:
        paras = doc.paragraphs()
        for value in ("ch-00000000", f"{paras[5]['at']}..{paras[2]['at']}"):
            with pytest.raises(DocControlError) as err:
                doc.run("doc.segment", {"unit": "dialogue", "range": value})
            assert err.value.code is ErrorCode.ADDRESS_INVALID
