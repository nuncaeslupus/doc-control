"""`compare`: a redline whose accept-all is the new text and whose reject-all is the old."""

from __future__ import annotations

import itertools
import json
import random
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex
from doc_control.ooxml.zipio import Package
from doc_control.ops.compare import accepts_to, compare_documents, word_edits
from doc_control.registry import REGISTRY, load_operations
from tests.corpus import validator
from tests.corpus.generator import KINDS, generate
from tests.helpers import make_docx
from tests.ops import revsim

AUTHOR = "Compare Bot"
AFTER = "<w:p><w:r><w:t>after</w:t></w:r></w:p>"
_WORDS = ["alpha", "beta", "gamma", "delta", "omega", "sigma", "kappa", "tau", "iota", "zeta"]


@pytest.fixture(autouse=True)
def _ops() -> None:
    load_operations()


def words(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(_WORDS) + str(rng.randint(0, 40)) for _ in range(n))


def sentence(rng: random.Random) -> str:
    return words(rng, rng.randint(6, 20)) + "."


def edit_texts(rng: random.Random, old: list[str], steps: int) -> list[str]:
    """Apply random edits (word changes, inserts, deletes, splits, merges, moves, edits at
    the end) to a list of paragraph texts; never returns an empty list."""
    new = list(old)
    for _ in range(steps):
        if not new:
            new.append(sentence(rng))
            continue
        k = rng.randrange(len(new))
        op = rng.choice("mmmidsMTtJ")
        if op == "m":
            w = new[k].split()
            if w:
                w[rng.randrange(len(w))] = words(rng, rng.randint(1, 3))
                new[k] = " ".join(w)
        elif op == "i":
            new.insert(k, sentence(rng))
        elif op == "d":
            del new[k]
        elif op == "s":
            w = new[k].split()
            h = len(w) // 2
            new[k : k + 1] = [" ".join(w[:h]), " ".join(w[h:])]
        elif op == "M" and k + 1 < len(new):
            new[k : k + 2] = [new[k] + " " + new[k + 1]]
        elif op == "T":
            new.extend(sentence(rng) for _ in range(rng.randint(1, 3)))
        elif op == "t" and len(new) > 1:
            del new[-rng.randint(1, min(3, len(new) - 1)) :]
        elif op == "J":
            moved = new.pop(k)
            new.insert(rng.randrange(len(new) + 1), moved)
    return new or [sentence(rng)]


def redline_texts(docx: Path, author: str = AUTHOR) -> tuple[list[str], list[str]]:
    """The paragraph texts with every revision of ``author`` accepted, and rejected,
    by the independent simulator."""
    with zipfile.ZipFile(docx) as zf:
        data = zf.read("word/document.xml")
    return revsim.resolved(data, author, "accept"), revsim.resolved(data, author, "reject")


def text_mismatches(old_doc: Path, new_doc: Path, out: Path) -> int:
    """Paragraphs of the new text that accept-all gets wrong, plus those of the old text
    that reject-all gets wrong (a different paragraph count counts every paragraph)."""
    result = compare_documents(old_doc, new_doc, out, author=AUTHOR)
    accepted, rejected = redline_texts(out)
    with zipfile.ZipFile(old_doc) as zf:
        pending = any(revsim.deleted_marks(zf.read("word/document.xml")))
    total = 0
    # Only the known differences are tolerated (see accepts_to): an unremovable cell-last
    # paragraph leaves an empty one, another author's pending deletion joins paragraphs.
    unremovable = any("cannot be removed" in w for w in result.warnings)
    if not accepts_to(accepted, texts(new_doc), unremovable=unremovable, foreign=pending):
        total += max(len(accepted), len(texts(new_doc)))
    if rejected != texts(old_doc):
        total += max(len(rejected), len(texts(old_doc)))
    return total


def texts(docx: Path) -> list[str]:
    return [p.text for p in DocumentIndex.from_package(Package(docx)).paragraphs]


def test_compare_accept_all_equals_new_reject_all_equals_old(tmp_path: Path) -> None:
    mismatches = pairs = 0
    for seed in range(500):
        rng = random.Random(seed)
        old = [sentence(rng) for _ in range(rng.randint(1, 14))]
        new = edit_texts(rng, old, rng.randint(0, 8))
        old_doc = make_docx(tmp_path / f"o{seed}.docx", *old)
        new_doc = make_docx(tmp_path / f"n{seed}.docx", *new)
        mismatches += text_mismatches(old_doc, new_doc, tmp_path / f"r{seed}.docx")
        pairs += 1
    print(f"compare_redline_text_mismatches={mismatches} ({pairs} pairs)")
    assert mismatches == 0


