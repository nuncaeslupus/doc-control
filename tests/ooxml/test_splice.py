"""Paragraph spans and the splice writer: untouched bytes stay byte-identical."""

from __future__ import annotations

import random
import re
import xml.etree.ElementTree as ET

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.splice import W_NS, DocumentSpans, scan, splice
from tests.ooxml.docgen import FOREIGN_NS, generate

SEEDS = range(400)
W = f"{{{W_NS}}}"


def _parse_full(data: bytes) -> ET.Element:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    parser.feed(data)
    return parser.close()


def _canon(elem: ET.Element) -> object:
    return (
        elem.tag if isinstance(elem.tag, str) else repr(elem.tag),
        sorted(elem.attrib.items()),
        elem.text or "",
        [_canon(child) for child in elem],
        elem.tail or "",
    )


def _canon_no_tail(elem: ET.Element) -> object:
    c = _canon(elem)
    assert isinstance(c, tuple)
    return c[:-1]


def _et_paragraphs(data: bytes) -> list[ET.Element]:
    root = _parse_full(data)
    body = root.find(f"{W}body")
    assert body is not None
    return list(body.iter(f"{W}p"))


def test_splice_identity_edit_returns_identical_bytes() -> None:
    """Splicing every paragraph's own bytes back yields the original document."""
    mismatches = 0
    total_paragraphs = 0
    for seed in SEEDS:
        data = generate(seed)
        spans = scan(data)
        total_paragraphs += len(spans.paragraphs)
        own = {p.index: data[p.start : p.end] for p in spans.paragraphs}
        parents = {p.parent for p in spans.paragraphs}
        # Disjoint sets in one call: every top-level paragraph; every innermost one.
        top = {i: b for i, b in own.items() if not spans.paragraphs[i].nested}
        leaves = {i: b for i, b in own.items() if i not in parents}
        for batch in (top, leaves, {}):
            if splice(data, batch) != data:
                mismatches += 1
        # And each paragraph on its own, nested ones included.
        for i, b in own.items():
            if spans.splice({i: b}) != data:
                mismatches += 1
    print(f"splice_roundtrip_mismatches={mismatches} documents={len(SEEDS)}")
    assert total_paragraphs > 1000
    assert mismatches == 0


@pytest.mark.parametrize("seed", range(0, 400, 7))
def test_spans_match_elementtree_paragraphs(seed: int) -> None:
    """Spans are found in document order and agree with ElementTree element by element."""
    data = generate(seed)
    spans = scan(data)
    et_paras = _et_paragraphs(data)
    assert len(spans.paragraphs) == len(et_paras)
    for span, elem in zip(spans.paragraphs, et_paras, strict=True):
        assert _canon_no_tail(spans.parse_paragraph(span.index)) == _canon_no_tail(elem)
    for a, b in zip(spans.paragraphs, spans.paragraphs[1:], strict=False):
        assert a.start < b.start
        if b.nested and b.parent is not None:
            outer = spans.paragraphs[b.parent]
            assert outer.start < b.start and b.end <= outer.end
        elif not a.nested and not b.nested:
            assert a.end <= b.start


def test_generator_covers_the_awkward_cases() -> None:
    corpus = [generate(s) for s in SEEDS]
    joined = b"".join(corpus)
    for needle in (b"<![CDATA[", b"<!--", b"<?dc-pi", b"mc:Ignorable", b"xmlns:q=", b"/>"):
        assert needle in joined
    assert FOREIGN_NS.encode() in joined
    assert any(b"<p" in d and b"xmlns=" in d for d in corpus)
    assert sum(sum(p.nested for p in scan(d).paragraphs) for d in corpus) > 20


