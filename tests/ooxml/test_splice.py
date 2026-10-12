"""Paragraph spans and the splice writer: untouched bytes stay byte-identical."""

from __future__ import annotations

import hashlib
import io
import os
import random
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.ooxml.splice import W_NS, DocumentSpans, ParagraphSpan, scan, splice
from tests.ooxml.docgen import FOREIGN_NS, SHADOW_NS, WPS_NS, generate

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


def _text(elem: ET.Element) -> str:
    return "".join(elem.itertext())


def _span_as_element(data: bytes, span: ParagraphSpan) -> ET.Element:
    """Parse a span's bytes on their own (ElementTree, not the splice reader)."""
    decls = "".join(
        f' xmlns{":" + k if k else ""}="{v.replace("&", "&amp;").replace(chr(34), "&quot;")}"'
        for k, v in span.outer_scope.items()
        if k != "xml"
    )
    wrapper = _parse_full(f"<wrap{decls}>".encode() + data[span.start : span.end] + b"</wrap>")
    assert len(wrapper) == 1, f"span {span.index} holds {len(wrapper)} nodes"
    assert not wrapper.text and not wrapper[0].tail, f"span {span.index} has stray text"
    assert wrapper[0].tag == f"{W}p", f"span {span.index} is {wrapper[0].tag!r}"
    return wrapper[0]