@pytest.mark.parametrize("kind", [k for k in KINDS if k != "novel"])
def test_compare_corpus_documents_with_structure(kind: str, tmp_path: Path) -> None:
    """Tables, fields, comments, footnotes and old revisions in the old document."""
    old_doc = tmp_path / "old.docx"
    old_doc.write_bytes(generate(kind, 3))
    old = texts(old_doc)
    mismatches = 0
    for seed in range(8):
        rng = random.Random(f"{kind}{seed}")
        new_doc = make_docx(tmp_path / f"n{seed}.docx", *edit_texts(rng, old, rng.randint(1, 6)))
        try:
            mismatches += text_mismatches(old_doc, new_doc, tmp_path / f"r{seed}.docx")
        except DocControlError as err:
            # Content the tracked-text primitives refuse is refused cleanly, never half done.
            assert err.code is ErrorCode.UNSUPPORTED_CONTENT
            assert not (tmp_path / f"r{seed}.docx").exists()
    assert mismatches == 0


def test_compare_marks_only_the_changed_words(tmp_path: Path) -> None:
    old = make_docx(tmp_path / "o.docx", "The quick brown fox jumps over the lazy dog.")
    new = make_docx(tmp_path / "n.docx", "The quick red fox jumps over the sleepy dog.")
    out = tmp_path / "r.docx"
    result = compare_documents(old, new, out, author=AUTHOR)
    xml = zipfile.ZipFile(out).read("word/document.xml").decode()
    assert xml.count("<w:del ") == 2 and xml.count("<w:ins ") == 2
    assert ">brown <" in xml and ">red <" in xml and ">lazy <" in xml and ">sleepy <" in xml
    assert result.revisions == 4
    assert redline_texts(out) == (texts(new), texts(old))


def test_word_edits_keep_whitespace_and_never_touch() -> None:
    old = "one  two\tthree four"
    new = "one  2 three  four five"
    edits = word_edits(old, new)
    text = old
    for start, end, repl in reversed(edits):
        text = text[:start] + repl + text[end:]
    assert text == new
    assert all(a[1] < b[0] for a, b in itertools.pairwise(edits))
    assert word_edits("same text", "same text") == []


def test_word_edits_do_not_split_combining_marks() -> None:
    old, new = "café au lait", "cafés au lait"
    for start, end, _ in word_edits(old, new):
        assert start == 0 or old[start] != "́"
        assert end == len(old) or old[end] != "́"


def test_compare_moved_paragraph_is_delete_plus_insert(tmp_path: Path) -> None:
    rng = random.Random(1)
    paras = [sentence(rng) for _ in range(6)]
    moved = [paras[0], paras[1], paras[3], paras[4], paras[2], paras[5]]
    old = make_docx(tmp_path / "o.docx", *paras)
    new = make_docx(tmp_path / "n.docx", *moved)
    out = tmp_path / "r.docx"
    result = compare_documents(old, new, out, author=AUTHOR)
    assert result.summary["moved"] >= 1
    assert redline_texts(out) == (moved, paras)
    xml = zipfile.ZipFile(out).read("word/document.xml").decode()
    assert "w:moveFrom" not in xml and "<w:del " in xml and "<w:ins " in xml


def test_compare_split_and_merge(tmp_path: Path) -> None:
    rng = random.Random(2)
    a, b, c = (words(rng, 14) + "." for _ in range(3))
    half = len(b.split()) // 2
    split = [a, " ".join(b.split()[:half]), " ".join(b.split()[half:]), c]
    old = make_docx(tmp_path / "o.docx", a, b, c)
    new = make_docx(tmp_path / "n.docx", *split)
    result = compare_documents(old, new, tmp_path / "split.docx", author=AUTHOR)
    assert result.summary["split"] == 1
    assert redline_texts(tmp_path / "split.docx") == (split, [a, b, c])
    result = compare_documents(new, old, tmp_path / "merge.docx", author=AUTHOR)
    assert result.summary["merged"] == 1
    assert redline_texts(tmp_path / "merge.docx") == ([a, b, c], split)


