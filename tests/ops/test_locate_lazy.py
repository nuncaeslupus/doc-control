"""Lazy ``locate`` (T53): a few paragraphs answered without the whole-document indexes.

The gate ``lazy_locate_mismatches == 0``: for every paragraph of every corpus kind and for
every seventh paragraph of the 100k-word novel, the lazy locator and find phrase equal the
whole-document indexes', and ``locate`` rows asked one by one equal the whole-document rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_control import api
from doc_control.model.document import Document
from doc_control.ops import locate as locate_module
from doc_control.ops.locate import LazyIndexes, _indexes
from tests.corpus.generator import KINDS, generate


def _texts(path: Path) -> list[str]:
    index = Document(str(path)).index
    return [index.paragraph(n).text for n in range(1, len(index) + 1)]


def test_lazy_locate_gate(tmp_path: Path) -> None:
    checks = mismatches = 0
    for kind in (*KINDS, "novel"):
        path = tmp_path / f"{kind}.docx"
        path.write_bytes(generate(kind, 1))
        texts = _texts(path)
        locators, finds = _indexes(texts)
        lazy = LazyIndexes(texts)
        step = 7 if kind == "novel" else 1
        for i in range(0, len(texts), step):
            checks += 1
            mismatches += lazy.locator(i) != locators.locator(i) or lazy.find(i) != finds[i]
        if kind == "novel":
            continue
        with api.open(path) as doc:
            whole = {row["at"]: row for row in doc.run("locate")["paragraphs"]}
        for at, row in whole.items():
            with api.open(path) as doc:  # a fresh handle: no whole-document index memoised
                checks += 1
                mismatches += doc.run("locate", {"at": at})["paragraphs"] != [row]
    print(f"lazy_locate_mismatches == {mismatches} (of {checks} checks)")
    assert mismatches == 0


def test_duplicate_and_affix_cases() -> None:
    texts = ["Uno dos tres.", "uno DOS tres.", "Straße hier", "strasse dort", "", "a b a b a"]
    locators, finds = _indexes(texts)
    lazy = LazyIndexes(texts)
    for i in range(len(texts)):
        assert lazy.locator(i) == locators.locator(i)
        assert lazy.find(i) == finds[i]
    assert lazy.find(0).across == "next"  # a repeated paragraph, told apart across its mark


def test_over_budget_switches_to_the_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kind.docx"
    path.write_bytes(generate(KINDS[0], 1))
    with api.open(path) as doc:
        whole = doc.run("locate")["paragraphs"][:5]
    monkeypatch.setattr(locate_module, "LAZY_BUDGET", 0.0)
    for row in whole:
        with api.open(path) as doc:
            assert doc.run("locate", {"at": row["at"]})["paragraphs"] == [row]


def test_long_shared_edges_and_line_breaks() -> None:
    body = " ".join(f"w{k}" for k in range(30))
    texts = [f"Same start {body} end", f"Same start {body} other end", "a\nb c", "a b\nc", "b c"]
    texts += [f"x {' '.join(['y'] * 14)} z{k}" for k in range(3)]
    locators, finds = _indexes(texts)
    lazy = LazyIndexes(texts)
    for i in range(len(texts)):
        assert lazy.locator(i) == locators.locator(i)
        assert lazy.find(i) == finds[i]
