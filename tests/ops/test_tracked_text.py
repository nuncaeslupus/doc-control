"""Tracked text edits over generated documents: reject-all restores, accept-all is the new text.

The gate ``text_after_reject_all_mismatches == 0``: after random batches of
``text.replace``, ``para.insert``, ``para.delete`` and ``para.move`` on the
synthetic corpus, rejecting every revision the call wrote gives back exactly the
text the document had before, and accepting them gives the intended new text.
The accept/reject model (``tests/ops/revsim.py``) is independent of the code
under test. Batches run on top of one another, so later edits meet the earlier
ones' revisions as someone else's.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import Paragraph, build_index
from doc_control.ops.tracked import (
    DeleteParagraphs,
    Edit,
    InsertParagraph,
    MoveParagraphs,
    ReplaceText,
    apply_edits,
)
from tests.conftest import CorpusWriter
from tests.corpus.generator import KINDS
from tests.corpus.private import ENV_VAR, corpus_dir, private_documents
from tests.corpus.structural import validate_structure
from tests.corpus.validator import require_soffice, soffice_path, validate_documents
from tests.ooxml.docgen import generate as generate_document_xml
from tests.ops import revsim

KINDS_NO_NOVEL = tuple(k for k in KINDS if k != "novel")
SEEDS = range(8)
ROUNDS = 3
TEXTS = (
    "",
    "X",
    "nuevo texto",
    " con espacio delante",
    "y detrás ",
    "tab\taquí",
    "línea\nnueva",
    "ñandú — «comillas» “así”",
    "emoji \U0001f600 fin",
    "<&> \"'",
)
REFUSALS = {
    ErrorCode.UNSUPPORTED_CONTENT,
    ErrorCode.BATCH_CONFLICT,
    ErrorCode.ADDRESS_INVALID,
    ErrorCode.SCHEMA_INVALID,
}


@dataclass
class Spec:
    """One random edit in terms of 0-based paragraph positions, for the model."""

    kind: str  # replace | cross | insert | delete | move
    i: int
    j: int = -1
    s: int = 0
    e: int = 0
    text: str = ""
    where: str = "after"
    target: int = -1
    whole: bool = False


def random_specs(rng: random.Random, texts: list[str]) -> list[Spec]:
    n = len(texts)
    filled = [k for k, t in enumerate(texts) if t]
    specs: list[Spec] = []
    used: set[int] = set()

    def free(candidates: list[int], width: int = 1) -> int | None:
        """A position whose next ``width`` paragraphs are unused (mostly)."""
        rng.shuffle(candidates)
        for c in candidates:
            if not used.intersection(range(c, c + width)) and c + width <= n:
                return c
        return None

    for _ in range(rng.randint(1, 4)):
        kind = rng.choices(
            ["replace", "insert-text", "whole", "cross", "insert", "delete", "move"],
            [34, 10, 5, 10, 12, 10, 10],
        )[0]
        if not filled:
            break
        if kind in ("replace", "insert-text", "whole"):
            # now and then reuse a paragraph, to meet several replacements in one
            i = free(list(filled)) if rng.random() < 0.85 else rng.choice(filled)
            if i is None:
                continue
            used.add(i)
            size = len(texts[i])
            if kind == "whole":
                specs.append(Spec("replace", i, 0, 0, size, rng.choice(TEXTS), whole=True))
                continue
            s = rng.randint(0, size)
            e = s if kind == "insert-text" else rng.randint(s, min(size, s + rng.randint(1, 30)))
            text = rng.choice(TEXTS[1:] if s == e else TEXTS)
            specs.append(Spec("replace", i, i, s, e, text))
        elif kind == "cross" and n > 2:
            i = free(list(range(n - 1)), 2)
            if i is None:
                continue
            j = min(n - 1, i + rng.randint(1, 3))
            if used.intersection(range(i, j + 1)):
                j = i + 1
            used.update(range(i, j + 1))
            s, e = rng.randint(0, len(texts[i])), rng.randint(0, len(texts[j]))
            specs.append(Spec("cross", i, j, s, e, rng.choice(TEXTS)))
        elif kind == "insert":
            i = rng.randrange(n)
            where = rng.choice(["before", "after"])
            specs.append(Spec("insert", i, text=rng.choice(TEXTS), where=where))
        elif kind == "delete":
            i = free(list(range(n)))
            if i is None:
                continue
            j = (
                i
                if used.intersection(range(i, min(n, i + 2)))
                else min(n - 1, i + rng.randint(0, 1))
            )
            used.update(range(i, j + 1))
            specs.append(Spec("delete", i, j))
        elif kind == "move" and n > 4:
            i = free(list(range(n)))
            if i is None:
                continue
            j = (
                i
                if used.intersection(range(i, min(n, i + 2)))
                else min(n - 1, i + rng.randint(0, 1))
            )
            used.update(range(i, j + 1))
            where = rng.choice(["before", "after"])
            outside = [t for t in range(n) if not i - 1 <= t <= j + 1]
            if not outside:
                continue
            specs.append(Spec("move", i, j, where=where, target=rng.choice(outside)))
    return specs


def to_edit(spec: Spec, paras: tuple[Paragraph, ...]) -> Edit:
    a = paras[spec.i].address
    b = paras[spec.j].address if spec.j >= 0 else a
    if spec.kind == "replace":
        return ReplaceText(str(a) if spec.whole else f"{a}:{spec.s}-{spec.e}", spec.text)
    if spec.kind == "cross":
        return ReplaceText(f"{a}:{spec.s}..{b}:{spec.e}", spec.text)
    if spec.kind == "insert":
        anchor = str(a)
        return (
            InsertParagraph(spec.text, after=anchor)
            if spec.where == "after"
            else InsertParagraph(spec.text, before=anchor)
        )
    run = str(a) if spec.i == spec.j else f"{a}..{b}"
    if spec.kind == "delete":
        return DeleteParagraphs(run)
    target = str(paras[spec.target].address)
    if spec.where == "after":
        return MoveParagraphs(run, after=target)
    return MoveParagraphs(run, before=target)


def model(texts: list[str], specs: list[Spec]) -> tuple[list[str], list[bool]]:
    """The paragraphs and the removed-mark flags after accepting the specs' revisions."""
    new_text: dict[int, str] = {}
    joins: dict[int, bool] = {}
    before: dict[int, list[str]] = defaultdict(list)
    after: dict[int, list[str]] = defaultdict(list)
    replaces: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for spec in specs:
        if spec.kind == "replace":
            replaces[spec.i].append((spec.s, spec.e, spec.text))
        elif spec.kind == "cross":
            replaces[spec.i].append((spec.s, len(texts[spec.i]), spec.text))
            replaces[spec.j].append((0, spec.e, ""))
            for k in range(spec.i, spec.j):
                joins[k] = True
            for k in range(spec.i + 1, spec.j):
                new_text[k] = ""
        elif spec.kind == "insert":
            (before if spec.where == "before" else after)[spec.i].append(spec.text)
        else:
            for k in range(spec.i, spec.j + 1):
                new_text[k] = ""
                joins[k] = True
            if spec.kind == "move":
                bucket = before if spec.where == "before" else after
                bucket[spec.target].extend(texts[spec.i : spec.j + 1])
    out: list[str] = []
    flags: list[bool] = []
    for k, text in enumerate(texts):
        out += before[k]
        flags += [False] * len(before[k])
        if k in new_text:
            text = new_text[k]
        for s, e, new in sorted(replaces[k], reverse=True):
            text = text[:s] + new + text[e:]
        out.append(text)
        flags.append(joins.get(k, False))
        out += after[k]
        flags += [False] * len(after[k])
    return out, flags