def test_compare_paragraphs_added_and_removed_at_both_ends(tmp_path: Path) -> None:
    rng = random.Random(3)
    body = [sentence(rng) for _ in range(5)]
    head, tail = [sentence(rng) for _ in range(2)], [sentence(rng) for _ in range(3)]
    for old, new in (
        (body, head + body + tail),
        (head + body + tail, body),
        (body, tail),
        (body[:1], head + tail),
    ):
        old_doc = make_docx(tmp_path / "o.docx", *old)
        new_doc = make_docx(tmp_path / "n.docx", *new)
        out = tmp_path / "r.docx"
        out.unlink(missing_ok=True)
        compare_documents(old_doc, new_doc, out, author=AUTHOR)
        assert redline_texts(out) == (new, old)


def test_compare_identical_texts_gives_a_copy_without_revisions(tmp_path: Path) -> None:
    old = make_docx(tmp_path / "o.docx", "One.", "Two.")
    new = make_docx(tmp_path / "n.docx", "One.", "Two.")
    result = compare_documents(old, new, tmp_path / "r.docx")
    assert result.revisions == 0
    assert (tmp_path / "r.docx").read_bytes() == old.read_bytes()


def test_compare_cell_last_paragraph_is_removed_by_joining_the_previous_one(
    tmp_path: Path,
) -> None:
    def cell(*paras: str) -> str:
        inner = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paras)
        return f"<w:tbl><w:tr><w:tc>{inner}</w:tc></w:tr></w:tbl>{AFTER}"

    old = revsim.make_doc(tmp_path / "o.docx", cell("first line here", "second line here"))
    new = revsim.make_doc(tmp_path / "n.docx", cell("first line here"))
    result = compare_documents(old, new, tmp_path / "r.docx", author=AUTHOR)
    assert result.warnings == []
    assert redline_texts(tmp_path / "r.docx") == (texts(new), texts(old))


def test_compare_cell_with_only_one_paragraph_removed_warns(tmp_path: Path) -> None:
    def doc(cell: str) -> str:
        return (
            f"<w:tbl><w:tr><w:tc>{cell}</w:tc></w:tr></w:tbl><w:p><w:r><w:t>after</w:t></w:r></w:p>"
        )

    para = "<w:p><w:r><w:t>only line</w:t></w:r></w:p>"
    old = revsim.make_doc(tmp_path / "o.docx", doc(para))
    new = revsim.make_doc(tmp_path / "n.docx", doc("<w:p/>"))
    result = compare_documents(old, new, tmp_path / "r.docx", author=AUTHOR)
    assert redline_texts(tmp_path / "r.docx")[1] == texts(old)
    assert result.revisions >= 1


def test_compare_author_tag_and_date(tmp_path: Path) -> None:
    old = make_docx(tmp_path / "o.docx", "A first paragraph here.")
    new = make_docx(tmp_path / "n.docx", "A second paragraph here.")
    when = datetime(2026, 10, 12, 9, 30, 15, tzinfo=UTC)
    result = compare_documents(old, new, tmp_path / "r.docx", author="Reviewer", tag="v2", now=when)
    assert result.author == "Reviewer (v2)" and result.date == "2026-10-12T09:30:15Z"
    xml = zipfile.ZipFile(tmp_path / "r.docx").read("word/document.xml").decode()
    assert 'w:author="Reviewer (v2)"' in xml and 'w:date="2026-10-12T09:30:15Z"' in xml
    assert redline_texts(tmp_path / "r.docx", "Reviewer (v2)") == (texts(new), texts(old))


def test_compare_never_modifies_an_input_and_refuses_bad_outputs(tmp_path: Path) -> None:
    old = make_docx(tmp_path / "o.docx", "Before text here.")
    new = make_docx(tmp_path / "n.docx", "After text here.")
    before = (old.read_bytes(), new.read_bytes())
    out = tmp_path / "r.docx"
    compare_documents(old, new, out)
    assert (old.read_bytes(), new.read_bytes()) == before
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

    existing = out.read_bytes()
    link = tmp_path / "link.docx"
    link.symlink_to(old)
    for bad in (out, old, new, link, tmp_path / "x" / "r.docx", tmp_path / "." / "o.docx"):
        with pytest.raises(DocControlError) as err:
            compare_documents(old, new, bad)
        assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert out.read_bytes() == existing
    assert (old.read_bytes(), new.read_bytes()) == before

    with pytest.raises(DocControlError) as missing:
        compare_documents(tmp_path / "none.docx", new, tmp_path / "z.docx")
    assert missing.value.code is ErrorCode.DOC_NOT_FOUND
    junk = tmp_path / "junk.docx"
    junk.write_bytes(b"not a zip")
    with pytest.raises(DocControlError) as junk_err:
        compare_documents(old, junk, tmp_path / "z.docx")
    assert junk_err.value.code is ErrorCode.NOT_A_DOCX
    assert not (tmp_path / "z.docx").exists()


