"""`diff`: paragraph alignment between two versions, against known edits."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import fingerprint
from doc_control.model.align import GROUP_MIN_SIMILARITY, MODIFIED_MIN, Alignment, align
from doc_control.model.index import DocumentIndex
from doc_control.ooxml.zipio import Package
from doc_control.ops.diff import address_map, diff_documents
from doc_control.ops.diff import diff as diff_op
from tests.corpus.generator import generate
from tests.helpers import make_docx

_SYLLABLES = [c + v for c in "bcdfglmnprstvz" for v in "aeiou"]


def run_diff(request: dict[str, Any]) -> dict[str, Any]:
    reply = diff_op(request)
    assert isinstance(reply, Reply)
    return {**reply.result, "next": reply.next_cursor}


@dataclass(frozen=True)
class P:
    text: str

    @property
    def fp(self) -> str:
        return fingerprint(self.text)


def _vocabulary(rng: random.Random, size: int = 4000) -> list[str]:
    words: set[str] = set()
    while len(words) < size:
        words.add("".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 4))))
    return sorted(words)


def _sentences(rng: random.Random, vocab: list[str], count: int) -> list[str]:
    out = []
    for _ in range(count):
        words = [rng.choice(vocab) for _ in range(rng.randint(8, 14))]
        out.append(" ".join(words).capitalize() + ".")
    return out


# --- ground truth -------------------------------------------------------------------


@dataclass
class Entry:
    kind: str  # same, mod, split, merge, ins
    ids: tuple[int, ...]  # old paragraph positions it comes from
    text: str
    moved: bool = False


def _edited_pair(seed: int, chapters: int = 14) -> tuple[list[str], list[str], set[Any]]:
    """An old and a new version plus the expected set of (kind, old, new) changes.

    Ten content paragraphs per chapter, with repeated lines ("Capítulo", "***", "—Sí.")
    and short unique dialogue lines among them. Edits: modify, delete, insert, move a
    paragraph, move a block, modify and move, split, merge, and swap two chapters.
    Kinds of the swapped chapters are expected as "shifted" (moved or unchanged: which
    side of a swap is the "moved" one is arbitrary).
    """
    rng = random.Random(seed)
    vocab = _vocabulary(rng)
    count = chapters * 10
    sents = [_sentences(rng, vocab, rng.randint(4, 6)) for _ in range(count)]
    old: list[str] = []
    pos: dict[int, int] = {}  # content id -> position in the old version
    chapter_start: dict[int, int] = {}
    for c in range(count):
        if c % 10 == 0:
            chapter_start[c // 10] = len(old)
            old.append("Capítulo")
        pos[c] = len(old)
        old.append(" ".join(sents[c]))
        if c % 10 == 2:
            old.append(f"—¿Qué, {rng.choice(vocab)}?")
        elif c % 10 == 4:
            old.append("***")
        elif c % 10 == 7:
            old.append("—Sí.")
    entries = [Entry("same", (n,), text) for n, text in enumerate(old)]

    for c in (5, 35, 65, 80):  # modified: a few words replaced
        words = entries[pos[c]].text.split(" ")
        for at in (3, 17, 31):
            words[at] = rng.choice(vocab)
        entries[pos[c]].kind, entries[pos[c]].text = "mod", " ".join(words)
    deleted = {pos[15], pos[45]}

    out: list[Entry] = []
    for e in entries:
        n = e.ids[0]
        if n in deleted:
            continue
        splitting = [c for c in (100, 20) if pos[c] == n]
        merging = [c for c in (105, 30) if pos[c] == n]
        if splitting:  # split at a sentence boundary
            parts = sents[splitting[0]]
            cut = len(parts) // 2
            out.append(Entry("split", (n,), " ".join(parts[:cut])))
            out.append(Entry("split", (n,), " ".join(parts[cut:])))
        elif merging:  # merge with the next paragraph
            out.append(Entry("merge", (n, n + 1), old[n] + " " + old[n + 1]))
        elif any(pos[c] + 1 == n for c in (105, 30)):
            continue
        else:
            out.append(e)

    def pop(ids: list[int]) -> list[Entry]:
        found = [e for e in out if e.ids[0] in ids and e.kind in ("same", "mod")]
        for e in found:
            out.remove(e)
            e.moved = True
        return found

    def insert_after(anchor: int, items: list[Entry]) -> None:
        at = next(i for i, e in enumerate(out) if pos[anchor] in e.ids) + 1
        out[at:at] = items

    insert_after(10, pop([pos[75]]))
    insert_after(40, pop([pos[90], pos[91], pos[92]]))
    insert_after(50, pop([pos[80]]))  # modified and moved
    for anchor in (25, 55):
        insert_after(anchor, [Entry("ins", (), " ".join(_sentences(rng, vocab, 4)))])

    def chapter(x: int) -> list[Entry]:
        lo = chapter_start[x]
        hi = chapter_start.get(x + 1, len(old))
        return [e for e in out if e.ids and lo <= e.ids[0] < hi]

    first, last = chapter(11), chapter(13)  # swap chapters 11 and 13 around chapter 12
    start = out.index(first[0])
    end = out.index(last[-1]) + 1
    middle = out[start + len(first) : end - len(last)]
    out[start:end] = [*last, *middle, *first]
    swapped = {e.ids[0] for e in (*first, *middle, *last)}

    expected: set[Any] = set()
    placed: dict[tuple[int, ...], list[int]] = {}
    for j, e in enumerate(out):
        if e.kind == "ins":
            expected.add(("inserted", (), (j,)))
        else:
            placed.setdefault(e.ids, []).append(j)
    for e in out:
        if e.kind == "ins":
            continue
        js = tuple(placed[e.ids])
        if e.ids[0] in swapped:
            expected.add(("shifted", e.ids, js))
        elif e.kind == "split":
            expected.add(("split", e.ids, js))
        elif e.kind == "merge":
            expected.add(("merged", e.ids, js))
        elif e.kind == "mod":
            expected.add(("modified", e.ids, js))
        else:
            expected.add(("moved" if e.moved else "unchanged", e.ids, js))
    for n in deleted:
        expected.add(("deleted", (n,), ()))
    return old, [e.text for e in out], expected


def _write(path: Path, paragraphs: list[str]) -> Path:
    return make_docx(path, *paragraphs)


def _actual(alignment: Alignment, expected: set[Any]) -> set[Any]:
    shifted = {old for kind, old, _ in expected if kind == "shifted"}
    return {
        (
            "shifted" if c.old in shifted and c.kind in ("unchanged", "moved") else c.kind,
            c.old,
            c.new,
        )
        for c in alignment.changes
    }


def _alignment_errors(tmp_path: Path, seeds: range) -> int:
    errors = 0
    for seed in seeds:
        old, new, expected = _edited_pair(seed)
        _, _, alignment = diff_documents(
            _write(tmp_path / f"old{seed}.docx", old), _write(tmp_path / f"new{seed}.docx", new)
        )
        errors += len(expected ^ _actual(alignment, expected))
    return errors


def test_diff_detects_moved_and_split_paragraphs(tmp_path: Path) -> None:
    """Every edit kind at known places: aligned exactly, 0 errors over 20 document pairs."""
    diff_alignment_errors = _alignment_errors(tmp_path, range(20))
    print(f"diff_alignment_errors={diff_alignment_errors}")
    assert diff_alignment_errors == 0


def test_diff_ground_truth_covers_every_kind(tmp_path: Path) -> None:
    _, _, expected = _edited_pair(0)
    assert {kind for kind, _, _ in expected} == {
        "unchanged",
        "shifted",
        "modified",
        "moved",
        "split",
        "merged",
        "deleted",
        "inserted",
    }


def test_diff_address_map_follows_the_edits(tmp_path: Path) -> None:
    old, new, expected = _edited_pair(0)
    index_old, index_new, alignment = diff_documents(
        _write(tmp_path / "old.docx", old), _write(tmp_path / "new.docx", new)
    )
    mapping = address_map(alignment, index_old, index_new)
    assert len(mapping) == len(old)
    new_addresses = {str(p.address) for p in index_new.paragraphs}
    for source, target in mapping.items():
        assert target is None or target in new_addresses
        assert source.split("#")[0].startswith("p")
    # deleted paragraphs map to null; the first split part wins; merged parts share a target
    for kind, olds, news in expected:
        addresses = [str(index_old.paragraph(n + 1).address) for n in olds]
        if kind == "deleted":
            assert mapping[addresses[0]] is None
        elif kind == "split":
            assert mapping[addresses[0]] == str(index_new.paragraph(news[0] + 1).address)
        elif kind == "merged":
            assert {mapping[a] for a in addresses} == {
                str(index_new.paragraph(news[0] + 1).address)
            }
        elif kind == "unchanged":
            assert mapping[addresses[0]] == str(index_new.paragraph(news[0] + 1).address)


def test_diff_identical_documents_have_no_changes(tmp_path: Path) -> None:
    path = _write(tmp_path / "a.docx", ["one two three", "four five six"])
    result = run_diff({"old": str(path), "new": str(path)})
    assert result["changes"] == []
    assert result["address_map"] == {}
    assert result["summary"]["unchanged"] == 2
    assert result["next"] is None


def test_diff_lists_every_shifted_paragraph_in_the_address_map(tmp_path: Path) -> None:
    body = [
        f"paragraph number {w} with some words" for w in ("alpha", "beta", "gamma", "delta", "eps")
    ]
    old = _write(tmp_path / "old.docx", body)
    new = _write(tmp_path / "new.docx", ["a brand new first paragraph", *body])
    result = run_diff({"old": str(old), "new": str(new)})
    assert [c["kind"] for c in result["changes"]] == ["inserted"] + ["unchanged"] * 5
    assert len(result["address_map"]) == 5  # every paragraph whose number changed
    for source, target in result["address_map"].items():
        assert source.split("#")[1] == target.split("#")[1]
        assert int(target[1:].split("#")[0]) == int(source[1:].split("#")[0]) + 1
    collapsed = run_diff({"old": str(old), "new": str(new), "collapse_runs": True})
    assert [(c["kind"], c.get("count")) for c in collapsed["changes"]] == [
        ("inserted", None),
        ("unchanged", 5),
    ]
    assert collapsed["address_map"] == {}


def test_diff_split_record_with_many_parts_is_not_attempted() -> None:
    sentence = " ".join(f"word{n}" for n in range(40))
    parts = [P(" ".join(f"word{n}" for n in range(k, k + 4))) for k in range(0, 40, 4)]
    kinds = {c.kind for c in align([P(sentence)], parts).changes}
    assert "split" not in kinds  # ten parts: beyond MAX_GROUP_RUN


def test_diff_pages_with_cursor_and_rejects_foreign_cursor(tmp_path: Path) -> None:
    old, new, _ = _edited_pair(1)
    po, pn = _write(tmp_path / "o.docx", old), _write(tmp_path / "n.docx", new)

    def pages(limit: int | None) -> tuple[list[dict[str, Any]], int, str | None]:
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        count = 0
        first_cursor: str | None = None
        while True:
            req: dict[str, Any] = {"old": str(po), "new": str(pn)}
            if limit:
                req["limit"] = limit
            if cursor:
                req["cursor"] = cursor
            page = run_diff(req)
            collected += page["changes"]
            count += 1
            cursor = page["next"]
            first_cursor = first_cursor or cursor
            if cursor is None:
                return collected, count, first_cursor

    by_budget, budget_pages, _ = pages(None)
    by_five, five_pages, cursor = pages(5)
    assert by_budget == by_five and five_pages > budget_pages >= 1
    assert cursor is not None
    with pytest.raises(DocControlError) as err:  # a cursor only continues its own comparison
        run_diff({"old": str(pn), "new": str(po), "cursor": cursor, "limit": 5})
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    pn.write_bytes(_write(tmp_path / "n2.docx", [*new, "one more paragraph"]).read_bytes())
    with pytest.raises(DocControlError) as changed:  # the new file changed under the cursor
        run_diff({"old": str(po), "new": str(pn), "cursor": cursor, "limit": 5})
    assert changed.value.code is ErrorCode.SCHEMA_INVALID


def test_diff_is_read_only(tmp_path: Path) -> None:
    old, new, _ = _edited_pair(2)
    po, pn = _write(tmp_path / "o.docx", old), _write(tmp_path / "n.docx", new)
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (po, pn)]
    run_diff({"old": str(po), "new": str(pn)})
    after = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (po, pn)]
    assert before == after
    assert sorted(f.name for f in tmp_path.iterdir()) == ["n.docx", "o.docx"]


def test_diff_errors_for_missing_and_invalid_files(tmp_path: Path) -> None:
    good = _write(tmp_path / "g.docx", ["text"])
    with pytest.raises(DocControlError) as missing:
        run_diff({"old": str(tmp_path / "nope.docx"), "new": str(good)})
    assert missing.value.code is ErrorCode.DOC_NOT_FOUND
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(DocControlError) as invalid:
        run_diff({"old": str(good), "new": str(bad)})
    assert invalid.value.code is ErrorCode.NOT_A_DOCX


def test_diff_through_the_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = _write(
        tmp_path / "o.docx", ["alpha beta gamma delta epsilon zeta eta", "other text here"]
    )
    new = _write(tmp_path / "n.docx", ["alpha beta gamma delta epsilon zeta eta", "changed"])
    code = main(["diff", "--json", json.dumps({"old": str(old), "new": str(new)})])
    env = json.loads(capsys.readouterr().out)
    assert code == 0 and env["ok"] and env["op"] == "diff"
    assert env["result"]["summary"]["deleted"] == 1
    bad = main(["diff", "--json", json.dumps({"old": str(old)})])
    assert bad == 2
    capsys.readouterr()


def test_diff_on_generated_corpus_documents(tmp_path: Path) -> None:
    """Corpus documents: identical copies align fully, and an edited copy is found."""
    for kind in ("plain", "novel", "tables", "kitchen_sink"):
        path = tmp_path / f"{kind}.docx"
        path.write_bytes(generate(kind, 1))
        index = DocumentIndex.from_package(Package(path))
        same = align(index.paragraphs, index.paragraphs)
        assert same.counts()["unchanged"] == len(index)
        assert len(same.changes) == len(index)
        texts = [p.text for p in index.paragraphs]
        longest = max(range(len(texts)), key=lambda n: len(texts[n]))
        edited = [P(t) for t in texts]
        edited[longest] = P(texts[longest] + " Y añadido al final")
        result = align(index.paragraphs, edited)
        nonblank = [c for c in result.changes if c.kind != "unchanged"]
        if len(set(texts)) == len(texts):
            assert [(c.kind, c.old, c.new) for c in nonblank] == [
                ("modified", (longest,), (longest,))
            ]


# --- thresholds, edge cases ---------------------------------------------------------


def test_modified_threshold_boundary() -> None:
    base = " ".join(f"w{n}" for n in range(20))
    close = " ".join(f"w{n}" for n in range(20) if n % 5)  # 16 of 20 words kept: ratio 0.89
    far = " ".join(f"w{n}" if n < 8 else f"x{n}" for n in range(20))  # 8 of 20 kept: 0.4
    assert MODIFIED_MIN == 0.6
    kinds = [c.kind for c in align([P(base)], [P(close)]).changes]
    assert kinds == ["modified"]
    kinds = sorted(c.kind for c in align([P(base)], [P(far)]).changes)
    assert kinds == ["deleted", "inserted"]


def test_short_heading_edit_is_modified_in_place() -> None:
    old = [P("Capítulo uno"), P("x " * 30), P("Capítulo dos"), P("y " * 30)]
    new = [P("Capítulo 1"), P("x " * 30), P("Capítulo dos"), P("y " * 30)]
    changes = {c.kind: c for c in align(old, new).changes if c.kind != "unchanged"}
    assert set(changes) == {"modified"}
    assert changes["modified"].old == (0,) and changes["modified"].new == (0,)


def test_blank_paragraphs_never_move_or_split() -> None:
    old = [P(t) for t in ("alpha beta gamma delta", "", "", "one two three four", "")]
    new = [P(t) for t in ("alpha beta gamma delta", "", "one two three four", "", "")]
    result = align(old, new)
    assert {c.kind for c in result.changes} <= {"unchanged", "deleted", "inserted"}


def test_split_needs_similarity_above_threshold() -> None:
    long = " ".join(f"word{n}" for n in range(30))
    halves = [
        P(" ".join(f"word{n}" for n in range(15))),
        P(" ".join(f"other{n}" for n in range(15))),
    ]
    changes = align([P(long)], halves).changes
    assert "split" not in {c.kind for c in changes}
    assert GROUP_MIN_SIMILARITY == 0.8


def test_modified_and_moved_paragraph_is_flagged() -> None:
    body = [P(f"filler paragraph {n} " + "pad " * 10) for n in range(30)]
    target = P(" ".join(f"term{n}" for n in range(24)))
    edited = P(" ".join(f"term{n}" if n % 12 else "changed" for n in range(24)))
    old = [*body[:5], target, *body[5:]]
    new = [*body[:25], edited, *body[25:]]
    mods = [c for c in align(old, new).changes if c.kind == "modified"]
    assert len(mods) == 1 and mods[0].moved and mods[0].old == (5,) and mods[0].new == (25,)


def test_duplicate_paragraphs_align_in_order() -> None:
    old = [P("same line of text here"), P("middle part of the story"), P("same line of text here")]
    new = [P("same line of text here"), P("middle part of the story"), P("same line of text here")]
    assert {c.kind for c in align(old, new).changes} == {"unchanged"}


def test_empty_documents() -> None:
    assert align([], []).changes == ()
    only_new = align([], [P("something new here")]).changes
    assert [c.kind for c in only_new] == ["inserted"]


# --- scale --------------------------------------------------------------------------


def _novel(seed: int, words: int) -> list[str]:
    rng = random.Random(seed)
    vocab = _vocabulary(rng, 6000)
    out: list[str] = []
    total = 0
    while total < words:
        n = rng.randint(20, 90)
        out.append(" ".join(rng.choice(vocab) for _ in range(n)).capitalize() + ".")
        total += n
    return out


def test_diff_100k_word_novel_in_reasonable_time(tmp_path: Path) -> None:
    old = _novel(7, 100_000)
    rng = random.Random(8)
    new = list(old)
    for n in rng.sample(range(len(new)), 40):  # modify
        words = new[n].split(" ")
        words[len(words) // 2] = "cambiada"
        new[n] = " ".join(words)
    del new[300:310]
    new[500:500] = ["Un párrafo nuevo que no existía antes en la novela original."] * 1
    block = new[800:805]
    del new[800:805]
    new[1200:1200] = block
    po, pn = _write(tmp_path / "o.docx", old), _write(tmp_path / "n.docx", new)
    start = time.perf_counter()
    result = run_diff({"old": str(po), "new": str(pn), "limit": 1000})
    seconds = time.perf_counter() - start
    total_words = sum(len(p.split()) for p in old)
    print(f"diff_100k_words: {total_words} words, {len(old)} paragraphs, {seconds:.2f}s")
    summary = result["summary"]
    assert summary["deleted"] == 10 and summary["inserted"] == 1
    assert summary["moved"] == 5 and summary["modified"] == 40
    assert seconds < 20


def test_diff_unrelated_100k_word_documents_stay_fast(tmp_path: Path) -> None:
    a, b = (
        _write(tmp_path / "a.docx", _novel(1, 100_000)),
        _write(tmp_path / "b.docx", _novel(2, 100_000)),
    )
    start = time.perf_counter()
    _, _, alignment = diff_documents(a, b)
    seconds = time.perf_counter() - start
    print(f"diff_unrelated_100k_words: {seconds:.2f}s")
    counts = alignment.counts()
    assert counts["unchanged"] == 0 and counts["modified"] == 0
    assert seconds < 30


def test_old_new_hashes_reported(tmp_path: Path) -> None:
    path = _write(tmp_path / "a.docx", ["some text"])
    result = run_diff({"old": str(path), "new": str(path)})
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["old"]["sha256"] == digest and result["new"]["paragraphs"] == 1


# --- repeated lines, chapter swaps, adversarial runs --------------------------------


def _chapters(rng: random.Random, vocab: list[str], count: int) -> list[list[str]]:
    """Chapters of ["Capítulo", 5 sentences, "***", 4 sentences, "—Sí."]."""
    return [
        [
            "Capítulo",
            " ".join(_sentences(rng, vocab, 5)),
            "***",
            " ".join(_sentences(rng, vocab, 4)),
            "—Sí.",
        ]
        for _ in range(count)
    ]


def test_swapped_chapters_carry_their_repeated_lines() -> None:
    rng = random.Random(3)
    chapters = _chapters(rng, _vocabulary(rng), 3)
    old = [P(t) for c in chapters for t in c]
    new = [P(t) for c in (chapters[0], chapters[2], chapters[1]) for t in c]
    result = align(old, new)
    where = {0: 0, 1: 2, 2: 1}  # old chapter -> its position in the new version
    expected = {(c * 5 + k,): (where[c] * 5 + k,) for c in range(3) for k in range(5)}
    assert {c.old: c.new for c in result.changes} == expected
    assert result.counts()["deleted"] == result.counts()["inserted"] == 0


def test_run_of_candidates_is_capped_and_fast() -> None:
    """Many similar paragraphs on both sides: no near-cubic trimming of one long run."""
    rng = random.Random(5)
    vocab = [f"w{n}" for n in range(60)]
    old = [P(" ".join(rng.choice(vocab) for _ in range(100))) for _ in range(100)]
    new = [P(" ".join(rng.choice(vocab) for _ in range(5))) for _ in range(500)]
    start = time.perf_counter()
    align(old, new)
    seconds = time.perf_counter() - start
    print(f"adversarial_align_seconds={seconds:.2f}")
    assert seconds < 3


def test_long_candidate_runs_with_enough_words_stay_fast() -> None:
    rng = random.Random(6)
    vocab = [f"w{n}" for n in range(60)]
    old = [P(" ".join(rng.choice(vocab) for _ in range(100))) for _ in range(100)]
    new = [P(" ".join(rng.choice(vocab) for _ in range(20))) for _ in range(500)]
    start = time.perf_counter()
    align(old, new)
    seconds = time.perf_counter() - start
    print(f"adversarial_long_runs_seconds={seconds:.2f}")
    assert seconds < 3
