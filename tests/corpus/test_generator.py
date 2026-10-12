"""Synthetic corpus: every generated document passes the validator; generation is deterministic."""

from __future__ import annotations

import re
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from tests.conftest import CorpusWriter
from tests.corpus.generator import KINDS, generate, generate_parts
from tests.corpus.private import ENV_VAR, private_documents, short_id
from tests.corpus.structural import validate_structure
from tests.corpus.validator import validate_documents, validator_name

SEEDS = (0, 1, 2)


def _corpus() -> dict[str, bytes]:
    docs = {f"{kind}-{seed}": generate(kind, seed) for kind in KINDS for seed in SEEDS}
    # The novel is large; one seed is enough for the LibreOffice pass.
    for seed in SEEDS[1:]:
        del docs[f"novel-{seed}"]
    return docs


def _rebuild(parts: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_corpus_generator_documents_pass_validator(tmp_path: Path) -> None:
    docs = _corpus()
    results = validate_documents(docs, tmp_path)
    failing = {doc_id: problems for doc_id, problems in results.items() if problems}
    print(f"validator: {validator_name()}")
    print(f"synthetic_docs_failing_validator={len(failing)} (of {len(docs)})")
    assert failing == {}


@pytest.mark.parametrize("kind", KINDS)
def test_same_seed_gives_same_bytes(kind: str) -> None:
    assert generate(kind, 7) == generate(kind, 7)


def test_different_seeds_give_different_documents() -> None:
    assert generate("kitchen_sink", 1) != generate("kitchen_sink", 2)


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown corpus kind"):
        generate("nope", 0)


def test_novel_is_100k_spanish_words_and_fast() -> None:
    start = time.perf_counter()
    parts = generate_parts("novel", 0)
    data = generate("novel", 0)
    elapsed = time.perf_counter() - start
    xml = parts["word/document.xml"].decode("utf-8")
    text = " ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))
    assert len(text.split()) >= 100_000
    assert elapsed < 5.0, f"novel took {elapsed:.2f}s"
    assert "Capítulo 1" in xml and "* * *" in xml
    assert "—" in text and "«" in text and "»" in text
    assert len(data) < 5_000_000


def test_kitchen_sink_covers_every_structure() -> None:
    parts = generate_parts("kitchen_sink", 0)
    doc = parts["word/document.xml"].decode("utf-8")
    root = doc.split(">", 2)[1]
    assert 'mc:Ignorable="w14 w15 wp14"' in root
    for prefix in ("w14", "w15", "wp14"):
        assert f"xmlns:{prefix}=" in root
    for marker in (
        "<w:ins ",
        "<w:del ",
        "<w:delText",
        "<w:rPr><w:del ",  # deleted paragraph mark
        "<w:rPrChange ",
        "<w:commentRangeStart",
        "<w:commentReference",
        "<w:fldSimple",
        'w:fldCharType="begin"',
        "<w:instrText",
        "<w:hyperlink r:id=",
        "<w:hyperlink w:anchor=",
        "<w:tbl>",
        "<w:footnoteReference",
        "w14:paraId=",
        'w:pStyle w:val="Heading2"',
        "<w:b/>",
    ):
        assert marker in doc, marker
    assert doc.count("<w:tbl>") >= 3  # two tables, one nested
    ext = parts["word/commentsExtended.xml"].decode("utf-8")
    assert "w15:paraIdParent=" in ext and 'w15:done="1"' in ext
    for name in (
        "word/comments.xml",
        "word/commentsIds.xml",
        "word/commentsExtensible.xml",
        "word/people.xml",
        "word/footnotes.xml",
    ):
        assert name in parts
        assert f'PartName="/{name}"' in parts["[Content_Types].xml"].decode()
        assert f'Target="{name.removeprefix("word/")}"' in (
            parts["word/_rels/document.xml.rels"].decode()
        )


def test_corpus_doc_fixture_writes_a_docx(corpus_doc: CorpusWriter) -> None:
    path = corpus_doc("tables", 3)
    assert path.read_bytes() == generate("tables", 3)
    assert zipfile.is_zipfile(path)


# -- the validator catches what it claims to catch ---------------------


def _broken(mutate: str, old: str, new: str) -> bytes:
    parts = generate_parts("kitchen_sink", 0)
    text = parts[mutate].decode("utf-8")
    assert old in text
    parts[mutate] = text.replace(old, new, 1).encode("utf-8")
    return _rebuild(parts)


@pytest.mark.parametrize(
    ("part", "old", "new", "expected"),
    [
        ("word/document.xml", "<w:body>", "<w:body><w:bad>", "does not parse"),
        (
            "word/document.xml",
            '<w:t xml:space="preserve">Before the change, </w:t>',
            "<w:delText>Before the change, </w:delText>",
            "delText outside w:del",
        ),
        ("word/document.xml", 'w:id="2"', 'w:id="1"', "revision w:id 1 used 2 times"),
        ("word/document.xml", 'xmlns:wp14="', 'xmlns:wpX="', "'wp14' is not declared"),
        ("[Content_Types].xml", 'Extension="rels"', 'Extension="relz"', "no content type"),
        ("word/_rels/document.xml.rels", 'Target="styles.xml"', 'Target="x.xml"', "x.xml"),
        ("word/comments.xml", '<w:comment w:id="0"', '<w:comment w:id="99"', "comment"),
        ("word/commentsExtended.xml", "w15:paraIdParent=", "w15:x=", None),
        ("word/commentsIds.xml", "w16cid:durableId=", 'w16cid:durableId="0" x=', "durableIds"),
    ],
)
def test_structural_lint_flags_broken_packages(
    part: str, old: str, new: str, expected: str | None
) -> None:
    problems = validate_structure(_broken(part, old, new))
    if expected is None:  # dropping a reply link is legal; it just flattens the thread
        assert problems == []
    else:
        assert any(expected in p for p in problems), problems


def test_structural_lint_flags_paraid_parent_to_unknown_comment() -> None:
    parts = generate_parts("comments", 0)
    ext = parts["word/commentsExtended.xml"].decode()
    ext = re.sub(r'w15:paraIdParent="[0-9A-F]{8}"', 'w15:paraIdParent="00000001"', ext, count=1)
    parts["word/commentsExtended.xml"] = ext.encode()
    problems = validate_structure(_rebuild(parts))
    assert any("paraIdParent 00000001" in p for p in problems), problems


def test_structural_lint_rejects_non_zip() -> None:
    assert validate_structure(b"not a zip") == ["not a zip package"]


def test_validator_rejects_document_libreoffice_cannot_load(tmp_path: Path) -> None:
    parts = generate_parts("plain", 0)
    parts["word/document.xml"] = b"<w:document"
    results = validate_documents({"broken": _rebuild(parts)}, tmp_path)
    assert results["broken"]


# -- private corpus ----------------------------------------------------

PRIVATE = list(private_documents())


@pytest.mark.skipif(not PRIVATE, reason=f"{ENV_VAR} not set or holds no .docx files")
def test_private_corpus_documents_pass_validator(tmp_path: Path) -> None:
    results = validate_documents(dict(PRIVATE), tmp_path)
    failing = {doc_id: len(problems) for doc_id, problems in results.items() if problems}
    print(f"private_docs_failing_validator={len(failing)} (of {len(PRIVATE)})")
    # Report short ids and problem counts only: never names, never content.
    assert failing == {}


def test_private_corpus_ids_are_short_hashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "Secret Manuscript Title"
    secret.mkdir()
    data = generate("plain", 0)
    (secret / "Chapter One Draft.docx").write_bytes(data)
    (secret / "~$apter One Draft.docx").write_bytes(b"lock")
    monkeypatch.setenv(ENV_VAR, str(secret))
    docs = list(private_documents())
    assert docs == [(short_id(data), data)]
    assert re.fullmatch(r"[0-9a-f]{12}", docs[0][0])


def test_private_corpus_unset_yields_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert list(private_documents()) == []