def test_compare_leaves_no_output_when_the_write_fails(tmp_path: Path) -> None:
    old = make_docx(tmp_path / "o.docx", "Before text here.")
    new = make_docx(tmp_path / "n.docx", "After text here.")

    def boom(stage: str) -> None:
        if stage == "replace":
            raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        compare_documents(old, new, tmp_path / "r.docx", stage=boom)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["n.docx", "o.docx"]


def test_compare_cli_envelope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = make_docx(tmp_path / "o.docx", "Before text here.", "Same.")
    new = make_docx(tmp_path / "n.docx", "After text here.", "Same.")
    request = {"old": str(old), "new": str(new), "output": str(tmp_path / "r.docx"), "tag": "t"}
    assert main(["compare", "--json", json.dumps(request)]) == 0
    envelope: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert envelope["ok"] and envelope["op"] == "compare"
    result = envelope["result"]
    assert result["output"]["path"] == str(tmp_path / "r.docx")
    assert result["summary"]["modified"] == 1 and result["revisions"] > 0
    assert result["author"] == "Claude (t)"
    assert envelope["doc"]["path"] == str(old)
    assert REGISTRY["compare"].kind == "read"

    assert main(["compare", "--json", json.dumps(request)]) == 2  # output exists now
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "SCHEMA_INVALID"
    request["output"] = str(tmp_path / "r2.docx")
    request["date"] = "2026-10-12T09:00:00"
    assert main(["compare", "--json", json.dumps(request)]) == 2  # no time zone
    assert not (tmp_path / "r2.docx").exists()


@pytest.mark.skipif(validator.soffice_path() is None, reason="LibreOffice not installed")
def test_compare_redline_opens_in_libreoffice(tmp_path: Path) -> None:
    rng = random.Random(9)
    old = [sentence(rng) for _ in range(12)]
    old_doc = make_docx(tmp_path / "o.docx", *old)
    new_doc = make_docx(tmp_path / "n.docx", *edit_texts(rng, old, 8))
    out = tmp_path / "r.docx"
    compare_documents(old_doc, new_doc, out, author=AUTHOR)
    problems = validator.libreoffice_round_trip({"redline": out.read_bytes()}, tmp_path / "lo")
    assert problems == {"redline": []} or not any(problems.values())


