"""How a .txt splits into paragraphs: auto-detected hard wrapping, or ``text_mode`` (T55).

The gate ``text_mode_mismatches == 0`` checks: hard-wrapped prose stays in blocks mode with
its paragraphs unchanged; a writer's export (one paragraph per line, empty lines only
around headings) is read one paragraph per line; verse read with ``text_mode="lines"``
keeps every line; the mode is reported in ``doc.source`` and settable by environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.plaintext import paragraphs, text_mode

LONG = "She said that the house had been empty for years, and nobody in the village "
EXPORT = (
    "CHAPTER ONE\n\n"
    + "".join(f"{LONG}line {n} goes on long enough to pass the wrap limit.\n" for n in range(6))
    + "\nCHAPTER TWO\n\n"
    + "".join(f"—Short line {n}.\n" for n in range(4))
    + f"{LONG}and one long paragraph closes the chapter for good.\n"
)
WRAPPED = "".join(
    f"Paragraph {p} line {k} of hard-wrapped prose set to about seventy columns\n"
    + ("\n" if k == 3 else "")
    for p in range(5)
    for k in range(4)
)
VERSE = "Roses are red,\nviolets are blue,\n\nsugar is sweet,\nand so are you.\n"


def _texts(path: Path, mode: str | None = None) -> tuple[list[str], dict[str, str]]:
    with api.open(path, text_mode=mode) as doc:
        reply = doc.reply("paragraphs.list")
        assert reply.doc is not None
        return [r["text"] for r in reply.result["paragraphs"]], reply.doc["source"]


def test_text_mode_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checks: list[bool] = []
    export, wrapped, verse = (tmp_path / n for n in ("export.txt", "wrapped.txt", "verse.txt"))
    export.write_text(EXPORT, "utf-8")
    wrapped.write_text(WRAPPED, "utf-8")
    verse.write_text(VERSE, "utf-8")

    texts, source = _texts(wrapped)
    checks += [source["mode"] == "blocks", len(texts) == 5]
    texts, source = _texts(export)
    lines = EXPORT.splitlines()
    checks += [source["mode"] == "lines", texts == lines]
    checks.append([t for t in texts if t] == [line for line in lines if line])
    texts, source = _texts(verse, "lines")
    checks += [source["mode"] == "lines", texts == VERSE.splitlines()]
    texts, _ = _texts(verse, "blocks")
    checks.append(texts == ["Roses are red, violets are blue,", "sugar is sweet, and so are you."])
    monkeypatch.setenv("DOC_CONTROL_TEXT_MODE", "lines")
    texts, source = _texts(wrapped)
    checks += [source["mode"] == "lines", len(texts) == len(WRAPPED.splitlines())]
    mismatches = checks.count(False)
    print(f"text_mode_mismatches == {mismatches} (of {len(checks)} checks)")
    assert mismatches == 0


def test_no_empty_line_or_a_single_block_is_lines() -> None:
    assert paragraphs("a\nb\n", markdown=False)[1] == "lines"
    assert paragraphs("\nshort\nlines\n\n", markdown=False)[1] == "lines"


def test_markdown_ignores_text_mode() -> None:
    with text_mode("lines"):
        assert paragraphs("a\nb\n", markdown=True)[1] == "markdown"


def test_unknown_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DocControlError) as err:
        api.open(tmp_path / "x.txt", text_mode="words")
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_unknown_environment_mode_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.txt"
    path.write_text("a\nb\n", "utf-8")
    monkeypatch.setenv("DOC_CONTROL_TEXT_MODE", "bogus")
    with api.open(path) as doc, pytest.raises(DocControlError) as err:
        doc.run("info")
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_auto_reads_short_stanzas_as_blocks() -> None:
    # Documented limit: verse in short stanzas looks wrapped; read it with text_mode="lines".
    assert paragraphs(VERSE, markdown=False)[1] == "blocks"
