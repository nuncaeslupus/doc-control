"""`revision.accept` and `revision.reject`: accept-all gives the final text, reject-all the old.

The gate ``revision_accept_reject_mismatches == 0``. The expected text comes from
``tests/ops/revsim.py``, which shares no code with the tool. It is checked over the corpus
(revisions already in the documents), over documents carrying the tool's own tracked edits
(author by author, then all) and over a fuzz of random revision markup: nested insertions
and deletions, moves, deleted and inserted marks, format changes.
"""

from __future__ import annotations

import random
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.annotations import read_revisions
from doc_control.model.document import Document
from doc_control.model.index import build_index
from doc_control.ooxml import resolve
from doc_control.ops.batch import batch
from doc_control.ops.revision import ResolveRevisions, apply_resolutions
from doc_control.ops.tracked import apply_edits
from tests.conftest import CorpusWriter
from tests.corpus.generator import KINDS
from tests.corpus.structural import validate_structure
from tests.ops import revsim
from tests.ops.test_tracked_text import random_specs, to_edit

ANY = revsim.ANY
W = revsim.W
KINDS_NO_NOVEL = tuple(k for k in KINDS if k != "novel")
AUTHORS = ("Ana", "Ben", "Claude")
A = 'w:id="{}" w:author="Ana" w:date="2026-01-01T00:00:00Z"'


def resolve_doc(path: Path, action: str, **sel: Any) -> dict[str, Any]:
    return apply_resolutions(path, [ResolveRevisions(action, **sel)], env={}).to_dict()


def xml_of(path: Path) -> bytes:
    return revsim.package_parts(path.read_bytes())["word/document.xml"]


def texts(path: Path) -> list[str]:
    return [p.text for p in build_index(xml_of(path)).paragraphs]


def pending(path: Path) -> list[str]:
    return [r.id for r in read_revisions(Document(str(path)))]


def check(path: Path, tmp: Path, author: str | None, bad: list[str], label: str) -> None:
    """Resolve a copy both ways and compare with the oracle."""
    data = xml_of(path)
    for action in ("accept", "reject"):
        copy = tmp / f"{action}.docx"
        copy.write_bytes(path.read_bytes())
        sel = {"all": True} if author is None else {"author": author}
        resolve_doc(copy, action, **sel)
        expected = revsim.resolved(data, ANY if author is None else author, action)
        if texts(copy) != expected:
            bad.append(f"{label} {action}")
        if validate_structure(copy.read_bytes()):
            bad.append(f"{label} {action} lint")
        if author is None:
            left = [
                r
                for r in read_revisions(Document(str(copy)))
                if not r.type.startswith(("row", "cell"))
            ]
            if left:
                bad.append(f"{label} {action} left {len(left)}")


def fuzz_body(rng: random.Random) -> str:
    rid = iter(range(1, 10_000))
    n = [0]

    def at() -> str:
        return f'w:id="{next(rid)}" w:author="{rng.choice(AUTHORS)}" w:date="2026-01-01T00:00:00Z"'

    def run(text: str, tag: str = "w:t") -> str:
        rpr = "<w:rPr><w:b/></w:rPr>" if rng.random() < 0.2 else ""
        return f'<w:r>{rpr}<{tag} xml:space="preserve">{text}</{tag}></w:r>'

    def word() -> str:
        n[0] += 1
        return f"w{n[0]} "

    def piece() -> str:
        k = rng.random()
        if k < 0.3:
            return run(word())
        if k < 0.45:
            return f"<w:del {at()}>{run(word(), 'w:delText')}</w:del>"
        if k < 0.6:
            return f"<w:ins {at()}>{run(word())}{run(word())}</w:ins>"
        if k < 0.7:
            gone = run(word(), "w:delText")
            return f"<w:ins {at()}>{run(word())}<w:del {at()}>{gone}</w:del></w:ins>"
        if k < 0.78:
            return f"<w:moveFrom {at()}>{run(word(), 'w:delText')}</w:moveFrom>"
        if k < 0.86:
            return f"<w:moveTo {at()}>{run(word())}</w:moveTo>"
        if k < 0.93:
            return (
                f"<w:r><w:rPr><w:i/><w:rPrChange {at()}><w:rPr/></w:rPrChange></w:rPr>"
                f"<w:t>{word()}</w:t></w:r>"
            )
        return '<w:bookmarkStart w:id="900" w:name="b"/><w:bookmarkEnd w:id="900"/>' + run(word())

    paras = []
    for _ in range(rng.randint(3, 14)):
        r = rng.random()
        kind = "ins" if r < 0.15 else "del" if r < 0.3 else "moveFrom" if r < 0.35 else None
        if kind is None and r < 0.4:
            kind = "moveTo"
        ppr = f"<w:rPr><w:{kind} {at()}/></w:rPr>" if kind else ""
        if rng.random() < 0.2:
            old = f'<w:pPrChange {at()}><w:pPr><w:jc w:val="left"/></w:pPr></w:pPrChange>'
            ppr = f'<w:jc w:val="center"/>{ppr}{old}'
        ppr = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
        body = "".join(piece() for _ in range(rng.randint(0, 4)))
        paras.append(f"<w:p>{ppr}{body}</w:p>")
    return "".join(paras)