@dataclass
class Stats:
    batches: int = 0
    applied: int = 0
    refused: int = 0
    edits: int = 0
    reject_mismatches: int = 0
    accept_mismatches: int = 0
    accept_skipped: int = 0
    index_mismatches: int = 0
    id_problems: int = 0
    lint_problems: int = 0
    other_changes: int = 0
    refusal_codes: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int))


def run_batch(path: Path, rng: random.Random, author: str, stats: Stats) -> bool:
    """One random batch on ``path``; returns True if it was applied."""
    before_bytes = path.read_bytes()
    old_parts = revsim.package_parts(before_bytes)
    old_xml = old_parts["word/document.xml"]
    index = build_index(old_xml)
    visible = [p.text for p in index.paragraphs]
    specs = random_specs(rng, visible)
    if not specs:
        return False
    edits = [to_edit(s, index.paragraphs) for s in specs]
    stats.batches += 1
    try:
        apply_edits(path, edits, author=author, tag="gate", env={})
    except DocControlError as err:
        assert err.code in REFUSALS, f"{err.code}: {err.message} for {edits}"
        assert path.read_bytes() == before_bytes, "a refused batch changed the file"
        stats.refused += 1
        stats.refusal_codes[err.code.value] += 1
        return False
    stats.applied += 1
    stats.edits += len(edits)
    tool = f"{author} (gate)"
    new_bytes = path.read_bytes()
    new_parts = revsim.package_parts(new_bytes)
    new_xml = new_parts["word/document.xml"]

    # Gate: rejecting this call's revisions restores the text exactly.
    if revsim.resolved(new_xml, tool, "reject") != revsim.resolved(old_xml, None, "current"):
        stats.reject_mismatches += 1

    # Accepting them gives the intended text (where the model's text matches the index's).
    raw, _ = revsim.raw_texts(old_xml, None, "current")
    touched = {k for s in specs for k in range(s.i, max(s.i, s.j) + 1)}
    touched |= {s.target for s in specs if s.kind == "move"}
    already_deleted = revsim.deleted_marks(old_xml)
    marked = {k for s in specs if s.kind in ("delete", "move", "cross") for k in range(s.i, s.j)}
    marked |= {s.j for s in specs if s.kind in ("delete", "move")}
    if any(already_deleted[k] for k in marked):
        stats.accept_skipped += 1  # a mark someone else deleted stays theirs
    elif all(raw[k] == visible[k] for k in touched if k < len(raw)):
        exp_texts, exp_joins = model(raw, specs)
        if revsim.resolved(new_xml, tool, "accept") != revsim.merge(exp_texts, exp_joins):
            stats.accept_mismatches += 1
        if [p.text for p in build_index(new_xml).paragraphs] != model(visible, specs)[0]:
            stats.index_mismatches += 1
    else:
        stats.accept_skipped += 1

    # Ids: the call's own revisions are unique and clash with nothing that was there.
    ids = revsim.tool_ids(new_xml, tool)
    if len(ids) != len(set(ids)) or set(ids) & revsim.all_ids(old_parts):
        stats.id_problems += 1
    if validate_structure(new_bytes):
        stats.lint_problems += 1
    # Nothing but word/document.xml changed in the package.
    for name, data in old_parts.items():
        if name != "word/document.xml" and new_parts.get(name) != data:
            stats.other_changes += 1
    return True


