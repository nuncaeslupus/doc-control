"""Structure detection: signals, stable ids, overrides, profile settings, the `segment` op."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.model.address import fingerprint
from doc_control.model.structure import (
    DetectedChapter,
    Override,
    Para,
    Profile,
    Structure,
    detect,
)
from tests.helpers import CONTENT_TYPES, RELS

BODY = "La lluvia cayó sobre el pueblo durante toda la noche y nadie salió a la calle."


def para(number: int, text: str, **layout: Any) -> Para:
    return Para(number, text, fingerprint(text), **layout)


def build(*specs: str | tuple[str, dict[str, Any]]) -> list[Para]:
    out: list[Para] = []
    for number, spec in enumerate(specs, 1):
        text, layout = (spec, {}) if isinstance(spec, str) else spec
        out.append(para(number, text, **layout))
    return out


def book(chapters: int = 6, scenes: int = 2, filler: int = 3) -> list[str]:
    """Invented text: chapters of ``scenes`` scenes split by ``* * *``."""
    texts: list[str] = []
    for c in range(1, chapters + 1):
        texts.append(f"Capítulo {c}")
        for s in range(scenes):
            if s:
                texts.append("* * *")
            texts += [f"{BODY} Texto {c}.{s}.{k}." for k in range(filler)]
    return texts


def chapter_of(found: Structure, number: int) -> DetectedChapter:
    return next(c for c in found.chapters if c.first == number)


def id_set(found: Structure) -> set[str]:
    return {c.id for c in found.chapters} | {s.id for s in found.scenes}


# ----------------------------------------------------------------------------- signals


def test_chapter_patterns_and_layout_signals() -> None:
    paras = build(
        "Prólogo",
        BODY,
        ("Capítulo 1", {"centred": True, "page_break_before": True}),
        BODY,
        "",
        "CHAPTER IV. The Storm",
        BODY,
    )
    found = detect(paras)
    assert [c.first for c in found.chapters] == [1, 3, 6]
    assert found.chapters[0].signals == ("section_name",)
    assert found.chapters[1].signals == ("chapter_word", "centred", "page_break_before")
    assert found.chapters[2].signals == ("chapter_word", "blank_lines")
    assert [c.kind for c in found.chapters] == ["prologue", "chapter", "chapter"]
    assert found.chapters[2].number == 4


def test_heading_style_and_outline_level_are_signals() -> None:
    paras = build(
        ("Un comienzo", {"heading_level": 1, "heading_source": "heading_style"}),
        BODY,
        ("Otro comienzo", {"heading_level": 1, "heading_source": "outline_level"}),
        BODY,
        ("Una sección", {"heading_level": 2, "heading_source": "heading_style"}),
        BODY,
    )
    found = detect(paras)
    assert [(c.first, c.signals) for c in found.chapters] == [
        (1, ("heading_style",)),
        (3, ("outline_level",)),
    ]
    deeper = detect(paras, Profile(chapter_level=2))
    assert [c.first for c in deeper.chapters] == [1, 3, 5]
    off = detect(paras, Profile(heading_styles=False))
    assert off.chapters == ()


def test_bare_numeral_needs_a_layout_signal() -> None:
    paras = build("7", BODY, ("VIII", {"centred": True}), BODY, ("9", {"page_break_before": True}))
    found = detect(paras)
    assert [(c.first, c.kind, c.signals) for c in found.chapters] == [
        (3, "numeral", ("numeral", "centred")),
        (5, "numeral", ("numeral", "page_break_before")),
    ]
    assert detect(paras, Profile(chapter_patterns=False)).chapters == ()


def test_scene_breaks_and_their_signals() -> None:
    texts = ["Capítulo 1", BODY, "* * *", BODY, "", "", "---", BODY, "* * *", "* * *", BODY, "***"]
    found = detect(build(*texts))
    assert [(s.first, s.last, s.signals) for s in found.scenes] == [
        (1, 2, ("chapter_start",)),
        (3, 6, ("scene_marker",)),
        (7, 9, ("scene_marker", "blank_lines")),
        (10, 12, ("scene_marker",)),
    ]
    # a marker right after a heading, doubled, or at the end of the chapter opens no scene
    reasons = {r for _, kind, r in found.suppressed if kind == "scene"}
    assert reasons == {"no_text_between"}
    assert [n for n, _, _ in found.suppressed] == [9, 12]


def test_blank_lines_start_a_scene_only_when_the_profile_asks() -> None:
    paras = build("Capítulo 1", BODY, "", "", BODY, "", BODY)
    assert len(detect(paras).scenes) == 1
    found = detect(paras, Profile(scene_blank_lines=2))
    assert [(s.first, s.signals) for s in found.scenes] == [
        (1, ("chapter_start",)),
        (5, ("blank_lines",)),
    ]


def test_text_before_the_first_chapter_is_a_scene_without_a_chapter() -> None:
    found = detect(build("", BODY, "Capítulo 1", BODY))
    assert found.scenes[0].chapter is None
    assert (found.scenes[0].first, found.scenes[0].signals) == (2, ("document_start",))
    assert found.scenes[1].chapter == found.chapters[0].id


# ------------------------------------------------------------------------ profile words


def test_minimum_chapter_words_drops_short_chapters_but_not_parts_or_forced() -> None:
    paras = build(
        "Parte I",
        "Capítulo 1",
        BODY,
        BODY,
        "Capítulo 2",
        "Una línea corta.",
        "Capítulo 3",
        BODY,
        BODY,
        "Capítulo 4",
        "Breve.",
    )
    found = detect(paras, Profile(min_chapter_words=20))
    assert [(p.first, p.chapters) for p in found.parts] == [(1, 2)]  # a part holds chapters
    assert [c.first for c in found.chapters] == [2, 7]
    assert chapter_of(found, 2).last == 6  # the dropped chapter's text stays in the one above
    assert [(n, k, r) for n, k, r in found.suppressed if k == "chapter"] == [
        (5, "chapter", "below_minimum_words"),
        (10, "chapter", "below_minimum_words"),
    ]
    forced = detect(
        paras, Profile(min_chapter_words=20), [Override(paras[9].fp, "chapter", "force")]
    )
    assert [c.first for c in forced.chapters] == [2, 7, 10]
    assert "override" in chapter_of(forced, 10).signals
    assert [c.first for c in detect(paras).chapters] == [2, 5, 7, 10]


# ------------------------------------------------------------------------- stable ids


def test_ids_are_unique_and_use_prefixes() -> None:
    found = detect(build(*book()))
    every = [c.id for c in found.chapters] + [s.id for s in found.scenes]
    assert len(every) == len(set(every)) == 6 + 12
    assert all(i.startswith("ch-") for i in [c.id for c in found.chapters])
    assert all(i.startswith("sc-") for i in [s.id for s in found.scenes])


def test_ids_survive_edits_inside_other_chapters_and_insertions_elsewhere() -> None:
    texts = book()
    before = detect(build(*texts))

    # edit the text of every paragraph of chapters 2, 3 and 5 except their headings
    edited = list(texts)
    for i, text in enumerate(texts):
        # not the first paragraph of a scene: that one names the scene's id
        edit = f"Texto {2}." in text or f"Texto {5}." in text
        if text.startswith(BODY) and edit and not text.endswith(".0."):
            edited[i] = text + " Y siguió lloviendo."
    after = detect(build(*edited))
    assert id_set(after) == id_set(before)

    # insert new paragraphs, a new scene and a whole new chapter after chapter 5's scenes
    inserted = list(texts)
    inserted[3:3] = ["Un párrafo nuevo al comienzo.", "Otro más."]
    inserted[-1:-1] = ["* * *", "Una escena más al final del libro."]
    inserted += ["Capítulo 7", BODY, BODY]
    grown = detect(build(*inserted))
    assert id_set(before) <= id_set(grown)
    assert len(grown.chapters) == 7

    # ids do not move with position: shifting the paragraph numbers keeps every id
    shifted = detect(build("Nota previa de la edición.", *texts))
    assert id_set(before) <= id_set(shifted)
    assert [c.id for c in shifted.chapters][-6:] == [c.id for c in before.chapters]


def test_repeated_headings_and_markers_get_distinct_stable_ids() -> None:
    heading = {"heading_level": 1, "heading_source": "heading_style"}
    parts: list[str | tuple[str, dict[str, Any]]] = [
        ("Interludio", heading), BODY, "* * *", BODY,
    ]  # fmt: skip
    found = detect(build(*parts, *parts))
    assert len(id_set(found)) == 2 + 4
    # a third copy in front leaves the first two copies' places to the later ids
    extra = detect(build(*parts, *parts, *parts))
    assert len(extra.chapters) == 3
    assert [c.id for c in found.chapters] == [c.id for c in extra.chapters][:2]


def test_renaming_a_heading_changes_only_that_chapter() -> None:
    texts = book(3)
    before = detect(build(*texts))
    texts[texts.index("Capítulo 2")] = "Capítulo 2: La tormenta"
    after = detect(build(*texts))
    assert after.chapters[0].id == before.chapters[0].id
    assert after.chapters[2].id == before.chapters[2].id
    assert after.chapters[1].id != before.chapters[1].id
    assert {s.id for s in after.scenes if s.chapter == before.chapters[2].id} == {
        s.id for s in before.scenes if s.chapter == before.chapters[2].id
    }


# ---------------------------------------------------------------------------- overrides


def override_failures() -> list[str]:
    """Every way a forced or denied chapter or scene fails to take effect."""
    failures: list[str] = []
    texts = book(5)
    for target in range(len(texts)):
        text = texts[target]
        if text.startswith("Capítulo") or not text.strip():
            continue
        for variant, shifted in (("plain", texts), ("shifted", ["Nota.", BODY, *texts])):
            paras = build(*shifted)
            index = target + (len(shifted) - len(texts))
            fp = paras[index].fp
            found = detect(paras, overrides=[Override(fp, "chapter", "force")])
            starts = [c for c in found.chapters if c.first == paras[index].number]
            if len(starts) != 1 or "override" not in starts[0].signals:
                failures.append(f"force chapter at p{index + 1} ({variant})")
    for c in detect(build(*texts)).chapters:
        paras = build(*texts)
        fp = paras[c.first - 1].fp
        found = detect(paras, overrides=[Override(fp, "chapter", "deny")])
        if any(x.first == c.first for x in found.chapters):
            failures.append(f"deny chapter at p{c.first}")
    return failures


def test_override_forces_chapter_start_by_fingerprint() -> None:
    """Gate: ``structure_override_failures == 0``."""
    failures = override_failures()
    print(f"structure_override_failures={len(failures)}")
    assert failures == []

    paras = build(*book(3))
    target = paras[5]  # an ordinary paragraph in chapter 1
    found = detect(paras, overrides=[Override(target.fp, "chapter", "force")])
    chapter = chapter_of(found, target.number)
    assert chapter.signals[0] == "override"
    assert chapter_of(found, 1).last == target.number - 1
    assert found.unmatched_overrides == ()


def test_deny_override_keeps_a_heading_out_and_reports_it() -> None:
    paras = build(*book(3))
    found = detect(paras, overrides=[Override(paras[0].fp, "chapter", "deny")])
    assert paras[0].number not in [c.first for c in found.chapters]
    assert (1, "chapter", "override") in found.suppressed


def test_scene_overrides_force_and_deny() -> None:
    paras = build("Capítulo 1", BODY, "* * *", BODY, BODY + " Final.", "* * *", BODY)
    deny = detect(paras, overrides=[Override(paras[2].fp, "scene", "deny")])
    assert [s.first for s in deny.scenes] == [1]  # both "* * *" share the fingerprint
    forced = detect(paras, overrides=[Override(paras[4].fp, "scene", "force")])
    assert [(s.first, s.signals[0]) for s in forced.scenes] == [
        (1, "chapter_start"),
        (3, "scene_marker"),
        (5, "override"),
        (6, "scene_marker"),
    ]


def test_nth_limits_an_override_to_one_of_several_equal_paragraphs() -> None:
    paras = build("Capítulo 1", BODY, "* * *", BODY, "* * *", BODY)
    found = detect(paras, overrides=[Override(paras[2].fp, "scene", "deny", nth=2)])
    assert [s.first for s in found.scenes] == [1, 3]


def test_overrides_survive_edits_elsewhere_and_unmatched_ones_are_reported() -> None:
    texts = book(4)
    target = texts[5]
    override = Override(fingerprint(target), "chapter", "force")
    before = detect(build(*texts), overrides=[override])
    edited = [
        t + " (revisado)" if i != 5 and not t.startswith("Cap") else t for i, t in enumerate(texts)
    ]
    edited[:0] = ["Una nota nueva.", "* * *"]
    after = detect(build(*edited), overrides=[override])
    assert before.unmatched_overrides == ()
    assert after.unmatched_overrides == ()
    forced_id = next(c.id for c in before.chapters if "override" in c.signals)
    assert forced_id in {c.id for c in after.chapters}
    # editing the paragraph the override names makes it lapse, loudly
    edited[5 + 2] = target + " cambiado"
    lapsed = detect(build(*edited), overrides=[override])
    assert lapsed.unmatched_overrides == (override,)
    assert not any("override" in c.signals for c in lapsed.chapters)


# --------------------------------------------------------------------------- the op


Run = tuple[int, dict[str, Any]]


def _call(capsys: pytest.CaptureFixture[str], op: str, **request: Any) -> Run:
    code = main([op, "--json", json.dumps(request)])
    return code, json.loads(capsys.readouterr().out)


def _write(path: Path, body: str, styles: str = "") -> Path:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", document)
        if styles:
            zf.writestr(
                "word/styles.xml",
                f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                f'<w:styles xmlns:w="{ns}">{styles}</w:styles>',
            )
    return path


def _p(text: str, ppr: str = "", prefix: str = "") -> str:
    props = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return f"<w:p>{props}{prefix}<w:r><w:t>{text}</w:t></w:r></w:p>"


@pytest.fixture
def novel(tmp_path: Path) -> Path:
    styles = (
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
        '<w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>'
    )
    page_break = '<w:r><w:br w:type="page"/></w:r>'
    body = "".join(
        [
            _p("Un comienzo", '<w:pStyle w:val="Heading1"/>'),
            _p(BODY),
            _p("* * *", '<w:jc w:val="center"/>'),
            _p("¿Quién anda ahí? —preguntó Ana. Nadie contestó."),
            f"<w:p>{page_break}</w:p>",
            _p("II", '<w:jc w:val="center"/>'),
            _p(BODY),
            _p("Capítulo 3", "<w:pageBreakBefore/>"),
            _p(BODY),
        ]
    )
    return _write(tmp_path / "novel.docx", body, styles)


def test_segment_chapters_report_ids_and_signals(
    novel: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, env = _call(capsys, "segment", doc=str(novel))
    assert code == 0, env
    chapters = env["result"]["chapters"]
    assert [(c["first"], c["kind"]) for c in chapters] == [
        (1, "chapter"),
        (6, "numeral"),
        (8, "chapter"),
    ]
    assert chapters[0]["signals"] == ["heading_style"]
    assert chapters[1]["signals"] == ["numeral", "centred", "page_break_before"]
    assert chapters[2]["signals"] == ["chapter_word", "page_break_before"]
    assert all(c["id"].startswith("ch-") for c in chapters)
    assert env["result"]["total"] == 3 and env["next"] is None


def test_segment_scenes_filter_by_chapter(novel: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, env = _call(capsys, "segment", doc=str(novel))
    first = env["result"]["chapters"][0]["id"]
    code, env = _call(capsys, "segment", doc=str(novel), unit="scenes", chapter=first)
    assert code == 0, env
    scenes = env["result"]["scenes"]
    assert [(s["first"], s["signals"]) for s in scenes] == [
        (1, ["chapter_start"]),
        (3, ["scene_marker", "centred"]),
    ]
    code, env = _call(capsys, "segment", doc=str(novel), unit="scenes", chapter="ch-00000000")
    assert code != 0 and env["error"]["code"] == "ADDRESS_INVALID"


def test_segment_overrides_profile_and_unmatched(
    novel: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, env = _call(capsys, "segment", doc=str(novel))
    chapters = env["result"]["chapters"]
    fp = chapters[1]["at"].split("#")[1]
    code, env = _call(
        capsys,
        "segment",
        doc=str(novel),
        overrides=[
            {"fp": fp, "kind": "chapter", "action": "deny"},
            {"fp": "00000000", "kind": "scene", "action": "force"},
        ],
        profile={"min_chapter_words": 10},
    )
    assert code == 0, env
    result = env["result"]
    assert [c["first"] for c in result["chapters"]] == [1, 8]
    assert result["unmatched_overrides"] == [{"fp": "00000000", "kind": "scene", "action": "force"}]
    assert result["suppressed"][0]["reason"] == "override"


def test_segment_pages_with_cursors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    body = "".join(_p(t) for t in book(40, 3, 2))
    doc = _write(tmp_path / "long.docx", body)
    seen: list[str] = []
    cursor = None
    for _ in range(500):
        extra: dict[str, Any] = {"cursor": cursor} if cursor else {}
        code, env = _call(capsys, "segment", doc=str(doc), unit="scenes", limit=25, **extra)
        assert code == 0, env
        seen += [s["id"] for s in env["result"]["scenes"]]
        cursor = env["next"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 40 * 3
    code, env = _call(
        capsys, "segment", doc=str(doc), unit="scenes", limit=25, cursor="c:outline:x"
    )
    assert code != 0 and env["error"]["code"] == "SCHEMA_INVALID"


def test_segment_sentences_and_dialogue(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = _write(
        tmp_path / "talk.docx",
        _p("¿Quién anda ahí? Nadie contestó. Llovía.")
        + _p("—Hola —dijo Ana—. Vamos.")
        + _p("Sin voces aquí."),
    )
    code, env = _call(capsys, "segment", doc=str(doc), unit="sentences")
    assert code == 0, env
    sentences = env["result"]["sentences"]
    assert [s["at"].rsplit(".", 1)[1] for s in sentences[:3]] == ["s1", "s2", "s3"]
    assert sentences[0]["text"] == "¿Quién anda ahí?"
    assert sentences[0]["at"].startswith("p1#")
    only = sentences[0]["at"].split(".")[0]
    code, env = _call(capsys, "segment", doc=str(doc), unit="sentences", range=only)
    assert code == 0 and env["result"]["total"] == 3
    code, env = _call(capsys, "segment", doc=str(doc), unit="dialogue")
    assert code == 0, env
    [line] = env["result"]["dialogue"]
    assert line["at"].startswith("p2#")
    assert [p["kind"] for p in line["parts"]] == ["speech", "narration", "speech"]


def test_segment_rejects_a_malformed_override_fingerprint(
    novel: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, env = _call(
        capsys,
        "segment",
        doc=str(novel),
        overrides=[{"fp": "NOT-HEX!!", "kind": "chapter", "action": "force"}],
    )
    assert code != 0 and env["error"]["code"] == "SCHEMA_INVALID"


# ----------------------------------------------------------- review follow-ups


def test_scene_ids_do_not_move_when_scenes_are_added_before_them() -> None:
    base = ["Capítulo 1", "a " + BODY, "***", "b " + BODY, "***", "c " + BODY]
    before = detect(build(*base))
    grown = detect(build(*base[:2], "***", "x " + BODY, "***", "y " + BODY, *base[2:]))
    assert {s.id for s in before.scenes} <= {s.id for s in grown.scenes}
    assert len(grown.scenes) == 5
    # a scene whose first text repeats gets a disambiguator rather than a clash
    twin = detect(build("Capítulo 1", BODY, "***", BODY, "***", BODY, "***", BODY))
    assert len({s.id for s in twin.scenes}) == 4


def test_spelled_numbers_are_chapters_with_a_layout_signal() -> None:
    paras = build(
        "Uno", BODY, ("Dos", {"centred": True}), BODY, ("Tercera", {"page_break_before": True})
    )
    found = detect(paras)
    assert [(c.first, c.kind, c.number) for c in found.chapters] == [
        (3, "numeral", 2),
        (5, "numeral", 3),
    ]
    assert detect(paras, Profile(chapter_patterns=False)).chapters == ()


def test_lone_i_depends_on_language() -> None:
    paras = build("I", BODY, ("I", {"centred": True}), BODY)
    assert [c.first for c in detect(paras).chapters] == [3]
    assert detect(paras, Profile(language="en")).chapters == ()
    both = build(("I", {"centred": True, "page_break_before": True}), BODY)
    assert len(detect(both, Profile(language="en")).chapters) == 1


def test_numbering_only_heading_is_a_heading_not_a_blank() -> None:
    heading = {"heading_level": 1, "heading_source": "heading_style", "empty": True}
    found = detect(build(("", heading), BODY, ("", heading), BODY))
    assert [c.first for c in found.chapters] == [1, 3]
    assert found.chapters[0].signals == ("heading_style",)


def test_unmatched_overrides_report_nth() -> None:
    found = detect(build(BODY), overrides=[Override("00000000", "scene", "deny", nth=2)])
    assert found.unmatched_overrides[0].nth == 2


def test_layout_facts_come_from_the_shared_layout_module(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    br = '<w:r><w:br w:type="page"/></w:r>'
    centre = '<w:jc w:val="center"/>'
    styles = (
        '<w:style w:type="paragraph" w:styleId="Normal" w:default="1"><w:name w:val="Normal"/>'
        '</w:style><w:style w:type="paragraph" w:styleId="Fancy"><w:name w:val="Fancy"/>'
        '<w:basedOn w:val="Normal"/><w:pPr><w:pageBreakBefore/><w:jc w:val="center"/></w:pPr>'
        "</w:style>"
    )
    sect = '<w:sectPr><w:type w:val="nextPage"/></w:sectPr>'
    cases = {
        "custom style": _p("a") + _p("IV", '<w:pStyle w:val="Fancy"/>') + _p("b"),
        "section break": _p("a", sect) + _p("IV", centre) + _p("b"),
        "break at start": _p("a") + f"<w:p>{br}<w:r><w:t>IV</w:t></w:r></w:p>" + _p("b"),
        "break paragraph, then empty": _p("a")
        + f"<w:p>{br}</w:p><w:p/>"
        + _p("IV", centre)
        + _p("b"),
        "break ends previous": f"<w:p><w:r><w:t>a</w:t></w:r>{br}</w:p>"
        + _p("IV", centre)
        + _p("b"),
    }
    for name, body in cases.items():
        doc = _write(tmp_path / "layout.docx", body, styles)
        code, env = _call(capsys, "segment", doc=str(doc))
        assert code == 0, env
        assert [c["text"] for c in env["result"]["chapters"]] == ["IV"], name