def test_accept_then_text_equals_expected_final_text(
    tmp_path: Path, corpus_doc: CorpusWriter
) -> None:
    bad: list[str] = []
    cases = 0
    for kind in KINDS_NO_NOVEL:  # revisions already in the documents
        for seed in range(6):
            check(corpus_doc(kind, seed), tmp_path, None, bad, f"corpus {kind}-{seed}")
            cases += 1
    for kind in KINDS_NO_NOVEL:  # the tool's own edits, per author and then all
        for seed in range(4):
            path = corpus_doc(kind, seed)
            rng = random.Random(f"{kind}:{seed}")
            authors = []
            for round_no in range(3):
                idx = build_index(xml_of(path))
                specs = random_specs(rng, [p.text for p in idx.paragraphs])
                if not specs:
                    continue
                try:
                    edits = [to_edit(s, idx.paragraphs) for s in specs]
                    apply_edits(path, edits, author=f"Tool{round_no}", env={})
                except DocControlError:
                    continue
                authors.append(f"Tool{round_no}")
            for name in authors:
                check(path, tmp_path, name, bad, f"tool {kind}-{seed} {name}")
                cases += 1
            check(path, tmp_path, None, bad, f"tool {kind}-{seed} all")
            cases += 1
    for seed in range(300):  # fuzz
        path = revsim.make_doc(tmp_path / f"fuzz{seed}.docx", fuzz_body(random.Random(seed)))
        check(path, tmp_path, None, bad, f"fuzz {seed}")
        check(path, tmp_path, AUTHORS[seed % 3], bad, f"fuzz-author {seed}")
        cases += 2
    print(f"revision_accept_reject_mismatches={len(bad)} cases={cases}")
    assert cases > 400
    assert bad == []


def test_selection_by_id_author_and_address(tmp_path: Path) -> None:
    ben = 'w:id="2" w:author="Ben" w:date="2026-01-01T00:00:00Z"'
    body = (
        f"<w:p><w:ins {A.format(1)}><w:r><w:t>one </w:t></w:r></w:ins></w:p>"
        f"<w:p><w:ins {ben}><w:r><w:t>two</w:t></w:r></w:ins></w:p>"
        f"<w:p><w:del {A.format(3)}><w:r><w:delText>three</w:delText></w:r></w:del></w:p>"
    )
    path = revsim.make_doc(tmp_path / "d.docx", body)
    out = resolve_doc(path, "reject", ids=["2"])
    assert out["rejected"] == 1 and texts(path) == ["one ", "", ""]
    out = resolve_doc(path, "accept", author="Ana")
    assert out["accepted"] == 2 and pending(path) == []
    path = revsim.make_doc(tmp_path / "e.docx", body)
    addr = [str(p.address) for p in build_index(xml_of(path)).paragraphs]
    out = resolve_doc(path, "accept", at=f"{addr[1]}..{addr[2]}")
    assert out["accepted"] == 2 and pending(path) == ["1"]
    with pytest.raises(DocControlError) as err:
        resolve_doc(path, "accept", ids=["99"])
    assert err.value.code is ErrorCode.STALE_ADDRESS


def test_deleted_mark_merges_and_move_pair(tmp_path: Path) -> None:
    body = (
        f"<w:p><w:pPr><w:rPr><w:del {A.format(1)}/></w:rPr></w:pPr>"
        "<w:r><w:t>head </w:t></w:r></w:p>"
        '<w:p><w:pPr><w:jc w:val="right"/></w:pPr><w:r><w:t>tail</w:t></w:r></w:p>'
        f"<w:p><w:moveFrom {A.format(2)}><w:r><w:delText>moved</w:delText></w:r></w:moveFrom></w:p>"
        f"<w:p><w:moveTo {A.format(3)}><w:r><w:t>moved</w:t></w:r></w:moveTo></w:p>"
    )
    path = revsim.make_doc(tmp_path / "d.docx", body)
    out = resolve_doc(path, "accept", all=True)
    assert out["merged_paragraphs"] == 1
    assert texts(path) == ["head tail", "", "moved"]
    assert b'w:jc w:val="right"' in xml_of(path)
    path = revsim.make_doc(tmp_path / "e.docx", body)
    resolve_doc(path, "reject", all=True)
    assert texts(path) == ["head ", "tail", "moved", ""]