def _edit(elem: ET.Element, rng: random.Random) -> None:
    choice = rng.randrange(4)
    texts = [e for e in elem.iter(f"{W}t")]
    if choice == 0 and texts:
        texts[0].text = (texts[0].text or "") + " <editado> & ñ"
    elif choice == 1:
        run = ET.SubElement(elem, f"{W}r")
        t = ET.SubElement(run, f"{W}t")
        t.text = " nuevo "
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    elif choice == 2:
        elem.set(f"{W}rsidR", "00AB12CD")
        elem.set("{urn:example:brand-new}flag", 'a"b')
    else:
        for child in list(elem):
            elem.remove(child)
        elem.text = None


def test_random_real_edits_keep_untouched_bytes_and_parse() -> None:
    """Edit, insert and delete random paragraphs; every other byte stays put."""
    checked = 0
    for seed in SEEDS:
        data = generate(seed)
        spans = scan(data)
        top = [p for p in spans.paragraphs if not p.nested]
        if not top:
            continue
        rng = random.Random(seed * 31 + 7)
        edited: dict[int, ET.Element] = {}
        for p in rng.sample(top, k=min(len(top), rng.randint(1, 3))):
            elem = spans.parse_paragraph(p.index)
            _edit(elem, rng)
            edited[p.index] = elem
        replacements = {i: spans.serialize(e, i) for i, e in edited.items()}
        deletable = [p.index for p in top if p.index not in edited]
        deletes = rng.sample(deletable, k=min(len(deletable), rng.randint(0, 2)))
        anchor = rng.choice(top).index
        new_para = spans.serialize(_new_paragraph("insertado"), anchor)
        out = spans.splice(replacements, insert_after={anchor: [new_para]}, delete=deletes)

        # Independent expectation: original bytes with each operation applied in order.
        ops: list[tuple[int, int, bytes]] = []
        for p in top:
            if p.index in replacements:
                ops.append((p.start, p.end, replacements[p.index]))
            elif p.index in deletes:
                ops.append((p.start, p.end, b""))
            if p.index == anchor:
                ops.append((p.end, p.end, new_para))
        expected, pos = bytearray(), 0
        for start, end, new in ops:
            expected += data[pos:start] + new
            pos = end
        expected += data[pos:]
        assert out == bytes(expected), seed

        # A namespace bound at the root never gets a generated nsN prefix.
        generated = {uri.decode() for uri in re.findall(rb'xmlns:ns\d+="([^"]*)"', out)}
        assert not generated & set(spans.root_scope.values()), seed
        new_spans = scan(out)
        assert out[: new_spans.root_end] == data[: spans.root_end]
        assert len([p for p in new_spans.paragraphs if not p.nested]) == len(top) - len(deletes) + 1
        _parse_full(out)  # still well-formed
        # Edited paragraphs read back as the edited trees.
        new_top = [p for p in new_spans.paragraphs if not p.nested]
        it = iter(new_top)
        for old in top:
            if old.index in deletes:
                pass
            elif old.index in edited:
                got = new_spans.parse_paragraph(next(it).index)
                assert _canon_no_tail(got) == _canon_no_tail(edited[old.index]), seed
            else:
                same = next(it)
                assert out[same.start : same.end] == data[old.start : old.end]
            if old.index == anchor:
                inserted = new_spans.parse_paragraph(next(it).index)
                assert inserted.findtext(f"{W}r/{W}t") == "insertado"
        checked += 1
    assert checked > 300


def _new_paragraph(text: str) -> ET.Element:
    p = ET.Element(f"{W}p")
    t = ET.SubElement(ET.SubElement(p, f"{W}r"), f"{W}t")
    t.text = text
    return p


def test_serialize_uses_original_prefixes() -> None:
    data = (
        b'<?xml version="1.0"?>\n<x:document xmlns:x="' + W_NS.encode() + b'" '
        b'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" mc:Ignorable="w14" '
        b'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        b"<x:body><x:p w14:paraId='01'><x:r><x:t>a</x:t></x:r></x:p></x:body></x:document>"
    )
    spans = scan(data)
    elem = spans.parse_paragraph(0)
    elem.find(f"{W}r/{W}t").text = "b & c"  # type: ignore[union-attr]
    out = spans.splice({0: spans.serialize(elem, 0)})
    assert out == data.replace(b"<x:p w14:paraId='01'>", b'<x:p w14:paraId="01">').replace(
        b">a<", b">b &amp; c<"
    )


