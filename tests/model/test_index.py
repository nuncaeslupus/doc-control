"""The paragraph index: visible text, offsets mapped back to runs, resolution errors."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex, Paragraph, build_index
from doc_control.ooxml.splice import MC_NS, W_NS
from doc_control.ooxml.zipio import Package
from tests.corpus.generator import KINDS, generate_parts
from tests.helpers import make_docx

W = f"{{{W_NS}}}"


def _doc(*paragraphs: str) -> bytes:
    body = "".join(paragraphs)
    return f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'.encode()


def _p(*inner: str) -> str:
    return "<w:p>" + "".join(inner) + "</w:p>"


def _r(*inner: str, rpr: str = "") -> str:
    return "<w:r>" + (f"<w:rPr>{rpr}</w:rPr>" if rpr else "") + "".join(inner) + "</w:r>"


def _t(text: str) -> str:
    return f'<w:t xml:space="preserve">{text}</w:t>'


def _fld(kind: str) -> str:
    return _r(f'<w:fldChar w:fldCharType="{kind}"/>')


def _texts(*paragraphs: str) -> list[str]:
    return [p.text for p in build_index(_doc(*paragraphs)).paragraphs]


def _resolve_path(root: ET.Element, path: tuple[int, ...]) -> ET.Element:
    elem = root
    for i in path:
        elem = list(elem)[i]
    return elem


def test_plain_text_tabs_and_breaks() -> None:
    para = _p(
        _r(_t("a"), "<w:tab/>", _t("b"), "<w:br/>", _t("c")),
        _r('<w:br w:type="page"/>', '<w:br w:type="column"/>', "<w:cr/>", _t("d")),
        _r("<w:noBreakHyphen/><w:softHyphen/>", "<w:sym/>", "<w:drawing/>"),
    )
    assert _texts(para) == ["a\tb\nc\nd\N{NON-BREAKING HYPHEN}\N{SOFT HYPHEN}"]


def test_revisions_keep_inserted_and_drop_deleted_text() -> None:
    para = _p(
        _r(_t("keep ")),
        '<w:ins w:id="1"><w:r><w:t>new </w:t></w:r></w:ins>',
        '<w:del w:id="2"><w:r><w:delText>old </w:delText></w:r></w:del>',
        '<w:moveFrom w:id="3"><w:r><w:t>moved away </w:t></w:r></w:moveFrom>',
        '<w:moveTo w:id="4"><w:r><w:t>moved in</w:t></w:r></w:moveTo>',
    )
    assert _texts(para) == ["keep new moved in"]


def test_stray_del_text_outside_a_deletion_is_not_visible() -> None:
    assert _texts(_p(_r(_t("a"), "<w:delText>b</w:delText>"))) == ["a"]


@pytest.mark.parametrize(
    ("vanish", "visible"),
    [
        ("<w:vanish/>", False),
        ('<w:vanish w:val="1"/>', False),
        ('<w:vanish w:val="true"/>', False),
        ('<w:vanish w:val="0"/>', True),
        ('<w:vanish w:val="false"/>', True),
        ("<w:b/>", True),
    ],
)
def test_hidden_runs(vanish: str, visible: bool) -> None:
    para = _p(_r(_t("a")), _r(_t("secret"), rpr=vanish), _r(_t("b")))
    assert _texts(para) == ["asecretb" if visible else "ab"]


def test_paragraph_mark_formatting_does_not_hide_the_text() -> None:
    para = _p("<w:pPr><w:rPr><w:vanish/></w:rPr></w:pPr>", _r(_t("shown")))
    assert _texts(para) == ["shown"]


def test_field_instructions_are_excluded_and_results_kept() -> None:
    para = _p(
        _r(_t("page ")),
        _fld("begin"),
        _r('<w:instrText xml:space="preserve"> PAGE </w:instrText>'),
        _fld("separate"),
        _r(_t("7")),
        _fld("end"),
        _r(_t(" of ")),
        '<w:fldSimple w:instr=" NUMPAGES "><w:r><w:t>9</w:t></w:r></w:fldSimple>',
    )
    assert _texts(para) == ["page 7 of 9"]


def test_nested_fields_hide_text_inside_an_inner_instruction() -> None:
    para = _p(
        _fld("begin"),
        _r("<w:instrText> IF </w:instrText>"),
        _fld("begin"),
        _r(_t("inner instr")),
        _fld("separate"),
        _r(_t("x")),
        _fld("end"),
        _fld("separate"),
        _r(_t("result "), _t("kept")),
        _fld("end"),
    )
    assert _texts(para) == ["result kept"]


def test_field_spanning_paragraphs_keeps_its_state() -> None:
    first = _p(_r(_t("a")), _fld("begin"), _r("<w:instrText> TOC </w:instrText>"))
    second = _p(_r(_t("still instruction")), _fld("separate"), _r(_t("entry")))
    third = _p(_fld("end"), _r(_t("after")))
    assert _texts(first, second, third) == ["a", "entry", "after"]


def test_unbalanced_field_marks_are_ignored() -> None:
    assert _texts(_p(_fld("end"), _fld("separate"), _r(_t("ok")))) == ["ok"]


def test_hyperlinks_smart_tags_and_content_controls_are_transparent() -> None:
    para = _p(
        '<w:hyperlink w:anchor="x"><w:r><w:t>link </w:t></w:r></w:hyperlink>',
        "<w:smartTag><w:r><w:t>tag </w:t></w:r></w:smartTag>",
        "<w:sdt><w:sdtPr><w:rPr><w:vanish/></w:rPr></w:sdtPr>"
        "<w:sdtContent><w:r><w:t>control</w:t></w:r></w:sdtContent></w:sdt>",
    )
    assert _texts(para) == ["link tag control"]


def test_text_box_paragraphs_are_neither_numbered_nor_part_of_the_parent() -> None:
    box = "<w:r><w:pict><w:txbxContent>" + _p(_r(_t("boxed"))) + "</w:txbxContent></w:pict></w:r>"
    index = build_index(_doc(_p(_r(_t("before")), box, _r(_t(" after"))), _p(_r(_t("next")))))
    assert [p.text for p in index.paragraphs] == ["before after", "next"]
    assert [p.number for p in index.paragraphs] == [1, 2]
    assert [p.span_index for p in index.paragraphs] == [0, 2]


def test_table_cell_paragraphs_are_numbered_in_document_order() -> None:
    table = (
        "<w:tbl><w:tr><w:tc>"
        + _p(_r(_t("c1")))
        + "</w:tc><w:tc>"
        + _p(_r(_t("c2")))
        + "</w:tc></w:tr></w:tbl>"
    )
    index = build_index(_doc(_p(_r(_t("a"))), table, _p(_r(_t("z")))))
    assert [p.text for p in index.paragraphs] == ["a", "c1", "c2", "z"]


def test_empty_document_and_self_closing_paragraph() -> None:
    assert len(build_index(_doc())) == 0
    assert build_index(_doc("<w:p/>")).paragraphs[0].text == ""


def test_pieces_map_visible_offsets_back_to_source_elements() -> None:
    data = _doc(
        _p(
            _r(_t("ab")),
            '<w:del w:id="1"><w:r><w:delText>XX</w:delText></w:r></w:del>',
            "<!-- note -->",
            '<w:ins w:id="2"><w:r>' + _t("cd") + "<w:tab/></w:r></w:ins>",
            _r(_t("e"), rpr="<w:vanish/>"),
            _r(_t("f")),
        )
    )
    index = build_index(data)
    para = index.paragraphs[0]
    assert para.text == "abcd\tf"
    _assert_pieces_reproduce_text(para, index.spans.parse_paragraph(para.span_index))
    assert [(p.start, p.end) for p in para.pieces] == [(0, 2), (2, 4), (4, 5), (5, 6)]
    assert [(p.start, p.end) for p in para.pieces_in(3, 5)] == [(2, 4), (4, 5)]
    assert para.pieces_in(2, 2) == []
    assert para.pieces_in(6, 9) == []


def _assert_pieces_reproduce_text(para: Paragraph, root: ET.Element) -> None:
    out = []
    for piece in para.pieces:
        elem = _resolve_path(root, piece.path)
        assert elem.tag in {
            W + "t",
            W + "tab",
            W + "br",
            W + "cr",
            W + "noBreakHyphen",
            W + "softHyphen",
            W + "sym",
            W + "ptab",
        }
        out.append("".join(elem.itertext()) if elem.tag == W + "t" else para.text[piece.start])
        assert piece.end - piece.start == len(out[-1])
    assert "".join(out) == para.text


@pytest.mark.parametrize("kind", KINDS)
def test_corpus_pieces_reproduce_the_visible_text(kind: str) -> None:
    index = build_index(generate_parts(kind, 1)["word/document.xml"])
    assert len(index) > 0
    for para in index.paragraphs:
        _assert_pieces_reproduce_text(para, index.spans.parse_paragraph(para.span_index))


def test_corpus_fields_exclude_instruction_text() -> None:
    texts = [
        p.text
        for kind in ("revisions", "fields", "kitchen_sink")
        for p in build_index(generate_parts(kind, 2)["word/document.xml"]).paragraphs
    ]
    assert any(texts)
    assert all("DATE" not in t for t in texts), "field instruction text leaked"


def _two_paragraph_index() -> DocumentIndex:
    return build_index(_doc(_p(_r(_t("Hola mundo"))), _p(_r(_t("Adiós")))))


def test_resolve_address_returns_the_paragraph() -> None:
    index = _two_paragraph_index()
    para = index.paragraphs[1]
    assert index.resolve_address(para.address) is para
    assert index.resolve_address(str(para.address)) is para


def test_stale_fingerprint_and_missing_position_raise_stale_address() -> None:
    index = _two_paragraph_index()
    for bad in ("p1#00000000", "p3#00000000"):
        with pytest.raises(DocControlError) as err:
            index.resolve_address(bad)
        assert err.value.code is ErrorCode.STALE_ADDRESS
    with pytest.raises(DocControlError) as err:
        index.resolve_address("p1#00000000")
    assert err.value.details["current"] == str(index.paragraphs[0].address)


def test_resolve_range_fills_offsets_and_checks_bounds() -> None:
    index = _two_paragraph_index()
    a, b = (p.address for p in index.paragraphs)
    whole = index.resolve_range(str(a))
    assert (whole.start_offset, whole.end_offset) == (0, 10)
    inside = index.resolve_range(f"{a}:5-10")
    assert (inside.first, inside.last) == (index.paragraphs[0],) * 2
    across = index.resolve_range(f"{a}:5..{b}:2")
    assert (across.first.number, across.last.number) == (1, 2)
    assert (across.start_offset, across.end_offset) == (5, 2)
    sentence = index.resolve_range(f"{b}.s1")
    assert (sentence.sentence, sentence.start_offset, sentence.end_offset) == (1, 0, 5)
    with pytest.raises(DocControlError) as err:
        index.resolve_range(f"{b}.s2")
    assert err.value.code is ErrorCode.ADDRESS_INVALID
    for bad in (f"{a}:5-11", f"{a}:11-11", f"{a}:5..{b}:6"):
        with pytest.raises(DocControlError) as err:
            index.resolve_range(bad)
        assert err.value.code is ErrorCode.ADDRESS_INVALID
    with pytest.raises(DocControlError) as err:
        index.resolve_range(f"{a}:5..p2#00000000:1")
    assert err.value.code is ErrorCode.STALE_ADDRESS


def test_from_package_reads_word_document_xml(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "d.docx", "uno", "dos")
    index = DocumentIndex.from_package(Package(path))
    assert [p.text for p in index.paragraphs] == ["uno", "dos"]


def test_unterminated_field_does_not_blank_later_paragraphs() -> None:
    open_field = _p(_fld("begin"), _r(_t("x")))
    assert _texts(open_field, _p(_r(_t("later"))), _p(_r(_t("last")))) == ["", "later", "last"]


def test_symbols_and_positional_tabs_are_shown() -> None:
    para = _p(
        _r(_t("a"), '<w:sym w:font="Wingdings" w:char="F0A7"/>', "<w:ptab/>", _t("b")),
        _r('<w:sym w:char="D800"/><w:sym w:char="zz"/><w:sym/>'),
    )
    index = build_index(_doc(para))
    text = index.paragraphs[0].text
    assert text == "a\tb"
    _assert_pieces_reproduce_text(
        index.paragraphs[0], index.spans.parse_paragraph(index.paragraphs[0].span_index)
    )


def _alternate(requires: str, decl: str = "") -> str:
    return (
        f'<mc:AlternateContent xmlns:mc="{MC_NS}"{decl}>'
        f'<mc:Choice Requires="{requires}"><w:r><w:t>choice</w:t></w:r></mc:Choice>'
        "<mc:Fallback><w:r><w:t>fallback</w:t></w:r></mc:Fallback>"
        "</mc:AlternateContent>"
    )


def test_alternate_content_reads_exactly_one_branch() -> None:
    w14 = ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    other = ' xmlns:zz="urn:example:other"'
    texts = _texts(
        _p(_r(_t("a ")), _alternate("w14", w14)),  # declared on the element: not in scope
        _p(_r(_t("a ")), _alternate("zz", other)),
        _p(_r(_t("a ")), _alternate("undeclared")),
    )
    assert texts == ["a fallback", "a fallback", "a fallback"]


def test_alternate_content_pieces_map_to_the_chosen_branch() -> None:
    w14 = ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    data = (
        f'<w:document xmlns:w="{W_NS}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
        f"<w:body>{_p(_r(_t('a ')), _alternate('w14', w14))}</w:body></w:document>"
    ).encode()
    index = build_index(data)
    para = index.paragraphs[0]
    assert para.text == "a choice"
    _assert_pieces_reproduce_text(para, index.spans.parse_paragraph(para.span_index))


def test_ruby_shows_base_text_only() -> None:
    ruby = (
        "<w:ruby><w:rt><w:r><w:t>kan</w:t></w:r></w:rt>"
        "<w:rubyBase><w:r><w:t>base</w:t></w:r></w:rubyBase></w:ruby>"
    )
    index = build_index(_doc(_p(_r(_t("x "), ruby))))
    para = index.paragraphs[0]
    assert para.text == "x base"
    _assert_pieces_reproduce_text(para, index.spans.parse_paragraph(para.span_index))


def test_equations_are_not_part_of_the_visible_text() -> None:
    m = "http://schemas.openxmlformats.org/officeDocument/2006/math"
    math = f'<m:oMath xmlns:m="{m}"><m:r><m:t>x+y</m:t></m:r></m:oMath>'
    assert _texts(_p(_r(_t("a")), math, _r(_t("b")))) == ["ab"]


def test_old_address_is_stale_after_a_paragraph_is_inserted() -> None:
    before = build_index(_doc(_p(_r(_t("one"))), _p(_r(_t("two"))), _p(_r(_t("three")))))
    old = str(before.paragraphs[2].address)
    after = build_index(
        _doc(_p(_r(_t("one"))), _p(_r(_t("new"))), _p(_r(_t("two"))), _p(_r(_t("three"))))
    )
    with pytest.raises(DocControlError) as err:
        after.resolve_address(old)
    assert err.value.code is ErrorCode.STALE_ADDRESS
    assert after.resolve_address(str(after.paragraphs[3].address)).text == "three"