def test_format_changes(tmp_path: Path) -> None:
    body = (
        f'<w:p><w:pPr><w:jc w:val="center"/><w:pPrChange {A.format(1)}><w:pPr><w:jc w:val="left"/>'
        "</w:pPr></w:pPrChange></w:pPr>"
        f"<w:r><w:rPr><w:b/><w:rPrChange {A.format(2)}><w:rPr><w:i/></w:rPr></w:rPrChange></w:rPr>"
        "<w:t>x</w:t></w:r></w:p>"
    )
    path = revsim.make_doc(tmp_path / "d.docx", body)
    resolve_doc(path, "reject", all=True)
    root = ET.fromstring(xml_of(path))
    jc = root.find(f".//{W}jc")
    assert jc is not None and jc.get(W + "val") == "left"
    assert root.find(f".//{W}r/{W}rPr/{W}i") is not None and root.find(f".//{W}b") is None
    assert b"Change" not in xml_of(path)
    path = revsim.make_doc(tmp_path / "e.docx", body)
    resolve_doc(path, "accept", all=True)
    assert b"Change" not in xml_of(path) and b'w:val="center"' in xml_of(path)


def test_nesting_and_comment_anchors_survive(tmp_path: Path) -> None:
    body = (
        f"<w:p><w:ins {A.format(1)}><w:r><w:t>a</w:t></w:r><w:del {A.format(2)}>"
        "<w:r><w:delText>b</w:delText></w:r></w:del></w:ins>"
        f'<w:del {A.format(3)}><w:commentRangeStart w:id="7"/><w:r><w:delText>c</w:delText></w:r>'
        '<w:commentRangeEnd w:id="7"/><w:r><w:commentReference w:id="7"/></w:r></w:del></w:p>'
    )
    path = revsim.make_doc(tmp_path / "d.docx", body)
    resolve_doc(path, "accept", ids=["1"])  # the nested deletion stays pending
    assert pending(path) == ["2", "3"]
    resolve_doc(path, "accept", all=True)
    data = xml_of(path)
    assert texts(path) == ["a"] and b"commentRangeStart" in data and b"commentReference" in data
    path = revsim.make_doc(tmp_path / "e.docx", body)
    resolve_doc(path, "reject", ids=["1"])
    assert texts(path) == [""] and pending(path) == ["3"]


def test_conflict_dry_run_and_nothing_to_do(tmp_path: Path) -> None:
    path = revsim.make_doc(
        tmp_path / "d.docx", f"<w:p><w:ins {A.format(1)}><w:r><w:t>a</w:t></w:r></w:ins></w:p>"
    )
    before = path.read_bytes()
    both = [ResolveRevisions("accept", all=True), ResolveRevisions("reject", ids=("1",))]
    with pytest.raises(DocControlError) as err:
        apply_resolutions(path, both)
    assert err.value.code is ErrorCode.BATCH_CONFLICT
    out = apply_resolutions(path, [ResolveRevisions("accept", all=True)], dry_run=True)
    assert out.accepted == 1 and path.read_bytes() == before
    out = apply_resolutions(path, [ResolveRevisions("accept", author="Nobody")], env={})
    assert out.accepted == 0 and path.read_bytes() == before


def test_batch_kinds_and_mixing_refused(tmp_path: Path) -> None:
    assert resolve.scan(ET.fromstring(f'<w:p xmlns:w="{revsim.W_NS}"/>')) == []
    path = revsim.make_doc(
        tmp_path / "d.docx", f"<w:p><w:ins {A.format(1)}><w:r><w:t>a</w:t></w:r></w:ins></w:p>"
    )
    mixed = [
        {"op": "revision.accept", "all": True},
        {"op": "text.replace", "at": "p1#00000000", "text": "x"},
    ]
    with pytest.raises(DocControlError) as err:
        batch({"doc": str(path), "ops": mixed})
    assert err.value.code is ErrorCode.BATCH_CONFLICT
    with pytest.raises(DocControlError) as err:
        batch({"doc": str(path), "ops": [{"op": "revision.reject"}]})
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    out = batch({"doc": str(path), "ops": [{"op": "revision.accept", "all": True}]})
    assert isinstance(out, object) and pending(path) == []
