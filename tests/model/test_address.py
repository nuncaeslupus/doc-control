"""Addresses and ranges: every paragraph's address resolves back to itself."""

from __future__ import annotations

import io
import zipfile

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import (
    Address,
    AddressRange,
    fingerprint,
    parse_address,
    parse_range,
)
from doc_control.model.index import DocumentIndex, build_index
from tests.corpus.generator import KINDS, generate_parts
from tests.corpus.private import ENV_VAR, corpus_dir, private_documents
from tests.ooxml.docgen import generate

SEEDS = range(400)
CORPUS_SEEDS = range(4)


def _resolution_errors(index: DocumentIndex) -> int:
    """Count paragraphs whose address does not resolve back to the same paragraph."""
    errors = 0
    for para in index.paragraphs:
        try:
            ok = (
                index.resolve_address(str(para.address)) is para
                and index.resolve_range(str(para.address)).first is para
                and index.resolve_range(f"{para.address}:0-{len(para.text)}").last is para
            )
        except DocControlError:
            ok = False
        errors += not ok
    return errors


def test_address_roundtrip_every_paragraph_resolves() -> None:
    errors = paragraphs = documents = 0
    docs = [generate(seed) for seed in SEEDS]
    docs += [generate_parts(k, s)["word/document.xml"] for k in KINDS for s in CORPUS_SEEDS]
    for data in docs:
        index = build_index(data)
        errors += _resolution_errors(index)
        paragraphs += len(index)
        documents += 1
    print(f"address_resolution_errors={errors} documents={documents} paragraphs={paragraphs}")
    assert paragraphs > 1000
    assert errors == 0


def test_private_corpus_addresses_resolve() -> None:
    """The same gate over real documents in ``DOC_CONTROL_PRIVATE_CORPUS`` (skipped if unset).

    Documents are named by a short sha256 of their bytes; failures report only
    the error code or exception type, never document text.
    """
    if corpus_dir() is None:
        pytest.skip(f"{ENV_VAR} is not set")
    docs = list(private_documents())
    if not docs:
        pytest.skip(f"{ENV_VAR} has no .docx files")
    errors = 0
    failures: list[str] = []
    for doc_id, raw in docs:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                data = zf.read("word/document.xml")
            found = _resolution_errors(build_index(data))
        except DocControlError as exc:
            failures.append(f"{doc_id}: {exc.code}")
            continue
        except (KeyError, zipfile.BadZipFile) as exc:
            failures.append(f"{doc_id}: {type(exc).__name__}")
            continue
        if found:
            failures.append(f"{doc_id}: {found} unresolved addresses")
        errors += found
    print(f"address_resolution_errors={errors} documents={len(docs)}")
    assert not failures, "\n".join(failures)


def test_fingerprint_is_eight_hex_digits_of_nfc_blake2s() -> None:
    fp = fingerprint("María")
    assert len(fp) == 8
    int(fp, 16)
    assert fingerprint("María") == fp  # decomposed accent: same NFC text
    assert fingerprint("Maria") != fp


@pytest.mark.parametrize(
    "text",
    [
        "p1#00000000",
        "p142#a7b2a7b2",
        "p142#a7b2a7b2:10-15",
        "p142#a7b2a7b2:0-0",
        "p142#a7b2a7b2:10..p144#0c9e0c9e:3",
        "p142#a7b2a7b2.s3",
    ],
)
def test_parse_range_roundtrips_canonical_forms(text: str) -> None:
    assert str(parse_range(text)) == text


