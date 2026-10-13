"""Exact anchors on normalised text (T41).

Gate: ``exact_quotes_not_found_after_normalisation == 0``. Agents quote from the
normalised text (``text.normalized``), so ``anchor.resolve`` in ``mode: "exact"`` must find a
quote across soft hyphens, zero-width characters, tabs, no-break and double spaces in the
document, and return a range whose own text normalises to the quote.
"""

from __future__ import annotations

import random
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from doc_control.envelope import Reply
from doc_control.model.index import build_index
from doc_control.model.normalized import normalized_text
from doc_control.registry import REGISTRY, load_operations
from tests.corpus.generator import generate
from tests.helpers import make_docx

SPACES = (" ", " ", "  ", "\t", "\u00a0", " \u200b")
NOISE = ("\u00ad", "\u200b", "\u200d", "\ufeff")
AT = re.compile(r"p(\d+)#[0-9a-f]{8}:(\d+)-(\d+)")


def run(op: str, **request: Any) -> dict[str, Any]:
    load_operations()
    reply = REGISTRY[op].run(request)
    assert isinstance(reply, Reply)
    return reply.result


def noisy(text: str, rng: random.Random) -> str:
    """``text`` with invisible characters in words and odd whitespace between them."""
    out: list[str] = []
    for ch in text:
        if ch == " ":
            out.append(rng.choice(SPACES))
        else:
            out.append(ch)
            if ch.isalpha() and rng.random() < 0.08:
                out.append(rng.choice(NOISE))
    return "".join(out)


def test_exact_quotes_match_on_normalised_text(tmp_path: Path) -> None:
    rng = random.Random(41)
    data = zipfile.ZipFile(BytesIO(generate("novel", 3))).read("word/document.xml")
    clean = [p.text for p in build_index(data).paragraphs if len(p.text) > 200][:60]
    texts = [noisy(t, rng) for t in clean]
    doc = make_docx(tmp_path / "noisy.docx", *texts)
    missed = 0
    for n, text in enumerate(texts, 1):
        flat = normalized_text(text)
        words = [m.start() for m in re.finditer(r"(?<= )\S", flat)]
        start = rng.choice(words[2:-12])
        end = next(m.end() for m in re.finditer(r"\S+", flat) if m.start() > start + 40)
        quote = flat[start:end]
        anchor = {"exact": quote, "prefix": flat[max(0, start - 24) : start]}
        try:
            found = run("anchor.resolve", doc=str(doc), anchor=anchor, mode="exact")
        except Exception as err:
            print(repr(err)[:300])
            missed += 1
            continue
        m = AT.fullmatch(found["at"])
        assert m is not None and found["match"] == "exact"
        para, lo, hi = int(m[1]), int(m[2]), int(m[3])
        if para != n or normalized_text(text[lo:hi]) != quote:
            print(para, n, repr(normalized_text(text[lo:hi])[:60]), repr(quote[:60]))
            missed += 1
    print(f"exact_quotes_not_found_after_normalisation == {missed} (of {len(texts)} quotes)")
    assert missed == 0


@pytest.mark.parametrize(
    ("text", "quote"),
    [
        ("la ca­sa  blanca", "casa blanca"),
        ("la casa blanca", "casa  blanca"),
        ("la\tcasa​ blanca", "la casa blanca"),
    ],
)
def test_small_cases(tmp_path: Path, text: str, quote: str) -> None:
    doc = make_docx(tmp_path / "d.docx", "Otra cosa.", text)
    found = run("anchor.resolve", doc=str(doc), anchor={"exact": quote}, mode="exact")
    m = AT.fullmatch(found["at"])
    assert m is not None and int(m[1]) == 2
    assert normalized_text(text[int(m[2]) : int(m[3])]) == normalized_text(quote)