def run_corpus(tmp_path: Path, write: CorpusWriter, kinds: tuple[str, ...], seeds: range) -> Stats:
    stats = Stats()
    for kind in kinds:
        for seed in seeds:
            path = write(kind, seed)
            rng = random.Random(f"{kind}:{seed}")
            for round_no in range(ROUNDS):
                run_batch(path, rng, f"Tester{round_no}", stats)
    return stats


def test_reject_all_after_edits_restores_original_text(
    tmp_path: Path, corpus_doc: CorpusWriter
) -> None:
    stats = run_corpus(tmp_path, corpus_doc, KINDS_NO_NOVEL, SEEDS)
    print(
        f"text_after_reject_all_mismatches={stats.reject_mismatches} "
        f"batches={stats.batches} applied={stats.applied} edits={stats.edits} "
        f"refused={stats.refused} {dict(stats.refusal_codes)} "
        f"accept_checked={stats.applied - stats.accept_skipped}"
    )
    assert stats.applied > 100 and stats.edits > 250  # the run is not vacuous
    assert stats.reject_mismatches == 0
    assert stats.accept_mismatches == 0
    assert stats.index_mismatches == 0
    assert stats.id_problems == 0
    assert stats.lint_problems == 0
    assert stats.other_changes == 0
    assert stats.applied - stats.accept_skipped > 80