def test_parse_range_fields() -> None:
    rng = parse_range("p142#A7B2A7B2:10..p144#0c9e0c9e:3")
    assert rng == AddressRange(
        Address(142, "a7b2a7b2"), Address(144, "0c9e0c9e"), start_offset=10, end_offset=3
    )
    assert parse_range("p3#0000000a.s2").sentence == 2
    assert parse_address("p7#0000000a") == Address(7, "0000000a")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "p0#a7b2a7b2",
        "p01#a7b2a7b2",
        "p142",
        "p142#a7b2",
        "p142#a7b2a7b2a",
        "p142#g7b2a7b2",
        "P142#a7b2a7b2",
        " p142#a7b2a7b2",
        "p142#a7b2a7b2 ",
        "p142#a7b2a7b2:10",
        "p142#a7b2a7b2:15-10",
        "p142#a7b2a7b2:-1-5",
        "p142#a7b2a7b2:01-5",
        "p142#a7b2a7b2:1..p142#a7b2a7b2:3",
        "p144#0c9e0c9e:1..p142#a7b2a7b2:3",
        "p142#a7b2a7b2:1..p144#0c9e0c9e",
        "p142#a7b2a7b2.s0",
        "p142#a7b2a7b2.s",
        "p9999999999999#a7b2a7b2",
        "p142#a7b2a7b2\n",
    ],
)
def test_malformed_ranges_raise_address_invalid(text: str) -> None:
    with pytest.raises(DocControlError) as err:
        parse_range(text)
    assert err.value.code is ErrorCode.ADDRESS_INVALID


@pytest.mark.parametrize("bad", ["p1#a7b2a7b2:0-1", "p1#a7b2a7b2.s1", None, 7])
def test_parse_address_rejects_offsets_and_non_strings(bad: object) -> None:
    with pytest.raises(DocControlError) as err:
        parse_address(bad)  # type: ignore[arg-type]
    assert err.value.code is ErrorCode.ADDRESS_INVALID


STORY_FORMS = [
    "fn3/p1#0c9e0c9e",
    "fn3/p1#0c9e0c9e:4-10",
    "en12/p2#0c9e0c9e.s2",
    "hdr1.default/p1#5e5e5e5e",
    "ftr2.first/p1#5e5e5e5e:2..ftr2.first/p3#0c9e0c9e:5",
    "hdr10.even/p1#5e5e5e5e",
]


def _body_digests() -> dict[str, dict[str, object]]:
    import hashlib
    import tempfile
    from pathlib import Path

    from doc_control import api
    from tests.corpus.generator import generate as generate_doc

    out: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for kind in KINDS:
            path = Path(tmp) / f"{kind}.docx"
            path.write_bytes(generate_doc(kind, 0))
            with api.open(path) as handle:
                addresses = [p["at"] for p in handle.paragraphs()]
            assert all(str(parse_range(at)) == at for at in addresses)
            digest = hashlib.sha256("\n".join(addresses).encode()).hexdigest()
            out[kind] = {"count": len(addresses), "sha256": digest}
    return out


def test_story_prefix_round_trips_and_body_unchanged() -> None:
    import json
    from pathlib import Path

    for text in STORY_FORMS:
        rng = parse_range(text)
        assert str(rng) == text
        assert rng.start.story == text.split("/")[0]
    assert parse_address("fn3/p1#0C9E0C9E") == Address(1, "0c9e0c9e", "fn3")
    assert parse_address("p1#0c9e0c9e") == Address(1, "0c9e0c9e")
    for bad in (
        "fn3/p1#0c9e0c9e:2..p2#0c9e0c9e:1",  # crosses from a note into the body
        "p1#0c9e0c9e:2..fn3/p2#0c9e0c9e:1",
        "fn3/p1#0c9e0c9e:2..fn4/p2#0c9e0c9e:1",
        "fn0/p1#0c9e0c9e",  # separator notes are not addressable
        "hdr1.odd/p1#0c9e0c9e",
        "hdr1/p1#0c9e0c9e",
        "/p1#0c9e0c9e",
    ):
        with pytest.raises(DocControlError) as err:
            parse_range(bad)
        assert err.value.code is ErrorCode.ADDRESS_INVALID
    # body_address_drift == 0: every 0.6.0 body address of every corpus kind is unchanged.
    baseline = json.loads(
        (Path(__file__).parent / "data" / "body_addresses_0_6_0.json").read_text()
    )
    assert _body_digests() == baseline


def test_story_address_on_the_body_index_is_refused() -> None:
    index = build_index(generate_parts("plain", 0)["word/document.xml"])
    with pytest.raises(DocControlError) as err:
        index.resolve_address(f"fn3/p1#{index.paragraphs[0].fp}")
    assert err.value.code is ErrorCode.ADDRESS_INVALID