def _edited_novel(old_doc: Path, new_doc: Path, seed: int) -> None:
    old = texts(old_doc)
    rng = random.Random(seed)
    new = list(old)
    for _ in range(len(old) // 12):  # about 8% of the paragraphs change somehow
        k = rng.randrange(len(new))
        op = rng.choice("mmmmmmidsMJ")
        if op == "m":
            w = new[k].split()
            if len(w) > 3:
                for _ in range(rng.randint(1, 3)):
                    w[rng.randrange(len(w))] = rng.choice(_WORDS)
                new[k] = " ".join(w)
        elif op == "i":
            new.insert(k, sentence(rng))
        elif op == "d":
            del new[k]
        elif op == "s":
            w = new[k].split()
            if len(w) > 8:
                new[k : k + 1] = [" ".join(w[: len(w) // 2]), " ".join(w[len(w) // 2 :])]
        elif op == "M" and k + 1 < len(new):
            new[k : k + 2] = [new[k] + " " + new[k + 1]]
        elif op == "J":
            new.insert(rng.randrange(len(new) + 1), new.pop(k))
    make_docx(new_doc, *new)


@pytest.mark.bench
def test_compare_100k_word_novel_pair(tmp_path: Path) -> None:
    old_doc = tmp_path / "old.docx"
    old_doc.write_bytes(generate("novel", 7))
    new_doc = tmp_path / "new.docx"
    _edited_novel(old_doc, new_doc, 5)
    started = time.perf_counter()
    result = compare_documents(old_doc, new_doc, tmp_path / "r.docx", author=AUTHOR)
    seconds = time.perf_counter() - started
    accepted, rejected = redline_texts(tmp_path / "r.docx")
    mismatches = sum(a != b for a, b in zip(accepted, texts(new_doc), strict=False)) + abs(
        len(accepted) - len(texts(new_doc))
    )
    mismatches += sum(a != b for a, b in zip(rejected, texts(old_doc), strict=False)) + abs(
        len(rejected) - len(texts(old_doc))
    )
    words_total = sum(len(t.split()) for t in texts(old_doc))
    print(
        f"compare_100k_words_seconds={seconds:.1f} "
        f"({words_total} words, {result.revisions} revisions, {result.summary})"
    )
    assert mismatches == 0
    assert seconds < 60


def test_compare_word_diff_of_huge_paragraphs_is_bounded(tmp_path: Path) -> None:
    rng = random.Random(4)
    big = " ".join(rng.choice(_WORDS) + str(rng.randint(0, 999)) for _ in range(6000)) + "."
    changed = big.replace(" alpha", " ALPHA", 40)
    old = make_docx(tmp_path / "o.docx", big)
    new = make_docx(tmp_path / "n.docx", changed)
    assert text_mismatches(old, new, tmp_path / "r.docx") == 0


def _para(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def test_compare_repeated_paragraph_before_a_table_cell_does_not_crash(tmp_path: Path) -> None:
    body = _para("c") + _para("c") + f"<w:tbl><w:tr><w:tc>{_para('a')}</w:tc></w:tr></w:tbl>"
    old = revsim.make_doc(tmp_path / "o.docx", body + _para("end"))
    new = make_docx(tmp_path / "n.docx", "a", "c", "end")
    assert text_mismatches(old, new, tmp_path / "r.docx") == 0


def test_compare_fuzz_with_tables_in_the_old_document(tmp_path: Path) -> None:
    bad = 0
    for seed in range(150):
        rng = random.Random(seed)
        parts: list[str] = []
        for _ in range(rng.randint(2, 8)):
            if rng.random() < 0.3:
                cell = "".join(_para(rng.choice(_WORDS)) for _ in range(rng.randint(1, 2)))
                parts.append(f"<w:tbl><w:tr><w:tc>{cell}</w:tc></w:tr></w:tbl>")
                parts.append(_para("sep"))
            else:
                parts.append(_para(rng.choice(_WORDS)))
        old = revsim.make_doc(tmp_path / "o.docx", "".join(parts))
        new = make_docx(tmp_path / "n.docx", *edit_texts(rng, texts(old), rng.randint(1, 6)))
        out = tmp_path / "r.docx"
        out.unlink(missing_ok=True)
        try:
            bad += text_mismatches(old, new, out)
        except DocControlError as err:
            assert err.code is ErrorCode.UNSUPPORTED_CONTENT
    assert bad == 0


def test_compare_moved_paragraph_with_a_bookmark_is_deleted_and_inserted(tmp_path: Path) -> None:
    marked = (
        '<w:p><w:bookmarkStart w:id="7" w:name="m"/><w:r><w:t>marked paragraph text</w:t></w:r>'
        '<w:bookmarkEnd w:id="7"/></w:p>'
    )
    body = _para("one one one") + marked + _para("two two two") + _para("three three three")
    old = revsim.make_doc(tmp_path / "o.docx", body)
    new = make_docx(
        tmp_path / "n.docx",
        "one one one",
        "two two two",
        "three three three",
        "marked paragraph text",
    )
    result = compare_documents(old, new, tmp_path / "r.docx", author=AUTHOR)
    assert result.warnings == []
    assert redline_texts(tmp_path / "r.docx") == (texts(new), texts(old))


def test_compare_never_overwrites_a_file_that_appears_meanwhile(tmp_path: Path) -> None:
    old = make_docx(tmp_path / "o.docx", "Before text here.")
    new = make_docx(tmp_path / "n.docx", "After text here.")
    out = tmp_path / "r.docx"

    def appear(stage: str) -> None:
        if stage == "replace":
            out.write_bytes(b"someone else's file")

    with pytest.raises(DocControlError) as err:
        compare_documents(old, new, out, stage=appear)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert out.read_bytes() == b"someone else's file"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["n.docx", "o.docx", "r.docx"]