def test_serialize_declares_unknown_namespaces_on_the_paragraph() -> None:
    data = (
        b'<document xmlns="' + W_NS.encode() + b'" xmlns:o="urn:example:other">'
        b'<body><p/><p xmlns:z="urn:example:z"><z:x/></p></body></document>'
    )
    spans = scan(data)
    elem = _new_paragraph("x")
    elem.set("{urn:example:brand-new}flag", "1")
    elem.set(f"{W}rsidR", "1")
    ET.SubElement(elem, "{urn:example:other}thing")
    ET.SubElement(elem, "{urn:example:z}y")
    raw = spans.serialize(elem)
    # Default-namespace W keeps unprefixed element names; its attribute needs
    # a prefix (w), o: is reused, z: comes from elsewhere in the document.
    assert raw == (
        b'<p xmlns:ns0="urn:example:brand-new" xmlns:w="' + W_NS.encode() + b'" '
        b'xmlns:z="urn:example:z" ns0:flag="1" w:rsidR="1">'
        b"<r><t>x</t></r><o:thing/><z:y/></p>"
    )
    out = spans.splice({}, insert_after={-1: [raw]})
    root = _parse_full(out)
    assert root.find(f"{W}body/{W}p[@{{urn:example:brand-new}}flag='1']") is not None


def test_self_closing_and_tricky_markup() -> None:
    data = (
        b"<?xml version='1.0'?><!-- <w:p> --><a:document xmlns:a=\""
        + W_NS.encode()
        + b"\"><a:body><a:p/><a:p a:x='1>2' ><![CDATA[x]]></a:p ><a:p\n/>"
        b"<?pi <a:p>?><a:p></a:p></a:body></a:document>"
    )
    spans = scan(data)
    assert [data[p.start : p.end] for p in spans.paragraphs] == [
        b"<a:p/>",
        b"<a:p a:x='1>2' ><![CDATA[x]]></a:p >",
        b"<a:p\n/>",
        b"<a:p></a:p>",
    ]
    out = splice(data, {1: b"<a:p/>"}, delete=[0], insert_after={3: [b"<a:p>z</a:p>"]})
    assert out == data.replace(b"<a:p/><a:p a:x='1>2' ><![CDATA[x]]></a:p >", b"<a:p/>").replace(
        b"<a:p></a:p>", b"<a:p></a:p><a:p>z</a:p>"
    )


def test_paragraph_outside_body_and_foreign_p_are_ignored() -> None:
    data = (
        b'<w:document xmlns:w="' + W_NS.encode() + b'" xmlns:o="urn:o">'
        b"<w:background><w:p/></w:background><w:body><o:p/><w:p/></w:body></w:document>"
    )
    spans = scan(data)
    assert len(spans.paragraphs) == 1
    assert data[spans.paragraphs[0].start :].startswith(b"<w:p/></w:body>")


def test_insert_into_document_without_paragraphs() -> None:
    data = b'<w:document xmlns:w="' + W_NS.encode() + b'"><w:body><w:sectPr/></w:body></w:document>'
    out = splice(data, {}, insert_after={-1: [b"<w:p/>"]})
    assert out == data.replace(b"<w:body>", b"<w:body><w:p/>")


def test_conflicting_operations_are_rejected() -> None:
    data = generate(next(s for s in SEEDS if any(p.nested for p in scan(generate(s)).paragraphs)))
    spans = scan(data)
    inner = next(p for p in spans.paragraphs if p.nested)
    assert inner.parent is not None
    with pytest.raises(DocControlError) as exc:
        spans.splice({inner.parent: b"<w:p/>", inner.index: b"<w:p/>"})
    assert exc.value.code is ErrorCode.BATCH_CONFLICT
    with pytest.raises(DocControlError) as exc:
        spans.splice({0: b"<w:p/>"}, delete=[0])
    assert exc.value.code is ErrorCode.BATCH_CONFLICT
    with pytest.raises(IndexError):
        spans.splice({len(spans.paragraphs): b""})


