"""The markup tracked edits write, the refusals, ids, authorship and safety around the write."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import doc_control.ops.tracked as tracked
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import Paragraph, build_index
from doc_control.ooxml.revisions import ParagraphEditor, format_author, format_date, used_ids
from doc_control.ooxml.splice import scan
from doc_control.ooxml.zipio import Package
from doc_control.ops.tracked import (
    OPERATION_SCHEMAS,
    DeleteParagraphs,
    Edit,
    InsertParagraph,
    MoveParagraphs,
    ReplaceText,
    TrackedResult,
    apply_edits,
    boundary_ok,
    edit_from_dict,
)
from doc_control.schema import check_schema
from tests.ops import revsim

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
STAMP = 'w:author="Claude" w:date="2026-10-12T12:00:00Z"'
W = f"{{{revsim.W_NS}}}"
FOREIGN_INS = '<w:ins w:id="5" w:author="Ana" w:date="2026-01-01T00:00:00Z">'


def run(text: str, rpr: str = "") -> str:
    props = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
    return f"<w:r>{props}<w:t>{text}</w:t></w:r>"


def para(*content: str, ppr: str = "") -> str:
    return f"<w:p>{'<w:pPr>' + ppr + '</w:pPr>' if ppr else ''}{''.join(content)}</w:p>"


def paragraphs(path: Path) -> tuple[Paragraph, ...]:
    return build_index(revsim.document_xml(path.read_bytes())).paragraphs


def at(path: Path, number: int, start: int | None = None, end: int | None = None) -> str:
    address = str(paragraphs(path)[number - 1].address)
    return address if start is None else f"{address}:{start}-{end}"


def body(path: Path) -> str:
    """The body markup after an edit, with the (fixed) author and date left out."""
    xml = revsim.document_xml(path.read_bytes()).decode()
    inner = xml[xml.index("<w:body>") + len("<w:body>") : xml.index("</w:body>")]
    return inner.replace(f" {STAMP}", "")


def make(tmp_path: Path, *blocks: str, extra: dict[str, bytes] | None = None) -> Path:
    return revsim.make_doc(tmp_path / "doc.docx", "".join(blocks), extra)


def edit(path: Path, *edits: Edit, **kwargs: Any) -> TrackedResult:
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("env", {})
    return apply_edits(path, edits, **kwargs)


def refused(path: Path, *edits: Edit, **kwargs: Any) -> DocControlError:
    """Apply expecting a refusal, and check the file was not touched."""
    before = path.read_bytes()
    with pytest.raises(DocControlError) as caught:
        edit(path, *edits, **kwargs)
    assert path.read_bytes() == before
    return caught.value


# -- text.replace: the markup -------------------------------------------------------------------


def test_replace_inside_a_run_splits_it_and_keeps_formatting(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("Hello brave world", "<w:b/>")))
    edit(path, ReplaceText(at(path, 1, 6, 11), "new"))
    assert body(path) == para(
        '<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Hello </w:t></w:r>',
        '<w:del w:id="1"><w:r><w:rPr><w:b/></w:rPr><w:delText>brave</w:delText></w:r></w:del>',
        '<w:ins w:id="2"><w:r><w:rPr><w:b/></w:rPr><w:t>new</w:t></w:r></w:ins>',
        '<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve"> world</w:t></w:r>',
    )


def test_replace_across_runs_copies_the_formatting_of_the_first_run(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("Hello "), run("bold", "<w:b/>"), run(" end", "<w:i/>")))
    edit(path, ReplaceText(at(path, 1, 3, 12), "X Y"))
    assert body(path) == para(
        "<w:r><w:t>Hel</w:t></w:r>",
        '<w:del w:id="1"><w:r><w:delText xml:space="preserve">lo </w:delText></w:r>',
        "<w:r><w:rPr><w:b/></w:rPr><w:delText>bold</w:delText></w:r>",
        '<w:r><w:rPr><w:i/></w:rPr><w:delText xml:space="preserve"> e</w:delText></w:r></w:del>',
        '<w:ins w:id="2"><w:r><w:t>X Y</w:t></w:r></w:ins>',
        "<w:r><w:rPr><w:i/></w:rPr><w:t>nd</w:t></w:r>",
    )


def test_whole_run_replacement_and_pure_deletion(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("one"), run("two")), para(run("keep")))
    edit(path, ReplaceText(at(path, 1), ""))
    assert body(path).startswith(
        '<w:p><w:del w:id="1"><w:r><w:delText>one</w:delText></w:r>'
        "<w:r><w:delText>two</w:delText></w:r></w:del></w:p>"
    )
    assert [p.text for p in paragraphs(path)] == ["", "keep"]


def test_insertion_points_take_the_formatting_next_to_them(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("abc", "<w:b/>"), run("def")))
    edit(
        path,
        ReplaceText(at(path, 1, 3, 3), "|"),
        ReplaceText(at(path, 1, 6, 6), "END"),
        ReplaceText(at(path, 1, 0, 0), "START"),
    )
    assert body(path) == para(
        '<w:ins w:id="3"><w:r><w:rPr><w:b/></w:rPr><w:t>START</w:t></w:r></w:ins>',
        "<w:r><w:rPr><w:b/></w:rPr><w:t>abc</w:t></w:r>",
        '<w:ins w:id="2"><w:r><w:t>|</w:t></w:r></w:ins>',
        "<w:r><w:t>def</w:t></w:r>",
        '<w:ins w:id="1"><w:r><w:t>END</w:t></w:r></w:ins>',
    )
    assert paragraphs(path)[0].text == "STARTabc|defEND"


def test_insertion_into_an_empty_paragraph_uses_its_mark_formatting(tmp_path: Path) -> None:
    path = make(tmp_path, para(ppr="<w:rPr><w:i/></w:rPr>"), para())
    edit(path, ReplaceText(at(path, 1, 0, 0), "hi"))
    assert body(path).startswith(
        '<w:p><w:pPr><w:rPr><w:i/></w:rPr></w:pPr><w:ins w:id="1">'
        "<w:r><w:rPr><w:i/></w:rPr><w:t>hi</w:t></w:r></w:ins></w:p>"
    )


def test_tab_and_newline_in_new_text_become_tab_and_break(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("abc")))
    edit(path, ReplaceText(at(path, 1, 1, 2), "x\ty\nz"))
    assert "<w:t>x</w:t><w:tab/><w:t>y</w:t><w:br/><w:t>z</w:t>" in body(path)
    assert paragraphs(path)[0].text == "ax\ty\nzc"


def test_spaces_at_the_edges_of_pieces_are_preserved(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("a b c")))
    edit(path, ReplaceText(at(path, 1, 2, 3), " "))
    out = body(path)
    assert '<w:t xml:space="preserve">a </w:t>' in out
    assert '<w:t xml:space="preserve"> </w:t>' in out
    assert '<w:t xml:space="preserve"> c</w:t>' in out
    assert paragraphs(path)[0].text == "a   c"


def test_markers_between_the_ends_of_a_range_stay_in_place(tmp_path: Path) -> None:
    mark = '<w:bookmarkStart w:id="3" w:name="x"/><w:commentRangeStart w:id="4"/>'
    path = make(tmp_path, para(run("ab"), mark, run("cd"), '<w:bookmarkEnd w:id="3"/>'))
    edit(path, ReplaceText(at(path, 1, 1, 3), "-"))
    assert body(path) == para(
        "<w:r><w:t>a</w:t></w:r>",
        '<w:del w:id="5"><w:r><w:delText>b</w:delText></w:r></w:del>',
        mark,
        '<w:del w:id="6"><w:r><w:delText>c</w:delText></w:r></w:del>',
        '<w:ins w:id="7"><w:r><w:t>-</w:t></w:r></w:ins>',
        "<w:r><w:t>d</w:t></w:r>",
        '<w:bookmarkEnd w:id="3"/>',
    )


def test_split_run_gets_fresh_ids_for_a_tracked_format_change(tmp_path: Path) -> None:
    change = (
        '<w:rPrChange w:id="9" w:author="Ana" w:date="2026-01-01T00:00:00Z"><w:rPr/></w:rPrChange>'
    )
    path = make(tmp_path, para(run("abcdef", f"<w:b/>{change}")))
    edit(path, ReplaceText(at(path, 1, 2, 4), "X"))
    root = ET.fromstring(revsim.document_xml(path.read_bytes()))
    changes = [e.get(W + "id") for e in root.iter(W + "rPrChange")]
    assert len(changes) == 3 and changes[0] == "9" and len(set(changes)) == 3
    ids = [e.get(W + "id") for e in root.iter() if e.get(W + "id") is not None]
    assert len(ids) == len(set(ids))
    inserted = next(root.iter(W + "ins"))
    assert inserted.find(f".//{W}rPrChange") is None  # new text has no old format to record


# -- existing revisions -------------------------------------------------------------------------


def foreign_doc(tmp_path: Path) -> Path:
    one = run("one two three")
    return make(tmp_path, para(run("A "), f"{FOREIGN_INS}{one}</w:ins>", run(" Z")))


def test_deleting_inside_someone_elses_insertion_nests_the_deletion(tmp_path: Path) -> None:
    path = foreign_doc(tmp_path)
    edit(path, ReplaceText(at(path, 1, 6, 9), "2"))
    assert body(path) == para(
        "<w:r><w:t>A </w:t></w:r>",
        f"{FOREIGN_INS.replace(' w:author', ' w:author')}"
        '<w:r><w:t xml:space="preserve">one </w:t></w:r>'
        '<w:del w:id="6"><w:r><w:delText>two</w:delText></w:r></w:del></w:ins>',
        '<w:ins w:id="7"><w:r><w:t>2</w:t></w:r></w:ins>',
        '<w:ins w:id="8" w:author="Ana" w:date="2026-01-01T00:00:00Z">'
        '<w:r><w:t xml:space="preserve"> three</w:t></w:r></w:ins>',
        "<w:r><w:t> Z</w:t></w:r>",
    )
    assert paragraphs(path)[0].text == "A one 2 three Z"


def test_inserting_inside_someone_elses_insertion_splits_it(tmp_path: Path) -> None:
    path = foreign_doc(tmp_path)
    edit(path, ReplaceText(at(path, 1, 6, 6), "++"))
    out = body(path)
    assert out.count('w:author="Ana"') == 2  # both halves keep the original author
    assert '<w:ins w:id="6"><w:r><w:t>++</w:t></w:r></w:ins>' in out
    assert paragraphs(path)[0].text == "A one ++two three Z"
    ids = re.findall(r'<w:ins w:id="(\d+)"', out)
    assert len(ids) == len(set(ids)) == 3


def test_replacing_an_entire_foreign_insertion_leaves_only_a_nested_deletion(
    tmp_path: Path,
) -> None:
    path = foreign_doc(tmp_path)
    edit(path, ReplaceText(at(path, 1, 2, 15), "all"))
    assert (
        f"{FOREIGN_INS}"
        '<w:del w:id="6"><w:r><w:delText>one two three</w:delText></w:r></w:del></w:ins>'
        '<w:ins w:id="7"><w:r><w:t>all</w:t></w:r></w:ins>'
    ) in body(path)


# -- what is refused ----------------------------------------------------------------------------

LINK = f'<w:hyperlink w:anchor="x">{run("link")}</w:hyperlink>'
FIELD = (
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    "<w:r><w:instrText> PAGE </w:instrText></w:r>"
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    f'{run("7")}<w:r><w:fldChar w:fldCharType="end"/></w:r>'
)
FIELD2 = FIELD.replace(run("7"), run("77"))
SIMPLE_FIELD = f'<w:fldSimple w:instr=" PAGE ">{run("7")}</w:fldSimple>'
SMART = f"<w:smartTag>{run('tag')}</w:smartTag>"
DRAWING = "<w:r><w:drawing/></w:r>"
FOOTNOTE = '<w:r><w:footnoteReference w:id="1"/></w:r>'
PAGE_BREAK = '<w:r><w:br w:type="page"/></w:r>'


@pytest.mark.parametrize(
    ("content", "start", "end"),
    [
        pytest.param((run("see "), LINK, run(" end")), 2, 6, id="inside-a-link"),
        pytest.param((run("see "), LINK, run(" end")), 2, 10, id="across-a-link"),
        pytest.param((run("p "), FIELD, run(" q")), 1, 4, id="across-a-field-result"),
        pytest.param((run("p "), FIELD, run(" q")), 2, 3, id="inside-a-field-result"),
        pytest.param((run("p "), SIMPLE_FIELD, run(" q")), 2, 3, id="simple-field"),
        pytest.param((run("p "), SMART, run(" q")), 1, 4, id="smart-tag"),
        pytest.param((run("ab"), DRAWING, run("cd")), 1, 3, id="drawing-between"),
        pytest.param((run("ab"), FOOTNOTE, run("cd")), 1, 3, id="footnote-reference-between"),
        pytest.param((run("ab"), PAGE_BREAK, run("cd")), 1, 3, id="page-break-between"),
        pytest.param(
            (run("ab"), "<w:r><w:t>c</w:t><w:drawing/></w:r>", run("d")), 1, 3, id="drawing-in-run"
        ),
        pytest.param((run("p "), FIELD2, run(" q")), 3, 3, id="insertion-inside-a-field-result"),
        pytest.param(
            (run("p "), LINK, FIELD),
            6,
            6,
            id="insertion-between-a-link-and-a-field",
        ),
    ],
)
def test_ranges_over_unsupported_structure_are_refused(
    tmp_path: Path, content: tuple[str, ...], start: int, end: int
) -> None:
    path = make(tmp_path, para(*content))
    err = refused(path, ReplaceText(at(path, 1, start, end), "X"))
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT


def test_text_next_to_a_link_or_field_can_be_edited(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("see "), LINK, run(" end")))
    edit(path, ReplaceText(at(path, 1, 0, 3), "SEE"), ReplaceText(at(path, 1, 8, 12), "!"))
    assert LINK in body(path)
    assert paragraphs(path)[0].text == "SEE link!"
    path = make(tmp_path, para(run("p "), FIELD, run(" q")))
    edit(path, ReplaceText(at(path, 1, 3, 5), "Q"))
    assert FIELD in body(path)
    assert paragraphs(path)[0].text == "p 7Q"


def test_insertion_next_to_a_link_goes_outside_it(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("see "), LINK, run(" end")))
    edit(path, ReplaceText(at(path, 1, 8, 8), "X"))
    assert f'{LINK}<w:ins w:id="1">' in body(path)


# -- offsets and combining marks ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "offset", "ok"),
    [
        ("cafe\u0301", 4, False),  # between e and the combining acute accent
        ("cafe\u0301", 3, True),
        ("cafe\u0301", 5, True),
        ("a\u0301\u0302b", 2, False),  # between two combining marks
        ("\u0915\u093f", 1, False),  # Devanagari vowel sign (Mc)
        ("a\u200db", 1, False),  # before a zero width joiner
        ("a\u200db", 2, False),  # after it
        ("\U0001f468\u200d\U0001f469", 1, False),  # inside an emoji sequence
        ("\U0001f44d\U0001f3fd", 1, False),  # before the skin tone modifier
        ("\U0001f1ea\U0001f1f8\U0001f1eb\U0001f1f7", 1, False),  # inside the first flag
        ("\U0001f1ea\U0001f1f8\U0001f1eb\U0001f1f7", 2, True),  # between two flags
        ("\U0001f1ea\U0001f1f8\U0001f1eb\U0001f1f7", 3, False),
        ("\U0001f3f4\U000e0067\U000e0062", 1, False),  # tag characters of a flag sequence
        ("plain", 0, True),
        ("plain", 5, True),
        ("plain", 2, True),
        ("", 0, True),
    ],
)
def test_boundary_ok(text: str, offset: int, ok: bool) -> None:
    assert boundary_ok(text, offset) is ok


@pytest.mark.parametrize(("start", "end"), [(4, 6), (0, 4), (4, 4), (3, 4), (2, 4)])
def test_ranges_that_split_a_combining_mark_are_address_invalid(
    tmp_path: Path, start: int, end: int
) -> None:
    path = make(tmp_path, para(run("cafe\u0301 au lait")))
    err = refused(path, ReplaceText(at(path, 1, start, end), "X"))
    assert err.code is ErrorCode.ADDRESS_INVALID


def test_ranges_that_keep_the_mark_with_its_base_are_fine(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("cafe\u0301 au lait")))
    edit(path, ReplaceText(at(path, 1, 3, 5), "e"))
    assert paragraphs(path)[0].text == "cafe au lait"


# -- ranges across paragraphs -------------------------------------------------------------------


def test_cross_paragraph_replace_joins_on_accept_and_restores_on_reject(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        para(run("first para")),
        para(run("middle")),
        para(run("last paragraph")),
        para(run("tail")),
    )
    first, last = at(path, 1), at(path, 3)
    original = revsim.document_xml(path.read_bytes())
    edit(path, ReplaceText(f"{first}:6..{last}:5", "NEW"))
    assert body(path) == "".join(
        [
            para(
                '<w:r><w:t xml:space="preserve">first </w:t></w:r>',
                '<w:del w:id="1"><w:r><w:delText>para</w:delText></w:r></w:del>',
                '<w:ins w:id="2"><w:r><w:t>NEW</w:t></w:r></w:ins>',
                ppr='<w:rPr><w:del w:id="3"/></w:rPr>',
            ),
            para(
                '<w:del w:id="4"><w:r><w:delText>middle</w:delText></w:r></w:del>',
                ppr='<w:rPr><w:del w:id="5"/></w:rPr>',
            ),
            para(
                '<w:del w:id="6"><w:r><w:delText xml:space="preserve">last </w:delText>'
                "</w:r></w:del>",
                "<w:r><w:t>paragraph</w:t></w:r>",
            ),
            para(run("tail")),
        ]
    )
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["first NEWparagraph", "tail"]
    assert revsim.resolved(new, "Claude", "reject") == revsim.resolved(original, None, "current")


def test_cross_paragraph_range_over_a_table_is_refused(tmp_path: Path) -> None:
    table = f"<w:tbl><w:tr><w:tc>{para(run('cell'))}</w:tc></w:tr></w:tbl>"
    path = make(tmp_path, para(run("before")), table, para(run("after")))
    err = refused(path, ReplaceText(f"{at(path, 1)}:2..{at(path, 3)}:2", "X"))
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT


def test_cross_paragraph_range_over_a_section_break_is_refused(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        para(run("one"), ppr='<w:sectPr><w:pgSz w:w="11906"/></w:sectPr>'),
        para(run("two")),
    )
    err = refused(path, ReplaceText(f"{at(path, 1)}:1..{at(path, 2)}:1", "X"))
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT


# -- para.insert --------------------------------------------------------------------------------


def test_insert_after_takes_the_properties_of_the_reference_paragraph(tmp_path: Path) -> None:
    ppr = '<w:pStyle w:val="H1"/><w:sectPr/><w:rPr><w:ins w:id="7" w:author="Z"/><w:b/></w:rPr>'
    path = make(tmp_path, para(run("one"), ppr=ppr), para(run("two")))
    edit(path, InsertParagraph("new", after=at(path, 1)))
    assert body(path) == "".join(
        [
            para(run("one"), ppr=ppr),
            para(
                '<w:ins w:id="9"><w:r><w:rPr><w:b/></w:rPr><w:t>new</w:t></w:r></w:ins>',
                ppr='<w:pStyle w:val="H1"/><w:rPr><w:ins w:id="8"/><w:b/></w:rPr>',
            ),
            para(run("two")),
        ]
    )  # no section break, no copy of the other author's mark revision
    assert [p.text for p in paragraphs(path)] == ["one", "new", "two"]


def test_insert_before_and_after_place_the_paragraph_and_report_moved_addresses(
    tmp_path: Path,
) -> None:
    path = make(tmp_path, para(run("a")), para(run("b")), para(run("c")))
    old = [str(p.address) for p in paragraphs(path)]
    result = edit(path, InsertParagraph("x", before=old[0]), InsertParagraph("y", after=old[1]))
    assert [p.text for p in paragraphs(path)] == ["x", "a", "b", "y", "c"]
    now = [str(p.address) for p in paragraphs(path)]
    assert result.address_map == {old[0]: now[1], old[1]: now[2], old[2]: now[4]}


def test_insert_after_the_last_paragraph_uses_the_form_word_writes(tmp_path: Path) -> None:
    center = '<w:jc w:val="center"/>'
    path = make(tmp_path, para(run("one")), para(run("two"), ppr=center))
    original = revsim.document_xml(path.read_bytes())
    edit(path, InsertParagraph("end", after=at(path, 2)))
    assert body(path) == "".join(
        [
            para(run("one")),
            para(run("two"), ppr=f'{center}<w:rPr><w:ins w:id="1"/></w:rPr>'),
            para('<w:ins w:id="2"><w:r><w:t>end</w:t></w:r></w:ins>', ppr=center),
        ]
    )
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["one", "two", "end"]
    assert revsim.resolved(new, "Claude", "reject") == ["one", "two"]
    assert revsim.resolved(original, None, "current") == ["one", "two"]


def test_insert_after_a_cell_paragraph_stays_in_the_cell(tmp_path: Path) -> None:
    table = f"<w:tbl><w:tr><w:tc>{para(run('cell'))}</w:tc></w:tr></w:tbl>"
    path = make(tmp_path, para(run("a")), table, para(run("z")))
    edit(path, InsertParagraph("in cell", after=at(path, 2)))
    out = body(path)
    assert out.index("in cell") < out.index("</w:tc>")
    assert [p.text for p in paragraphs(path)] == ["a", "cell", "in cell", "z"]


def test_insert_with_empty_text_adds_an_empty_inserted_paragraph(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("a")), para(run("b")))
    edit(path, InsertParagraph("", before=at(path, 2)))
    assert '<w:p><w:pPr><w:rPr><w:ins w:id="1"/></w:rPr></w:pPr></w:p>' in body(path)


def test_insert_after_a_section_ending_paragraph_that_is_last_is_refused(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("only"), ppr="<w:sectPr/>"))
    assert refused(path, InsertParagraph("x", after=at(path, 1))).code is (
        ErrorCode.UNSUPPORTED_CONTENT
    )


# -- para.delete --------------------------------------------------------------------------------


def test_delete_a_run_of_paragraphs_tracks_content_and_marks(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("one")), para(run("two")), para(run("three")))
    first, second = at(path, 1), at(path, 2)
    edit(path, DeleteParagraphs(f"{first}..{second}"))
    assert body(path) == "".join(
        [
            para(
                '<w:del w:id="1"><w:r><w:delText>one</w:delText></w:r></w:del>',
                ppr='<w:rPr><w:del w:id="2"/></w:rPr>',
            ),
            para(
                '<w:del w:id="3"><w:r><w:delText>two</w:delText></w:r></w:del>',
                ppr='<w:rPr><w:del w:id="4"/></w:rPr>',
            ),
            para(run("three")),
        ]
    )
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["three"]


def test_delete_keeps_run_formatting_and_inserted_mark_order(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        para(run("x", "<w:i/>"), ppr='<w:rPr><w:ins w:id="8" w:author="Ana"/></w:rPr>'),
        para(run("y")),
    )
    edit(path, DeleteParagraphs(at(path, 1)))
    assert body(path).startswith(
        '<w:p><w:pPr><w:rPr><w:ins w:id="8" w:author="Ana"/><w:del w:id="10"/></w:rPr></w:pPr>'
        '<w:del w:id="9"><w:r><w:rPr><w:i/></w:rPr><w:delText>x</w:delText></w:r></w:del></w:p>'
    )


def test_delete_of_an_already_deleted_paragraph_adds_no_second_mark(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        para(
            '<w:del w:id="3" w:author="Ana"><w:r><w:delText>gone</w:delText></w:r></w:del>',
            ppr='<w:rPr><w:del w:id="4" w:author="Ana"/></w:rPr>',
        ),
        para(run("next")),
    )
    edit(path, DeleteParagraphs(at(path, 1)))
    assert body(path).count('<w:del w:id="4"') == 1
    assert body(path).count("<w:del ") == 2  # nothing new was needed


@pytest.mark.parametrize(
    "blocks",
    [
        pytest.param((para(run("a")), para(run("last"))), id="last-in-body"),
        pytest.param(
            (para(run("a")), f"<w:tbl><w:tr><w:tc>{para(run('x'))}</w:tc></w:tr></w:tbl>"),
            id="last-in-cell",
        ),
    ],
)
def test_the_last_paragraph_of_a_container_cannot_be_deleted(
    tmp_path: Path, blocks: tuple[str, ...]
) -> None:
    path = make(tmp_path, *blocks)
    number = len(paragraphs(path))
    assert refused(path, DeleteParagraphs(at(path, number))).code is ErrorCode.UNSUPPORTED_CONTENT


def test_delete_run_across_a_table_is_refused(tmp_path: Path) -> None:
    table = f"<w:tbl><w:tr><w:tc>{para(run('cell'))}</w:tc></w:tr></w:tbl>"
    path = make(tmp_path, para(run("a")), table, para(run("b")), para(run("c")))
    assert refused(path, DeleteParagraphs(f"{at(path, 1)}..{at(path, 3)}")).code is (
        ErrorCode.UNSUPPORTED_CONTENT
    )


def test_delete_paragraph_with_a_field_or_link_is_refused(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("see "), LINK), para(run("next")))
    assert refused(path, DeleteParagraphs(at(path, 1))).code is ErrorCode.UNSUPPORTED_CONTENT


def test_delete_paragraph_ending_a_section_is_refused(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("a"), ppr="<w:sectPr/>"), para(run("b")))
    assert refused(path, DeleteParagraphs(at(path, 1))).code is ErrorCode.UNSUPPORTED_CONTENT


# -- para.move ----------------------------------------------------------------------------------


def test_move_is_a_tracked_deletion_plus_a_tracked_insertion(tmp_path: Path) -> None:
    center = '<w:jc w:val="center"/>'
    path = make(
        tmp_path,
        para(run("one")),
        para(run("two", "<w:b/>"), ppr=center),
        para(run("three")),
        para(run("four")),
    )
    original = revsim.document_xml(path.read_bytes())
    result = edit(path, MoveParagraphs(at(path, 2), after=at(path, 3)))
    assert body(path) == "".join(
        [
            para(run("one")),
            para(
                '<w:del w:id="1"><w:r><w:rPr><w:b/></w:rPr><w:delText>two</w:delText>'
                "</w:r></w:del>",
                ppr=f'{center}<w:rPr><w:del w:id="2"/></w:rPr>',
            ),
            para(run("three")),
            para(
                '<w:ins w:id="4"><w:r><w:rPr><w:b/></w:rPr><w:t>two</w:t></w:r></w:ins>',
                ppr=f'{center}<w:rPr><w:ins w:id="3"/></w:rPr>',
            ),
            para(run("four")),
        ]
    )
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["one", "three", "two", "four"]
    assert revsim.resolved(new, "Claude", "reject") == ["one", "two", "three", "four"]
    assert revsim.resolved(original, None, "current") == ["one", "two", "three", "four"]
    old = [str(p.address) for p in build_index(original).paragraphs]
    now = [str(p.address) for p in paragraphs(path)]
    # "two" is now its inserted copy, and "four" moved down a place
    assert result.address_map == {old[1]: now[3], old[3]: now[4]}
    assert result.to_dict()["footprint"] == {"word/document.xml": ["p2", "p4"]}


def test_move_a_run_up_before_another_paragraph(tmp_path: Path) -> None:
    path = make(tmp_path, *(para(run(w)) for w in ("a", "b", "c", "d", "e", "f")))
    edit(path, MoveParagraphs(f"{at(path, 4)}..{at(path, 5)}", before=at(path, 2)))
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["a", "d", "e", "b", "c", "f"]
    assert revsim.resolved(new, "Claude", "reject") == ["a", "b", "c", "d", "e", "f"]


@pytest.mark.parametrize(
    ("run_at", "where", "target"),
    [
        ((2, 3), "after", 2),
        ((2, 3), "after", 3),
        ((2, 3), "after", 1),  # right where it already is
        ((2, 3), "before", 2),
        ((2, 3), "before", 4),
    ],
)
def test_move_to_inside_or_next_to_the_moved_run_is_refused(
    tmp_path: Path, run_at: tuple[int, int], where: str, target: int
) -> None:
    path = make(tmp_path, *(para(run(w)) for w in ("a", "b", "c", "d", "e")))
    run_text = f"{at(path, run_at[0])}..{at(path, run_at[1])}"
    move = (
        MoveParagraphs(run_text, after=at(path, target))
        if where == "after"
        else MoveParagraphs(run_text, before=at(path, target))
    )
    assert refused(path, move).code is ErrorCode.ADDRESS_INVALID


def test_move_after_the_last_paragraph_is_refused(tmp_path: Path) -> None:
    path = make(tmp_path, *(para(run(w)) for w in ("a", "b", "c")))
    err = refused(path, MoveParagraphs(at(path, 1), after=at(path, 3)))
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT


HIDDEN = "<w:r><w:rPr><w:vanish/></w:rPr><w:t>HID</w:t></w:r>"


def hidden_doc(tmp_path: Path) -> Path:
    return make(tmp_path, para(run("a"), HIDDEN), para(run("c")), para(run("d")))


def test_deleting_a_paragraph_deletes_its_hidden_runs_too(tmp_path: Path) -> None:
    path = hidden_doc(tmp_path)
    original = revsim.document_xml(path.read_bytes())
    edit(path, DeleteParagraphs(at(path, 1)))
    new = revsim.document_xml(path.read_bytes())
    assert "<w:delText>HID</w:delText>" in body(path)
    assert revsim.resolved(new, "Claude", "accept") == ["c", "d"]
    assert revsim.resolved(new, "Claude", "reject") == revsim.resolved(original, None, "current")


def test_moving_a_paragraph_does_not_duplicate_its_hidden_runs(tmp_path: Path) -> None:
    path = hidden_doc(tmp_path)
    original = revsim.document_xml(path.read_bytes())
    edit(path, MoveParagraphs(at(path, 1), before=at(path, 3)))
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["c", "aHID", "d"]
    assert revsim.resolved(new, "Claude", "reject") == revsim.resolved(original, None, "current")


def test_middle_paragraphs_of_a_cross_paragraph_replace_lose_hidden_runs(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("first")), para(run("m"), HIDDEN), para(run("last")))
    edit(path, ReplaceText(f"{at(path, 1)}:2..{at(path, 3)}:2", "-"))
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["fi-st"]


def test_hidden_comment_reference_blocks_delete_and_move(tmp_path: Path) -> None:
    ref = '<w:r><w:rPr><w:vanish/></w:rPr><w:commentReference w:id="4"/></w:r>'
    path = make(tmp_path, para(run("a"), ref), para(run("c")), para(run("d")))
    assert refused(path, DeleteParagraphs(at(path, 1))).code is ErrorCode.UNSUPPORTED_CONTENT
    err = refused(path, MoveParagraphs(at(path, 1), before=at(path, 3)))
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT


@pytest.mark.parametrize(
    "marker",
    [
        '<w:commentRangeStart w:id="4"/>',
        '<w:r><w:commentReference w:id="4"/></w:r>',
    ],
)
def test_move_refuses_paragraphs_with_anchors(tmp_path: Path, marker: str) -> None:
    path = make(tmp_path, para(run("mov"), marker), para(run("b")), para(run("c")))
    err = refused(path, MoveParagraphs(at(path, 1), after=at(path, 2)))
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT


def bookmarks(path: Path) -> list[tuple[str, str, str]]:
    """``(tag, id, name)`` of every bookmark marker in the document, in order."""
    root = ET.fromstring(revsim.document_xml(path.read_bytes()))
    return [
        (e.tag.removeprefix(W), e.get(W + "id", ""), e.get(W + "name", ""))
        for e in root.iter()
        if e.tag in (W + "bookmarkStart", W + "bookmarkEnd")
    ]


def assert_balanced(markers: list[tuple[str, str, str]]) -> None:
    starts = [(i, n) for tag, i, n in markers if tag == "bookmarkStart"]
    ends = [i for tag, i, _ in markers if tag == "bookmarkEnd"]
    assert sorted(i for i, _ in starts) == sorted(ends)
    assert len({i for i, _ in starts}) == len(starts)
    assert len({n for _, n in starts}) == len(starts)


def test_move_carries_a_heading_bookmark_with_its_text(tmp_path: Path) -> None:
    toc = '<w:bookmarkStart w:id="3" w:name="_Toc123"/>'
    path = make(
        tmp_path,
        para(run("one")),
        para(toc, run("Head"), '<w:bookmarkEnd w:id="3"/>'),
        para(run("three")),
        para(run("four")),
    )
    original = revsim.document_xml(path.read_bytes())
    edit(path, MoveParagraphs(at(path, 2), after=at(path, 3)))
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["one", "three", "Head", "four"]
    assert revsim.resolved(new, "Claude", "reject") == revsim.resolved(original, None, "current")
    marks = bookmarks(path)
    assert_balanced(marks)
    assert [(t, n) for t, _, n in marks if n] == [("bookmarkStart", "_Toc123")]
    assert all(i != "3" for _, i, _ in marks)  # fresh id
    paras = body(path).split("</w:p>")
    assert "bookmark" not in paras[1]  # the deleted original lost its markers
    assert "<w:ins " in paras[3] and paras[3].index("<w:ins ") < paras[3].index("<w:bookmarkStart")


def test_move_keeps_relative_positions_and_zero_width_bookmarks(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        para(
            '<w:bookmarkStart w:id="1" w:name="a"/><w:bookmarkEnd w:id="1"/>',
            run("ab"),
            '<w:bookmarkStart w:id="2" w:name="b"/>',
            run("cd"),
            '<w:bookmarkEnd w:id="2"/>',
        ),
        para(run("x")),
        para(run("y")),
    )
    edit(path, MoveParagraphs(at(path, 1), after=at(path, 2)))
    marks = bookmarks(path)
    assert_balanced(marks)
    assert [(t, n) for t, _, n in marks if n] == [("bookmarkStart", "a"), ("bookmarkStart", "b")]
    assert [t for t, _, _ in marks] == [
        "bookmarkStart",
        "bookmarkEnd",
        "bookmarkStart",
        "bookmarkEnd",
    ]
    ins = body(path).split("</w:p>")[2]
    assert ins.index('w:name="a"') < ins.index("ab") < ins.index('w:name="b"') < ins.index("cd")
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["x", "abcd", "y"]
    assert revsim.resolved(new, "Claude", "reject") == ["abcd", "x", "y"]


def test_move_drops_goback_and_keeps_the_rest_of_the_text(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        para(run("mov"), '<w:bookmarkStart w:id="0" w:name="_GoBack"/><w:bookmarkEnd w:id="0"/>'),
        para(run("b")),
        para(run("c")),
    )
    edit(path, MoveParagraphs(at(path, 1), after=at(path, 2)))
    assert bookmarks(path) == []
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["b", "mov", "c"]
    assert revsim.resolved(new, "Claude", "reject") == ["mov", "b", "c"]


def test_move_carries_a_bookmark_spanning_the_moved_run(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        para('<w:bookmarkStart w:id="5" w:name="span"/>', run("a")),
        para(run("b"), '<w:bookmarkEnd w:id="5"/>'),
        para(run("c")),
        para(run("d")),
    )
    edit(path, MoveParagraphs(f"{at(path, 1)}..{at(path, 2)}", after=at(path, 3)))
    marks = bookmarks(path)
    assert_balanced(marks)
    assert len(marks) == 2
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["c", "a", "b", "d"]


@pytest.mark.parametrize(
    ("first", "second", "name"),
    [
        ('<w:bookmarkStart w:id="3" w:name="open"/>', "", "open"),  # ends outside
        ("", '<w:bookmarkEnd w:id="3"/>', "open"),  # starts outside
    ],
)
def test_move_refuses_a_bookmark_reaching_outside_the_range(
    tmp_path: Path, first: str, second: str, name: str
) -> None:
    # The bookmark starts in p1 and ends in p2 (or the reverse); only p1 or p2 moves.
    start = '<w:bookmarkStart w:id="3" w:name="open"/>'
    end = '<w:bookmarkEnd w:id="3"/>'
    path = make(
        tmp_path, para(run("a"), start), para(run("b"), end), para(run("c")), para(run("d"))
    )
    moved = 1 if first else 2
    err = refused(path, MoveParagraphs(at(path, moved), after=at(path, 3)))
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT
    assert err.details["bookmark"] == name


@pytest.mark.parametrize(
    "ref",
    [
        '<w:fldSimple w:instr=" REF _Toc123 \\h "><w:r><w:t>x</w:t></w:r></w:fldSimple>',
        '<w:hyperlink w:anchor="_Toc123"><w:r><w:t>x</w:t></w:r></w:hyperlink>',
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> PAGEREF _Toc123 '
        '\\h </w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ],
)
def test_move_warns_when_a_carried_bookmark_is_referenced(tmp_path: Path, ref: str) -> None:
    head = para(
        '<w:bookmarkStart w:id="3" w:name="_Toc123"/>', run("H"), '<w:bookmarkEnd w:id="3"/>'
    )
    path = make(tmp_path, head, para(run("b")), para(run("c")), para(ref))
    result = edit(path, MoveParagraphs(at(path, 1), after=at(path, 2)))
    assert len(result.warnings) == 1 and "_Toc123" in result.warnings[0]
    assert result.to_dict()["warnings"] == result.warnings


def test_move_without_references_has_no_warning(tmp_path: Path) -> None:
    head = para(
        '<w:bookmarkStart w:id="3" w:name="_Toc123"/>', run("H"), '<w:bookmarkEnd w:id="3"/>'
    )
    path = make(tmp_path, head, para(run("b")), para(run("c")))
    result = edit(path, MoveParagraphs(at(path, 1), after=at(path, 2)))
    assert result.warnings == [] and "warnings" not in result.to_dict()


def test_delete_leaves_balanced_bookmarks_and_accept_reject_texts(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        para(run("keep")),
        para(
            '<w:bookmarkStart w:id="3" w:name="_Toc9"/>', run("gone"), '<w:bookmarkEnd w:id="3"/>'
        ),
        para(run("tail")),
    )
    original = revsim.document_xml(path.read_bytes())
    edit(path, DeleteParagraphs(at(path, 2)))
    assert_balanced(bookmarks(path))
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["keep", "tail"]
    assert revsim.resolved(new, "Claude", "reject") == revsim.resolved(original, None, "current")


def test_cross_paragraph_replace_keeps_bookmarks_balanced(tmp_path: Path) -> None:
    path = make(
        tmp_path,
        para(run("first")),
        para('<w:bookmarkStart w:id="3" w:name="mid"/>', run("m"), '<w:bookmarkEnd w:id="3"/>'),
        para(run("last")),
    )
    edit(path, ReplaceText(f"{at(path, 1)}:2..{at(path, 3)}:2", "-"))
    assert_balanced(bookmarks(path))
    new = revsim.document_xml(path.read_bytes())
    assert revsim.resolved(new, "Claude", "accept") == ["fi-st"]


def test_insert_after_a_last_paragraph_whose_mark_is_someone_elses_insertion(
    tmp_path: Path,
) -> None:
    mark = '<w:rPr><w:ins w:id="8" w:author="Ana"/></w:rPr>'
    path = make(tmp_path, para(run("one")), para(run("two"), ppr=mark))
    err = refused(path, InsertParagraph("x", after=at(path, 2)))
    assert err.code is ErrorCode.UNSUPPORTED_CONTENT


# -- batches ------------------------------------------------------------------------------------


def test_two_edits_to_one_paragraph_must_not_touch(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("abcdefgh")), para(run("next")))
    ok = [ReplaceText(at(path, 1, 0, 2), "A"), ReplaceText(at(path, 1, 2, 4), "C")]
    edit(path, *ok)  # adjacent non-empty ranges are fine
    assert paragraphs(path)[0].text == "ACefgh"
    path = make(tmp_path, para(run("abcdefgh")), para(run("next")))
    for clash in (
        [ReplaceText(at(path, 1, 0, 4), "A"), ReplaceText(at(path, 1, 2, 6), "C")],
        [ReplaceText(at(path, 1, 2, 4), "A"), ReplaceText(at(path, 1, 2, 2), "C")],
        [ReplaceText(at(path, 1, 2, 4), "A"), ReplaceText(at(path, 1, 4, 4), "C")],
        [ReplaceText(at(path, 1, 3, 3), "A"), ReplaceText(at(path, 1, 3, 3), "C")],
    ):
        assert refused(path, *clash).code is ErrorCode.BATCH_CONFLICT


def test_other_edits_to_a_paragraph_that_is_deleted_or_moved_conflict(tmp_path: Path) -> None:
    path = make(tmp_path, *(para(run(w)) for w in ("a", "b", "c", "d")))
    first = at(path, 1)
    for clash in (
        [DeleteParagraphs(first), ReplaceText(at(path, 1, 0, 1), "X")],
        [ReplaceText(at(path, 1, 0, 1), "X"), DeleteParagraphs(first)],
        [DeleteParagraphs(first), MoveParagraphs(first, after=at(path, 3))],
        [InsertParagraph("x", before=at(path, 3)), InsertParagraph("y", after=at(path, 2))],
    ):
        assert refused(path, *clash).code is ErrorCode.BATCH_CONFLICT


def test_edits_are_resolved_against_the_document_as_read(tmp_path: Path) -> None:
    path = make(tmp_path, *(para(run(w)) for w in ("a", "b", "c", "d")))
    addresses = [at(path, n) for n in range(1, 5)]
    edit(
        path,
        InsertParagraph("new", before=addresses[0]),
        ReplaceText(f"{addresses[3]}:0-1", "D"),
        DeleteParagraphs(addresses[1]),
    )
    assert [p.text for p in paragraphs(path)] == ["new", "a", "", "c", "D"]


# -- ids, authorship ----------------------------------------------------------------------------


def test_new_ids_are_above_every_id_in_every_story_part(tmp_path: Path) -> None:
    comments = (
        f'<w:comments xmlns:w="{revsim.W_NS}"><w:comment w:id="41" w:author="A" w:date="d">'
        "<w:p/></w:comment></w:comments>"
    ).encode()
    header = f'<w:hdr xmlns:w="{revsim.W_NS}"><w:p><w:ins w:id="90" w:author="A"/></w:p></w:hdr>'
    path = make(
        tmp_path,
        para(
            '<w:bookmarkStart w:id="77" w:name="b"/>',
            run("hello"),
            '<w:bookmarkEnd w:id="77"/>',
            f'<w:ins w:id="12" w:author="Ana">{run("x")}</w:ins>',
        ),
        extra={"word/comments.xml": comments, "word/header1.xml": header.encode()},
    )
    pkg = Package(path)
    assert used_ids(pkg) == {12, 41, 77, 90}
    result = edit(path, ReplaceText(at(path, 1, 0, 2), "J"))
    assert result.revision_ids == [91, 92]


def test_ids_continue_after_an_unusual_maximum(tmp_path: Path) -> None:
    path = make(
        tmp_path, para(f'<w:ins w:id="2147483000" w:author="A">{run("x")}</w:ins>', run("yz"))
    )
    result = edit(path, ReplaceText(at(path, 1, 1, 2), "Y"))
    assert result.revision_ids == [2147483001, 2147483002]


def test_author_tag_and_date(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("hello")))
    when = datetime(2026, 10, 12, 14, 30, 5, 999999, tzinfo=timezone(timedelta(hours=2)))
    result = edit(path, ReplaceText(at(path, 1, 0, 1), "J"), author="Ana", tag="lit", now=when)
    assert result.author == "Ana (lit)" and result.date == "2026-10-12T12:30:05Z"
    assert (
        'w:author="Ana (lit)" w:date="2026-10-12T12:30:05Z"'
        in revsim.document_xml(path.read_bytes()).decode()
    )
    assert format_author("Claude", None) == "Claude"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", format_date())
    with pytest.raises(ValueError, match="timezone"):
        format_date(datetime(2026, 1, 1))


def test_default_author_is_claude(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("hello")))
    edit(path, ReplaceText(at(path, 1, 0, 1), "J"))
    assert STAMP in revsim.document_xml(path.read_bytes()).decode()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"author": ""}, "author"),
        ({"author": "A\nB"}, "author"),
        ({"tag": "  "}, "tag"),
        ({"tag": "x\x00"}, "tag"),
    ],
)
def test_bad_author_or_tag_is_refused(tmp_path: Path, kwargs: dict[str, str], message: str) -> None:
    path = make(tmp_path, para(run("hello")))
    err = refused(path, ReplaceText(at(path, 1, 0, 1), "J"), **kwargs)
    assert err.code is ErrorCode.SCHEMA_INVALID and message in err.message


# -- errors and safety --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["a\x00b", "a\x0bb", "a\ud800b", "a\ufffeb", "a\rb"])
def test_text_xml_cannot_hold_is_refused(tmp_path: Path, bad: str) -> None:
    path = make(tmp_path, para(run("hello")))
    err = refused(path, ReplaceText(at(path, 1, 0, 1), bad))
    assert err.code is ErrorCode.SCHEMA_INVALID


def test_no_edits_and_no_op_edits_are_refused(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("hello")))
    assert refused(path).code is ErrorCode.SCHEMA_INVALID
    assert refused(path, ReplaceText(at(path, 1, 2, 2), "")).code is ErrorCode.SCHEMA_INVALID


def test_stale_and_invalid_addresses_change_nothing(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("hello")), para(run("world")))
    first, second = at(path, 1), at(path, 2)
    assert refused(path, ReplaceText("p1#00000000:0-1", "J")).code is ErrorCode.STALE_ADDRESS
    assert refused(path, DeleteParagraphs("p9#00000000")).code is ErrorCode.STALE_ADDRESS
    assert refused(path, ReplaceText("p1#zz:0-1", "J")).code is ErrorCode.ADDRESS_INVALID
    assert refused(path, ReplaceText(f"{first}:0-99", "J")).code is ErrorCode.ADDRESS_INVALID
    assert refused(path, ReplaceText(f"{first}.s1", "J")).code is ErrorCode.ADDRESS_INVALID
    assert refused(path, DeleteParagraphs(f"{second}..{first}")).code is ErrorCode.ADDRESS_INVALID
    assert refused(path, DeleteParagraphs(f"{first}:0-1")).code is ErrorCode.ADDRESS_INVALID


def test_a_changed_file_or_a_wrong_hash_is_refused(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("hello")), para(run("world")))
    err = refused(path, ReplaceText(at(path, 1, 0, 1), "J"), expect_sha256="0" * 64)
    assert err.code is ErrorCode.DOC_CHANGED_ON_DISK
    sha = Package(path).sha256
    edit(path, ReplaceText(at(path, 1, 0, 1), "J"), expect_sha256=sha)


def test_a_word_lock_file_blocks_the_write(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("hello")), para(run("world")))
    (tmp_path / "~$doc.docx").write_bytes(b"lock")
    assert refused(path, ReplaceText(at(path, 1, 0, 1), "J")).code is ErrorCode.DOC_LOCKED
    assert not (tmp_path / ".doc-control").exists()


def test_missing_and_foreign_files(tmp_path: Path) -> None:
    with pytest.raises(DocControlError) as caught:
        apply_edits(tmp_path / "nope.docx", [DeleteParagraphs("p1#00000000")], env={})
    assert caught.value.code is ErrorCode.DOC_NOT_FOUND
    junk = tmp_path / "junk.docx"
    junk.write_bytes(b"not a zip")
    with pytest.raises(DocControlError) as caught:
        apply_edits(junk, [DeleteParagraphs("p1#00000000")], env={})
    assert caught.value.code is ErrorCode.NOT_A_DOCX


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("hello")), para(run("world")), para(run("end")))
    before = path.read_bytes()
    result = edit(
        path,
        ReplaceText(at(path, 1, 0, 1), "J"),
        InsertParagraph("new", before=at(path, 2)),
        dry_run=True,
    )
    assert path.read_bytes() == before and not (tmp_path / ".doc-control").exists()
    assert result.dry_run and result.after_sha256 is None and result.backup is None
    assert result.preview[0] == {"op": 0, "at": at(path, 1, 0, 1), "before": "h", "after": "J"}
    assert result.address_map
    assert "backup" not in result.to_dict()


def test_a_write_backs_up_and_reports_hashes(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("hello")), para(run("world")))
    before = path.read_bytes()
    result = edit(path, ReplaceText(at(path, 1, 0, 1), "J"))
    assert result.backup is not None and result.backup.read_bytes() == before
    assert result.before_sha256 == Package(result.backup).sha256
    assert result.after_sha256 == Package(path).sha256
    out = result.to_dict()
    assert out["sha256"] == result.after_sha256 and out["applied"] == 1
    assert out["footprint"] == {"word/document.xml": ["p1"]}


def test_only_the_edited_paragraphs_and_document_xml_change(tmp_path: Path) -> None:
    odd = (
        '<w:p w14:paraId="1A2B3C4D" w14:textId="77777777" w:rsidR="00AB12CD" >\n  '
        '<w:pPr><w:pStyle w:val="Normal"/></w:pPr>\n  '
        '<w:r w:rsidRPr="00112233"><w:t>odd</w:t></w:r>'
        "</w:p>\n"
    )
    path = make(
        tmp_path,
        para(run("a")),
        odd,
        para(run("edit me")),
        "<!-- note --><w:bookmarkStart w:id='1' w:name='k'/>",
        odd,
        extra={"word/styles.xml": b"<styles/>", "word/media/x.bin": bytes(range(256))},
    )
    old = Package(path)
    old_spans = scan(old.read("word/document.xml"))
    edit(path, ReplaceText(at(path, 3, 0, 4), "Edit"))
    new = Package(path)
    new_spans = scan(new.read("word/document.xml"))
    assert len(new_spans.paragraphs) == len(old_spans.paragraphs)
    for i in range(len(old_spans.paragraphs)):
        same = old_spans.paragraph_bytes(i) == new_spans.paragraph_bytes(i)
        assert same == (i != 2), i
    # everything between the paragraphs is the original bytes
    assert new.read("word/document.xml").startswith(old.read("word/document.xml")[:200])
    for name in old.names:
        if name != "word/document.xml":
            assert new.fingerprint(name) == old.fingerprint(name)


def test_a_fault_while_writing_restores_the_original(tmp_path: Path) -> None:
    path = make(tmp_path, para(run("hello")), para(run("world")))
    before = path.read_bytes()

    def fail(stage: str) -> None:
        if stage == "replace":
            raise OSError("disk full")

    with pytest.raises(DocControlError) as caught:
        edit(path, ReplaceText(at(path, 1, 0, 1), "J"), stage=fail)
    assert caught.value.code is ErrorCode.VERIFY_FAILED_RESTORED
    assert path.read_bytes() == before


def test_the_self_check_refuses_a_writer_that_loses_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make(tmp_path, para(run("hello")), para(run("world")))
    original = ParagraphEditor.replace

    def lossy(self: ParagraphEditor, start: int, end: int, text: str | None) -> None:
        original(self, start, end, text)
        for ins in list(self.p.iter(f"{W}ins")):  # drop the inserted text
            self.p.remove(ins)

    monkeypatch.setattr(ParagraphEditor, "replace", lossy)
    err = refused(path, ReplaceText(at(path, 1, 0, 1), "J"))
    assert err.code is ErrorCode.INTERNAL and "self-check" in err.message


def test_the_self_check_refuses_revisions_that_do_not_reject_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make(tmp_path, para(run("hello")), para(run("world")))
    original = ParagraphEditor.replace

    def sticky(self: ParagraphEditor, start: int, end: int, text: str | None) -> None:
        original(self, start, end, text)
        for deletion in self.p.iter(f"{W}del"):  # a deletion nobody can reject
            deletion.set(f"{W}id", "999999")

    monkeypatch.setattr(ParagraphEditor, "replace", sticky)
    err = refused(path, ReplaceText(at(path, 1, 0, 1), "J"))
    assert err.code is ErrorCode.INTERNAL and "rejecting" in err.message


# -- request objects ----------------------------------------------------------------------------


def test_edit_from_dict_builds_each_edit_and_validates() -> None:
    assert edit_from_dict({"op": "text.replace", "at": "p1#aaaaaaaa", "text": "x"}) == ReplaceText(
        "p1#aaaaaaaa", "x"
    )
    assert edit_from_dict({"op": "para.insert", "text": "x", "before": "p1#aaaaaaaa"}) == (
        InsertParagraph("x", before="p1#aaaaaaaa")
    )
    assert edit_from_dict({"op": "para.delete", "at": "p1#aaaaaaaa"}) == DeleteParagraphs(
        "p1#aaaaaaaa"
    )
    assert edit_from_dict({"op": "para.move", "at": "p1#aaaaaaaa", "after": "p5#bbbbbbbb"}) == (
        MoveParagraphs("p1#aaaaaaaa", after="p5#bbbbbbbb")
    )
    bad: list[dict[str, Any]] = [
        {"op": "nope"},
        {},
        {"op": "text.replace", "at": "p1#aaaaaaaa"},
        {"op": "text.replace", "at": "p1#aaaaaaaa", "text": 5},
        {"op": "text.replace", "at": "p1#aaaaaaaa", "text": "x", "extra": 1},
        {"op": "para.insert", "text": "x"},
        {"op": "para.insert", "text": "x", "after": "p1#aaaaaaaa", "before": "p2#bbbbbbbb"},
        {"op": "para.move", "at": "p1#aaaaaaaa"},
    ]
    for raw in bad:
        with pytest.raises(DocControlError) as caught:
            edit_from_dict(raw)
        assert caught.value.code is ErrorCode.SCHEMA_INVALID, raw


def test_operation_schemas_are_well_formed() -> None:
    assert set(OPERATION_SCHEMAS) == {"text.replace", "para.insert", "para.delete", "para.move"}
    for name, schema in OPERATION_SCHEMAS.items():
        assert check_schema(schema) == [], name
        assert schema["properties"]["op"]["enum"] == [name]


def test_public_names() -> None:
    assert set(tracked.__all__) == {
        "DeleteParagraphs",
        "Edit",
        "InsertParagraph",
        "MoveParagraphs",
        "PreparedEdits",
        "ReplaceText",
        "TrackedResult",
        "apply_edits",
        "edit_from_dict",
        "edits_as_request",
        "next_in_container",
        "prepare_edits",
    }