_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_DECL = re.compile(rb"""xmlns(?::([^\s=]+))?\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _undeclared_mce_prefixes(data: bytes) -> list[str]:
    """MCE attribute values naming prefixes not in scope (ElementTree's ns events)."""
    problems: list[str] = []
    scopes: list[dict[str, str]] = [{"xml": "http://www.w3.org/XML/1998/namespace"}]
    pending: dict[str, str] = {}
    for event, item in ET.iterparse(io.BytesIO(data), events=("start", "end", "start-ns")):
        if event == "start-ns":
            prefix, uri = item
            pending[prefix] = uri
        elif event == "start":
            scope = {**scopes[-1], **pending}
            pending = {}
            scopes.append(scope)
            assert isinstance(item, ET.Element)
            for name, value in item.attrib.items():
                if name in (f"{_MC}Ignorable", f"{_MC}MustUnderstand") or (
                    item.tag == f"{_MC}Choice" and name == "Requires"
                ):
                    names = value.split()
                elif name in (
                    f"{_MC}ProcessContent",
                    f"{_MC}PreserveElements",
                    f"{_MC}PreserveAttributes",
                ):
                    names = [t.partition(":")[0] for t in value.split() if ":" in t]
                else:
                    continue
                problems += [f"{name}={value!r}" for n in names if n not in scope]
        else:
            scopes.pop()
    return problems


def _decls(raw: bytes) -> set[tuple[bytes, bytes]]:
    return {(m.group(1) or b"", m.group(2) or m.group(3) or b"") for m in _DECL.finditer(raw)}


def _gate_mismatches(data: bytes) -> tuple[int, int]:
    """Run the splice gate over one document: ``(mismatches, paragraphs)``.

    A mismatch is a splice of unchanged bytes that does not return the
    original. Spans are also checked against ElementTree directly (each is
    exactly one ``w:p``, same count and texts in document order), and
    re-serialising paragraphs and splicing them back must leave ElementTree's
    view of the document unchanged — so a wrong span cannot hide behind an
    identity replacement. Those checks fail by assertion.
    """
    spans = scan(data)
    et_paras = _et_paragraphs(data)
    assert len(spans.paragraphs) == len(et_paras), "span count differs from ElementTree"
    texts = [_text(e) for e in et_paras]
    for span, elem in zip(spans.paragraphs, et_paras, strict=True):
        own = _span_as_element(data, span)
        assert _canon_no_tail(own) == _canon_no_tail(elem), f"span {span.index} is another p"
        assert _text(own) == texts[span.index]
    mismatches = 0
    own_bytes = {p.index: data[p.start : p.end] for p in spans.paragraphs}
    parents = {p.parent for p in spans.paragraphs}
    # Disjoint sets in one call: every top-level paragraph; every innermost one.
    top = {i: b for i, b in own_bytes.items() if not spans.paragraphs[i].nested}
    leaves = {i: b for i, b in own_bytes.items() if i not in parents}
    for batch in (top, leaves, {}):
        if splice(data, batch) != data:
            mismatches += 1
    # And each paragraph on its own, nested ones included.
    for i, b in own_bytes.items():
        if spans.splice({i: b}) != data:
            mismatches += 1
    # Re-serialised paragraphs spliced back leave the document's meaning unchanged.
    want = _canon(_parse_full(data))
    assert _undeclared_mce_prefixes(data) == []
    for batch_ids in (top, leaves):
        batch = {i: spans.serialize(spans.parse_paragraph(i), i) for i in batch_ids}
        for i, raw in batch.items():  # every declaration the paragraph made survives
            lost = _decls(own_bytes[i]) - _decls(raw)
            assert not lost, f"paragraph {i} lost declarations {lost}"
        out = spans.splice(batch, check=False)  # judged independently below
        assert [_text(e) for e in _et_paragraphs(out)] == texts, "texts changed"
        assert _canon(_parse_full(out)) == want, "re-serialised paragraphs changed the tree"
        assert _undeclared_mce_prefixes(out) == [], "MCE value names an undeclared prefix"
    return mismatches, len(spans.paragraphs)


def test_splice_identity_edit_returns_identical_bytes() -> None:
    """Splicing every paragraph's own bytes back yields the original document."""
    mismatches = 0
    total_paragraphs = 0
    for seed in SEEDS:
        found, count = _gate_mismatches(generate(seed))
        mismatches += found
        total_paragraphs += count
    print(f"splice_roundtrip_mismatches={mismatches} documents={len(SEEDS)}")
    assert total_paragraphs > 1000
    assert mismatches == 0


def test_private_corpus_passes_the_splice_gate() -> None:
    """The same gate over real documents in ``DOC_CONTROL_PRIVATE_CORPUS`` (skipped if unset).

    Documents are named by a short sha256 of their bytes, never by file name.
    """
    root = os.environ.get("DOC_CONTROL_PRIVATE_CORPUS")
    if not root:
        pytest.skip("DOC_CONTROL_PRIVATE_CORPUS is not set")
    paths = sorted(Path(root).rglob("*.docx"))
    if not paths:
        pytest.skip("DOC_CONTROL_PRIVATE_CORPUS has no .docx files")
    mismatches = 0
    failures: list[str] = []
    for path in paths:
        raw = path.read_bytes()
        doc_id = hashlib.sha256(raw).hexdigest()[:12]
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                data = zf.read("word/document.xml")
            found, _ = _gate_mismatches(data)
        except (AssertionError, DocControlError, KeyError, zipfile.BadZipFile) as exc:
            failures.append(f"{doc_id}: {type(exc).__name__}: {exc}"[:300])
            continue
        if found:
            failures.append(f"{doc_id}: {found} identity mismatches")
        mismatches += found
    print(f"splice_roundtrip_mismatches={mismatches} documents={len(paths)}")
    assert not failures, "\n".join(failures)


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
    for needle in (
        b"<mc:AlternateContent xmlns:wps=",
        b"<mc:Choice xmlns:wps=",
        b'Requires="wps"',
        b"<wps:wsp>",
        b"fldSimple",
        b'fldCharType="separate"',
        b"instrText",
        b"bookmarkStart",
        b"bookmarkEnd",
        b"mc:Ignorable='w14 ",
        b'xmlns:o="' + SHADOW_NS.encode(),
    ):
        assert needle in joined, needle
    assert b'="' + WPS_NS.encode() in joined
    # Some document has a body-level bookmark between blocks.
    assert any(re.search(rb"body>(<[^>]*/>)*<\w*:?bookmarkStart", d) for d in corpus)
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
    assert exc.value.code is ErrorCode.SCHEMA_INVALID  # refused before splicing


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


# ---------------------------------------------------------------------------
# Review findings (PR #45)

MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_ROOT = b'<w:document xmlns:w="' + W_NS.encode() + b'" xmlns:mc="' + MC_NS.encode() + b'"'


def _roundtrip_paragraph(data: bytes, index: int = 0) -> bytes:
    spans = scan(data)
    return spans.serialize(spans.parse_paragraph(index), index)


@pytest.mark.parametrize(
    "inner",
    [
        # wps is used by an element: ElementTree keeps the URI but drops the declaration.
        b'<mc:AlternateContent xmlns:wps="urn:wps"><mc:Choice Requires="wps">'
        b"<wps:wsp/></mc:Choice><mc:Fallback/></mc:AlternateContent>",
        # wps is only named by Requires: ElementTree forgets it entirely.
        b'<mc:AlternateContent xmlns:wps="urn:wps"><mc:Choice Requires="wps">'
        b"<w:drawing/></mc:Choice><mc:Fallback/></mc:AlternateContent>",
    ],
)
def test_namespace_declared_inside_paragraph_survives(inner: bytes) -> None:
    data = _ROOT + b"><w:body><w:p><w:r>" + inner + b"</w:r></w:p></w:body></w:document>"
    raw = _roundtrip_paragraph(data)
    assert raw == b"<w:p><w:r>" + inner + b"</w:r></w:p>"
    out = scan(data).splice({0: raw})
    assert out == data


def test_redeclared_prefix_is_kept_with_its_ignorable_list() -> None:
    data = (
        _ROOT + b' xmlns:a="urn:x"><w:body><w:p>'
        b'<w:r xmlns:b="urn:x" mc:Ignorable="b"><b:foo/></w:r></w:p></w:body></w:document>'
    )
    raw = _roundtrip_paragraph(data)
    assert raw == b'<w:p><w:r xmlns:b="urn:x" mc:Ignorable="b"><b:foo/></w:r></w:p>'


def test_edited_paragraph_keeps_inner_declarations() -> None:
    data = (
        _ROOT + b' xmlns:a="urn:x"><w:body><w:p><w:r xmlns:b="urn:x" mc:Ignorable="b">'
        b"<b:foo/><w:t>a</w:t></w:r></w:p></w:body></w:document>"
    )
    spans = scan(data)
    elem = spans.parse_paragraph(0)
    run = elem.find(f"{W}r")
    assert run is not None
    run.find(f"{W}t").text = "b"  # type: ignore[union-attr]
    ET.SubElement(run, "{urn:x}bar")
    out = spans.splice({0: spans.serialize(elem, 0)})
    assert b'<w:r xmlns:b="urn:x" mc:Ignorable="b"><b:foo/><w:t>b</w:t><b:bar/></w:r>' in out


@pytest.mark.parametrize(
    ("local", "value"),
    [
        ("Ignorable", "zz"),
        ("MustUnderstand", "zz"),
        ("ProcessContent", "w:r zz:*"),
        ("PreserveElements", "zz:a"),
        ("PreserveAttributes", "zz:b"),
    ],
)
def test_serialize_refuses_mce_value_naming_undeclared_prefix(local: str, value: str) -> None:
    data = _ROOT + b"><w:body><w:p/></w:body></w:document>"
    spans = scan(data)
    elem = _new_paragraph("x")
    elem[0].set(f"{{{MC_NS}}}{local}", value)
    with pytest.raises(DocControlError) as exc:
        spans.serialize(elem, 0)
    assert exc.value.code is ErrorCode.INTERNAL


def test_serialize_refuses_choice_requiring_undeclared_prefix() -> None:
    data = _ROOT + b"><w:body><w:p/></w:body></w:document>"
    spans = scan(data)
    elem = _new_paragraph("x")
    alt = ET.SubElement(elem[0], f"{{{MC_NS}}}AlternateContent")
    ET.SubElement(alt, f"{{{MC_NS}}}Choice", Requires="wps")
    with pytest.raises(DocControlError) as exc:
        spans.serialize(elem, 0)
    assert exc.value.code is ErrorCode.INTERNAL


@pytest.mark.parametrize(
    "para",
    [
        b'<w:p><w:r mc:Ignorable="zz"/></w:p>',
        b'<w:p><mc:AlternateContent><mc:Choice Requires="w zz"/></mc:AlternateContent></w:p>',
        b'<w:p xmlns:k="urn:k"><w:r mc:ProcessContent="k:x zz:y"/></w:p>',
    ],
)
def test_splice_check_refuses_undeclared_mce_prefix(para: bytes) -> None:
    data = _ROOT + b"><w:body><w:p/></w:body></w:document>"
    spans = scan(data)
    for replacements, inserts in (
        ({0: para}, None),
        (None, {0: [para]}),
        (None, {-1: [para]}),
    ):
        with pytest.raises(DocControlError) as exc:
            spans.splice(replacements, insert_after=inserts)
        assert exc.value.code is ErrorCode.INTERNAL


def test_splice_check_accepts_declared_mce_prefixes() -> None:
    data = _ROOT + b' xmlns:w14="urn:w14"><w:body><w:p/></w:body></w:document>'
    para = (
        b'<w:p xmlns:k="urn:k" mc:Ignorable="w14 k"><mc:AlternateContent>'
        b'<mc:Choice Requires="k"/></mc:AlternateContent></w:p>'
    )
    out = scan(data).splice({0: para})
    assert para in out


@pytest.mark.parametrize(
    "first",
    [
        b"<w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>",
        b"<w:sdt><w:sdtContent><w:p/></w:sdtContent></w:sdt>",
    ],
)
def test_insert_before_everything_lands_at_body_level(first: bytes) -> None:
    data = _ROOT + b"><w:body>" + first + b"<w:p/></w:body></w:document>"
    new = b"<w:p><w:r><w:t>new</w:t></w:r></w:p>"
    out = splice(data, insert_after={-1: [new]})
    assert out == data.replace(b"<w:body>", b"<w:body>" + new)
    body = _parse_full(out).find(f"{W}body")
    assert body is not None and body[0].tag == f"{W}p"
    # Replacing the first paragraph too still puts the insertion first.
    out = splice(data, {0: b"<w:p/>"}, insert_after={-1: [new]})
    assert out.startswith(_ROOT + b"><w:body>" + new)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        # Invalid UTF-8 inside a name and inside text.
        (_ROOT + b"><w:body><w:p\xff/></w:body></w:document>", ErrorCode.NOT_A_DOCX),
        (_ROOT + b"><w:body><w:p>\xc3(</w:p></w:body></w:document>", ErrorCode.NOT_A_DOCX),
        # Character references beyond Unicode, to a surrogate, absurdly long.
        (_ROOT + b' xmlns:x="urn:&#x110000;"><w:body/></w:document>', ErrorCode.NOT_A_DOCX),
        (_ROOT + b' xmlns:x="urn:&#xD800;"><w:body/></w:document>', ErrorCode.NOT_A_DOCX),
        (_ROOT + b' xmlns:x="urn:&#99999999999;"><w:body/></w:document>', ErrorCode.NOT_A_DOCX),
        # An entity no DTD defines, and a bare ampersand.
        (_ROOT + b' xmlns:x="urn:&nbsp;"><w:body/></w:document>', ErrorCode.UNSUPPORTED_CONTENT),
        (_ROOT + b' xmlns:x="urn:a&b"><w:body/></w:document>', ErrorCode.NOT_A_DOCX),
    ],
)
def test_malformed_bytes_raise_doc_control_errors(data: bytes, code: ErrorCode) -> None:
    with pytest.raises(DocControlError) as exc:
        scan(data)
    assert exc.value.code is code


