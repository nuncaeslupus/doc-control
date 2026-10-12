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