def test_edit_inside_text_box() -> None:
    seed = next(s for s in SEEDS if any(p.nested for p in scan(generate(s)).paragraphs))
    data = generate(seed)
    spans = scan(data)
    inner = next(p for p in spans.paragraphs if p.nested)
    elem = spans.parse_paragraph(inner.index)
    elem.append(_new_paragraph("x")[0])
    out = spans.splice({inner.index: spans.serialize(elem, inner.index)})
    assert out[: inner.start] == data[: inner.start]
    assert out.endswith(data[inner.end :])
    _parse_full(out)


def test_broken_splice_result_is_refused() -> None:
    data = generate(1)
    spans = scan(data)
    if not spans.paragraphs:
        data = generate(2)
        spans = scan(data)
    with pytest.raises(DocControlError) as exc:
        spans.splice({0: b"<w:p>"})
    assert exc.value.code is ErrorCode.INTERNAL


@pytest.mark.parametrize(
    "data",
    [
        b"<w:document xmlns:w='" + W_NS.encode() + b"'><w:body><w:p></w:body></w:document>",
        b"<w:document xmlns:w='" + W_NS.encode() + b"'><w:body><w:p a='1></w:body>",
        b"\xff\xfe<\x00",
        b"<!DOCTYPE x [<!ENTITY a 'b'>]><x/>",
        b"<x:document xmlns:w='" + W_NS.encode() + b"'/>",
        b"no markup",
        b"<a xmlns='urn:a'/>",
        b"<w:document xmlns:w='" + W_NS.encode() + b"'/><x/>",
        b"<w:document xmlns:w='" + W_NS.encode() + b"'><w:body>",
        b"<w:document xmlns:w='" + W_NS.encode() + b"'><!-- open",
        b"<w:document xmlns:w='" + W_NS.encode() + b"'><![CDATA[ open",
        b"<w:document xmlns:w='" + W_NS.encode() + b"'><?pi open",
        b"<w:document xmlns:w='" + W_NS.encode() + b"'><:x/></w:document>",
        b"<w:document xmlns:w='" + W_NS.encode() + b"'></ w:document>",
        b"<w:document xmlns:w='" + W_NS.encode() + b"'>< w:x/></w:document>",
    ],
)
def test_malformed_input_raises(data: bytes) -> None:
    with pytest.raises(DocControlError):
        scan(data)


def test_document_spans_type() -> None:
    assert isinstance(scan(generate(0)), DocumentSpans)


def test_namespace_uri_with_character_references_resolves() -> None:
    uri = W_NS.replace("main", "m&#97;i&#x6E;").encode()
    data = b'<v:document xmlns:v="' + uri + b'"><v:body><v:p/></v:body></v:document>'
    assert len(scan(data).paragraphs) == 1


def test_insert_into_self_closing_body_is_refused() -> None:
    data = b'<w:document xmlns:w="' + W_NS.encode() + b'"><w:body/></w:document>'
    spans = scan(data)
    assert spans.paragraphs == ()
    with pytest.raises(DocControlError) as exc:
        spans.splice(insert_after={-1: [b"<w:p/>"]})
    assert exc.value.code is ErrorCode.UNSUPPORTED_CONTENT


def test_paragraph_bytes_and_scope() -> None:
    data = (
        b'<w:document xmlns:w="' + W_NS.encode() + b'"><w:body>'
        b'<q:p xmlns:q="' + W_NS.encode() + b'"/></w:body></w:document>'
    )
    spans = scan(data)
    p = spans.paragraphs[0]
    assert spans.paragraph_bytes(0) == b'<q:p xmlns:q="' + W_NS.encode() + b'"/>'
    assert p.own_decls == (("q", W_NS),)
    assert p.scope["q"] == p.scope["w"] == W_NS