@pytest.mark.parametrize(
    ("para", "code"),
    [
        (b"<w:p><w:r><w:t>&nbsp;</w:t></w:r></w:p>", ErrorCode.UNSUPPORTED_CONTENT),
        (b"<w:p><w:r><w:t>&#x110000;</w:t></w:r></w:p>", ErrorCode.NOT_A_DOCX),
        (b"<w:p><w:r><w:t>&#0;</w:t></w:r></w:p>", ErrorCode.NOT_A_DOCX),
        (b"<w:p><w:r><w:t a='1' a='2'/></w:r></w:p>", ErrorCode.NOT_A_DOCX),
    ],
)
def test_parse_paragraph_maps_malformed_content(para: bytes, code: ErrorCode) -> None:
    data = _ROOT + b"><w:body>" + para + b"</w:body></w:document>"
    spans = scan(data)
    with pytest.raises(DocControlError) as exc:
        spans.parse_paragraph(0)
    assert exc.value.code is code


def test_bool_indices_are_rejected() -> None:
    data = _ROOT + b"><w:body><w:p/><w:p/></w:body></w:document>"
    spans = scan(data)
    with pytest.raises(TypeError):
        spans.splice({True: b"<w:p/>"})
    with pytest.raises(TypeError):
        spans.splice(delete=[False])
    with pytest.raises(TypeError):
        spans.splice(insert_after={True: [b"<w:p/>"]})


