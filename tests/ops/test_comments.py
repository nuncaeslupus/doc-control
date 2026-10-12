"""``comment.add``, ``comment.reply`` and ``comment.resolve`` as batch operations.

The gate ``ooxml_validator_errors_introduced == 0``: adding, replying to and resolving
comments on every corpus document gives the structural lint and the LibreOffice round trip
no problem that the untouched document did not already have.
"""

from __future__ import annotations

import contextlib
import re
import zipfile
from collections import Counter
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.annotations import Comment, read_comments
from doc_control.model.document import Document
from doc_control.ops.batch import batch
from doc_control.registry import load_operations
from tests.corpus.generator import KINDS
from tests.corpus.private import ENV_VAR, private_documents
from tests.corpus.validator import require_soffice, validate_documents
from tests.helpers import make_docx

CorpusWriter = Callable[..., Path]  # corpus_doc(kind, seed=0)
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
PART_NAMES = (
    "word/comments.xml",
    "word/commentsExtended.xml",
    "word/commentsIds.xml",
    "word/commentsExtensible.xml",
    "word/people.xml",
)


def run(path: Path, ops: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    reply = batch({"doc": str(path), "ops": ops, **extra})
    return reply.result  # type: ignore[union-attr]


def comments(path: Path) -> list[Comment]:
    return read_comments(Document(str(path)))


def addresses(path: Path) -> list[str]:
    return [str(p.address) for p in Document(str(path)).index.paragraphs]


def members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def plain_doc(tmp_path: Path) -> Path:
    return make_docx(
        tmp_path / "plain.docx", "Primer párrafo del texto.", "Segundo párrafo.", "Tercero."
    )


# -- the gate -------------------------------------------------------------------------


def _roundtrip(path: Path) -> dict[str, int]:
    """Add, reply to and resolve comments on ``path``; returns counts."""
    counts = {"added": 0, "replied": 0, "resolved": 0, "refused": 0}
    paras = Document(str(path)).index.paragraphs
    texty = [p for p in paras if len(p.text) >= 12]
    if not texty:
        return counts
    picks = [texty[0], texty[len(texty) // 2]]
    for n, para in enumerate(picks):
        at = str(para.address) if n == 0 else f"{para.address}:2-9"
        before = {c.id for c in comments(path)}
        try:
            run(path, [{"op": "comment.add", "at": at, "text": f"Nota {n}\nsegunda línea"}])
        except DocControlError as err:
            if err.code is not ErrorCode.UNSUPPORTED_CONTENT:
                raise
            counts["refused"] += 1
            continue
        counts["added"] += 1
        new = [c for c in comments(path) if c.id not in before]
        assert len(new) == 1 and new[0].text == f"Nota {n}\nsegunda línea"
        run(path, [{"op": "comment.reply", "comment": new[0].id, "text": "Respuesta"}])
        counts["replied"] += 1
        run(path, [{"op": "comment.resolve", "comment": new[0].id}])
        counts["resolved"] += 1
        thread = [c for c in comments(path) if c.id == new[0].id or c.reply_to == new[0].id]
        assert len(thread) == 2 and all(c.resolved for c in thread)
    existing = [c for c in comments(path) if c.reply_to is None and not c.resolved]
    if existing:
        run(path, [{"op": "comment.reply", "comment": existing[0].id, "text": "Otra respuesta"}])
        run(path, [{"op": "comment.resolve", "comment": existing[0].id}])
        counts["replied"] += 1
        counts["resolved"] += 1
    return counts


def test_comment_reply_and_resolve_pass_validator(tmp_path: Path, corpus_doc: CorpusWriter) -> None:
    require_soffice()
    before: dict[str, bytes] = {}
    after: dict[str, bytes] = {}
    totals = Counter[str]()
    for kind in KINDS:
        for seed in (0,) if kind == "novel" else (0, 1):
            path = corpus_doc(kind, seed)
            name = f"{kind}-{seed}"
            before[name] = path.read_bytes()
            totals.update(_roundtrip(path))
            after[name] = path.read_bytes()
    assert totals["added"] >= 15 and totals["replied"] >= 15 and totals["resolved"] >= 15
    base = validate_documents(before, tmp_path / "before")
    final = validate_documents(after, tmp_path / "after")
    introduced = sum(sum((Counter(final[n]) - Counter(base[n])).values()) for n in after)
    print(
        f"ooxml_validator_errors_introduced={introduced} documents={len(after)} "
        f"added={totals['added']} replied={totals['replied']} resolved={totals['resolved']} "
        f"refused={totals['refused']}"
    )
    assert introduced == 0


def test_private_corpus_comments_pass_validator(tmp_path: Path) -> None:
    docs = list(private_documents())
    if not docs:
        pytest.skip(f"{ENV_VAR} not set or holds no .docx files")
    before: dict[str, bytes] = {}
    after: dict[str, bytes] = {}
    for doc_id, data in docs:
        path = tmp_path / "private.docx"
        path.write_bytes(data)
        with contextlib.suppress(DocControlError):  # a refused document stays as it is
            _roundtrip(path)
        before[doc_id], after[doc_id] = data, path.read_bytes()
    base = validate_documents(before, tmp_path / "pb")
    final = validate_documents(after, tmp_path / "pa")
    introduced = sum(sum((Counter(final[n]) - Counter(base[n])).values()) for n in after)
    print(f"private ooxml_validator_errors_introduced={introduced} documents={len(docs)}")
    assert introduced == 0


# -- documents without comments -----------------------------------------------------------


def test_add_creates_all_parts_content_types_and_relationships(tmp_path: Path) -> None:
    path = plain_doc(tmp_path)
    a = addresses(path)
    result = run(path, [{"op": "comment.add", "at": a[1], "text": "Revisar."}], author="Ana")
    files = members(path)
    for name in PART_NAMES:
        assert name in files
    types = files["[Content_Types].xml"].decode()
    rels = files["word/_rels/document.xml.rels"].decode()
    for name in PART_NAMES:
        assert f'PartName="/{name}"' in types
        assert f'Target="{name.removeprefix("word/")}"' in rels
    assert len(set(re.findall(r'Id="(rId\d+)"', rels))) == 5
    [c] = comments(path)
    assert (c.author, c.text, c.resolved, c.reply_to, c.at) == (
        "Ana",
        "Revisar.",
        False,
        None,
        a[1],
    )
    assert result["comment_ids"] == [c.id]
    assert set(result["footprint"]) >= {"word/document.xml", "word/comments.xml"}
    assert Document(str(path)).index.paragraph(2).text == "Segundo párrafo."


def test_add_on_a_range_splits_the_run_and_keeps_text(tmp_path: Path) -> None:
    path = plain_doc(tmp_path)
    a = addresses(path)
    run(path, [{"op": "comment.add", "at": f"{a[0]}:7-14", "text": "Palabra."}])
    doc = Document(str(path))
    assert doc.index.paragraph(1).text == "Primer párrafo del texto."
    xml = doc.index.spans.data.decode()
    assert re.search(
        r"Primer </w:t></w:r><w:commentRangeStart[^>]*/><w:r><w:t[^>]*>párrafo</w:t></w:r>"
        r"<w:commentRangeEnd[^>]*/><w:r><w:commentReference",
        xml,
    )


def test_add_across_paragraphs_and_multiple_comments_in_one_batch(tmp_path: Path) -> None:
    path = plain_doc(tmp_path)
    a = addresses(path)
    result = run(
        path,
        [
            {"op": "comment.add", "at": f"{a[0]}:7..{a[1]}:3", "text": "Cruza."},
            {"op": "comment.add", "at": a[2], "text": "Tercera."},
            {"op": "comment.add", "at": f"{a[0]}:0-6", "text": "Inicio."},
        ],
    )
    assert result["applied"] == 3
    got = {c.text: c for c in comments(path)}
    assert got["Cruza."].at == a[0] and got["Tercera."].at == a[2] and got["Inicio."].at == a[0]
    assert len({c.id for c in got.values()}) == 3
    assert [p.text for p in Document(str(path)).index.paragraphs] == [
        "Primer párrafo del texto.",
        "Segundo párrafo.",
        "Tercero.",
    ]


def test_dry_run_writes_nothing_and_previews(tmp_path: Path) -> None:
    path = plain_doc(tmp_path)
    before = path.read_bytes()
    result = run(
        path, [{"op": "comment.add", "at": addresses(path)[0], "text": "Hola"}], dry_run=True
    )
    assert path.read_bytes() == before and result["dry_run"]
    assert "comment c" in result["preview"][0]["after"]


# -- documents with comments ----------------------------------------------------------------


def test_reply_and_resolve_on_existing_threads(corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("comments")
    old = {c.id: c for c in comments(path)}
    assert sorted(c.id for c in old.values() if c.resolved) == ["4"]
    original = members(path)
    root = next(c for c in old.values() if c.reply_to is None and not c.resolved)
    result = run(
        path,
        [
            {"op": "comment.reply", "comment": root.id, "text": "Hecho."},
            {"op": "comment.resolve", "comment": root.id},
        ],
        author="Ana",
        tag="crítico",
    )
    thread = [c for c in comments(path) if c.id == root.id or c.reply_to == root.id]
    assert thread[0].id == root.id and thread[-1].text == "Hecho."
    assert thread[-1].reply_to == root.id and thread[-1].author == "Ana (crítico)"
    assert thread[-1].at == root.at
    assert all(c.resolved for c in thread)
    assert int(result["comment_ids"][-1]) > max(int(i) for i in old)
    # Untouched threads and untouched parts stay as they were.
    others = [c for c in comments(path) if c.id in old and c.id not in {t.id for t in thread}]
    assert all(old[c.id] == c for c in others)
    after = members(path)
    assert after["word/styles.xml"] == original["word/styles.xml"]
    stem = original["word/comments.xml"].decode().rsplit("</w:comments>", 1)[0]
    assert after["word/comments.xml"].decode().startswith(stem)


def test_reply_to_a_reply_joins_the_thread_and_reopen_works(corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("comments")
    reply = next(c for c in comments(path) if c.reply_to is not None)
    run(path, [{"op": "comment.reply", "comment": reply.id, "text": "Y otra."}])
    new = next(c for c in comments(path) if c.text == "Y otra.")
    assert new.reply_to == reply.reply_to
    run(path, [{"op": "comment.resolve", "comment": reply.id}])
    assert all(
        c.resolved for c in comments(path) if c.id == reply.reply_to or c.reply_to == reply.reply_to
    )
    run(path, [{"op": "comment.resolve", "comment": reply.reply_to, "resolved": False}])
    assert not any(
        c.resolved for c in comments(path) if c.id == reply.reply_to or c.reply_to == reply.reply_to
    )


def test_ids_are_unique_and_parts_agree(corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("kitchen_sink")
    a = addresses(path)
    run(
        path,
        [
            {"op": "comment.add", "at": a[1], "text": "uno"},
            {"op": "comment.add", "at": a[2], "text": "dos"},
        ],
    )
    files = members(path)
    para_ids: list[str] = []
    for name, data in files.items():
        if name.startswith("word/") and name.endswith(".xml"):
            para_ids += re.findall(
                rb'[:]?paraId="([0-9A-F]{8})"', data.upper().replace(b"W14:PARAID", b"PARAID")
            )
    commented = ET.fromstring(files["word/comments.xml"])
    last = [
        list(c.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"))[-1]
        for c in commented
    ]
    last_ids = [p.get("{http://schemas.microsoft.com/office/word/2010/wordml}paraId") for p in last]
    assert len(last_ids) == len(set(last_ids))
    ids = ET.fromstring(files["word/commentsIds.xml"])
    ext = ET.fromstring(files["word/commentsExtensible.xml"])
    durable = {
        e.get("{http://schemas.microsoft.com/office/word/2016/wordml/cid}durableId") for e in ids
    }
    assert durable == {
        e.get("{http://schemas.microsoft.com/office/word/2018/wordml/cex}durableId") for e in ext
    }
    assert all(int(i, 16) < 0x80000000 for i in last_ids if i)


def test_legacy_comments_part_without_extensions_gains_them(tmp_path: Path) -> None:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    legacy = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:comments xmlns:w="{w}">'
        '<w:comment w:id="0" w:author="Vieja" w:initials="V">'
        "<w:p><w:r><w:t>Antigua</w:t></w:r></w:p></w:comment></w:comments>"
    ).encode()
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{REL}comments" Target="comments.xml"/>'
        "</Relationships>"
    ).encode()
    path = make_docx(
        tmp_path / "legacy.docx",
        "Texto comentado.",
        extra={"word/comments.xml": legacy, "word/_rels/document.xml.rels": rels},
    )
    # anchor the legacy comment by hand
    buf = BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.namelist():
            data = src.read(item)
            if item == "word/document.xml":
                data = data.replace(
                    b"<w:p><w:r>",
                    b'<w:p><w:commentRangeStart w:id="0"/><w:r>',
                ).replace(
                    b"</w:r></w:p>",
                    b'</w:r><w:commentRangeEnd w:id="0"/>'
                    b'<w:r><w:commentReference w:id="0"/></w:r></w:p>',
                )
            out.writestr(item, data)
    path.write_bytes(buf.getvalue())
    run(path, [{"op": "comment.reply", "comment": "0", "text": "Nueva"}])
    run(path, [{"op": "comment.resolve", "comment": "0"}])
    got = comments(path)
    assert [(c.id, c.reply_to, c.resolved) for c in got] == [("0", None, True), ("1", "0", True)]
    for name in PART_NAMES:
        assert name in members(path)
    extensible = members(path)["word/commentsExtensible.xml"]
    assert b'dateUtc=""' not in extensible
    ET.fromstring(extensible)


# -- refusals ---------------------------------------------------------------------------------


def test_unknown_comment_and_conflicts_are_refused_without_writing(tmp_path: Path) -> None:
    path = plain_doc(tmp_path)
    before = path.read_bytes()
    a = addresses(path)
    with pytest.raises(DocControlError) as err:
        run(path, [{"op": "comment.resolve", "comment": "99"}])
    assert err.value.code is ErrorCode.COMMENT_NOT_FOUND
    with pytest.raises(DocControlError) as err:
        run(
            path,
            [
                {"op": "text.replace", "at": f"{a[1]}:0-7", "text": "Otro"},
                {"op": "comment.add", "at": a[1], "text": "x"},
            ],
        )
    assert err.value.code is ErrorCode.BATCH_CONFLICT
    with pytest.raises(DocControlError) as err:
        run(path, [{"op": "comment.add", "at": f"{a[0]}:4-4", "text": "x"}])
    assert err.value.code is ErrorCode.ADDRESS_INVALID
    with pytest.raises(DocControlError) as err:
        run(path, [{"op": "comment.add", "at": "p1#00000000", "text": "x"}])
    assert err.value.code is ErrorCode.STALE_ADDRESS
    assert path.read_bytes() == before


def test_comment_with_text_edits_on_other_paragraphs_in_one_batch(tmp_path: Path) -> None:
    path = plain_doc(tmp_path)
    a = addresses(path)
    result = run(
        path,
        [
            {"op": "text.replace", "at": f"{a[2]}:0-7", "text": "Cuarto"},
            {"op": "comment.add", "at": a[0], "text": "Mira abajo."},
        ],
    )
    assert result["applied"] == 2 and [p["op"] for p in result["preview"]] == [0, 1]
    assert len(comments(path)) == 1


def test_comment_ops_are_batch_kinds_only() -> None:
    registry = load_operations()
    for kind in ("comment.add", "comment.reply", "comment.resolve"):
        assert kind not in registry
        assert kind in registry["batch"].schema["properties"]["ops"]["description"]


def test_comments_mix_with_format_ops(tmp_path: Path) -> None:
    path = plain_doc(tmp_path)
    a = addresses(path)
    with pytest.raises(DocControlError) as err:
        run(
            path,
            [
                {"op": "format.apply", "at": f"{a[1]}:0-7", "bold": True},
                {"op": "comment.add", "at": a[1], "text": "x"},
            ],
        )
    assert err.value.code is ErrorCode.BATCH_CONFLICT
    run(
        path,
        [
            {"op": "format.apply", "at": f"{a[1]}:0-7", "bold": True},
            {"op": "comment.add", "at": a[0], "text": "Otra parte."},
        ],
    )
    assert [c.text for c in comments(path)] == ["Otra parte."]