def test_novel_edits_restore_original_text(tmp_path: Path, corpus_doc: CorpusWriter) -> None:
    stats = Stats()
    path = corpus_doc("novel", 0)
    rng = random.Random("novel")
    for round_no in range(2):
        run_batch(path, rng, f"Novelist{round_no}", stats)
    print(
        f"novel text_after_reject_all_mismatches={stats.reject_mismatches} applied={stats.applied}"
    )
    assert stats.applied >= 1
    assert stats.reject_mismatches == stats.accept_mismatches == stats.id_problems == 0
    assert stats.lint_problems == stats.other_changes == 0


def _wrap_document(xml: bytes, path: Path) -> Path:
    """Put a generated ``word/document.xml`` (odd prefixes and all) into a package."""
    import zipfile

    from tests.helpers import CONTENT_TYPES, RELS

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", xml)
    return path


def test_edits_survive_odd_namespace_prefixes(tmp_path: Path) -> None:
    """Generated documents with default-namespace and re-declared prefixes edit cleanly."""
    stats = Stats()
    for seed in range(120):
        path = _wrap_document(generate_document_xml(seed), tmp_path / f"odd-{seed}.docx")
        rng = random.Random(f"odd:{seed}")
        for round_no in range(2):
            try:
                run_batch(path, rng, f"Odd{round_no}", stats)
            except DocControlError as err:  # a document the package reader refuses
                assert err.code in (ErrorCode.NOT_A_DOCX, ErrorCode.UNSUPPORTED_CONTENT)
                break
    print(
        f"odd-prefix text_after_reject_all_mismatches={stats.reject_mismatches} "
        f"applied={stats.applied} refused={stats.refused}"
    )
    assert stats.applied > 20
    assert stats.reject_mismatches == stats.id_problems == stats.other_changes == 0


@pytest.mark.skipif(soffice_path() is None, reason="LibreOffice (soffice) not found")
def test_libreoffice_opens_edited_documents(tmp_path: Path, corpus_doc: CorpusWriter) -> None:
    require_soffice()
    docs: dict[str, bytes] = {}
    stats = Stats()
    for kind in KINDS_NO_NOVEL:
        for seed in range(2):
            path = corpus_doc(kind, seed)
            rng = random.Random(f"lo:{kind}:{seed}")
            for round_no in range(2):
                run_batch(path, rng, f"Office{round_no}", stats)
            docs[f"{kind}-{seed}"] = path.read_bytes()
    assert stats.applied >= 10
    results = validate_documents(docs, tmp_path)
    assert {name: problems for name, problems in results.items() if problems} == {}


def test_private_corpus_edits_restore_original_text(tmp_path: Path) -> None:
    """Random edits on real documents; reports counts only, never names or text."""
    docs = list(private_documents())
    if not docs:
        pytest.skip(f"{ENV_VAR} not set or holds no .docx files")
    assert corpus_dir() is not None
    stats = Stats()
    for doc_id, data in docs:
        path = tmp_path / "private.docx"
        path.write_bytes(data)
        rng = random.Random(doc_id)
        for round_no in range(2):
            run_batch(path, rng, f"Private{round_no}", stats)
    print(
        f"private text_after_reject_all_mismatches={stats.reject_mismatches} "
        f"documents={len(docs)} applied={stats.applied} refused={stats.refused}"
    )
    assert stats.reject_mismatches == 0
    assert stats.accept_mismatches == stats.id_problems == stats.other_changes == 0