@pytest.mark.parametrize(
    "fragment",
    [
        b"<w:p/></w:body><w:body><w:p/>",  # would close the body and open another
        b"<w:p/><w:p/>",
        b"<w:r/>",
        b"text",
        b"<w:p/>tail",
        b" <w:p/>",
        b"<!-- c --><w:p/>",
        b'<x:p xmlns:x="urn:other"/>',
        b"<w:p>",
        b"<w:p>&nbsp;</w:p>",
        b"<zz:p/>",
        b'<?xml version="1.0"?><w:p/>',
        b"<w:p>\xff</w:p>",
    ],
)
def test_replacement_must_be_exactly_one_paragraph(fragment: bytes) -> None:
    data = _ROOT + b"><w:body><w:p/><w:p/></w:body></w:document>"
    spans = scan(data)
    for replacements, inserts in (
        ({0: fragment}, None),
        (None, {0: [fragment]}),
        (None, {-1: [fragment]}),
    ):
        with pytest.raises(DocControlError) as exc:
            spans.splice(replacements, insert_after=inserts)
        assert exc.value.code is ErrorCode.SCHEMA_INVALID, fragment


def test_parse_backstop_still_refuses_a_broken_result(monkeypatch: pytest.MonkeyPatch) -> None:
    import doc_control.ooxml.splice as mod

    data = _ROOT + b"><w:body><w:p/></w:body></w:document>"
    spans = scan(data)
    monkeypatch.setattr(mod, "_validate_fragment", lambda *a, **k: None)
    with pytest.raises(DocControlError) as exc:
        spans.splice({0: b"<w:p>"})
    assert exc.value.code is ErrorCode.INTERNAL
