"""Page-plan scenarios and a small writer that turns a plan into a .docx (LAYOUT T2)."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

from tests.helpers import CONTENT_TYPES, RELS

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HF_CONTENT = "application/vnd.openxmlformats-officedocument.wordprocessingml.{}+xml"
KINDS = ("default", "first", "even")
PARAGRAPHS_PER_SECTION = 3
XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _field(code: str) -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> {code} </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>7</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def _runs(text: str) -> str:
    """Runs for header text; ``{PAGE}`` and ``{chapter}`` as complex fields, ``{NUMPAGES}``
    as a simple field."""
    out = ""
    for part in re.split(r"(\{PAGE\}|\{NUMPAGES\}|\{chapter\})", text):
        if part == "{PAGE}":
            out += _field("PAGE")
        elif part == "{chapter}":
            out += _field('STYLEREF "Heading 1" \\* MERGEFORMAT')
        elif part == "{NUMPAGES}":
            out += '<w:fldSimple w:instr=" NUMPAGES "><w:r><w:t>9</w:t></w:r></w:fldSimple>'
        elif part:
            out += f"<w:r><w:t>{part}</w:t></w:r>"
    return out


def _sect(sect: dict[str, Any], refs: str) -> str:
    num = sect["numbering"]
    attrs = f' w:fmt="{num["format"]}"'
    if num["start"] is not None:
        attrs += f' w:start="{num["start"]}"'
    title = "<w:titlePg/>" if sect["first_page_different"] else ""
    return (
        f'<w:sectPr>{refs}<w:type w:val="{sect["break"]}"/><w:pgNumType{attrs}/>{title}</w:sectPr>'
    )


def write_plan(path: Path, plan: dict[str, Any]) -> Path:
    """A .docx whose section ``i`` starts at paragraph ``1 + 3 * i``, with the plan's
    headers, footers, numbering, breaks and settings."""
    body = ""
    parts: dict[str, bytes] = {}
    rels = ""
    overrides = ""
    sections = plan["sections"]
    for i, sect in enumerate(sections):
        refs = ""
        for group, tag in (("header", "hdr"), ("footer", "ftr")):
            for kind in KINDS:
                text = sect[group].get(kind)
                if text is None:
                    continue
                name = f"{group}{i + 1}_{kind}.xml"
                rid = f"rId{group[0]}{i + 1}{kind[0]}"
                paras = "".join(f"<w:p>{_runs(line)}</w:p>" for line in text.split("\n"))
                parts[f"word/{name}"] = f"{XML_DECL}<w:{tag} {NS}>{paras}</w:{tag}>".encode()
                rels += f'<Relationship Id="{rid}" Type="{REL}/{group}" Target="{name}"/>'
                overrides += (
                    f'<Override PartName="/word/{name}" ContentType="{HF_CONTENT.format(group)}"/>'
                )
                refs += f'<w:{group}Reference w:type="{kind}" r:id="{rid}"/>'
        body += "".join(
            f"<w:p><w:r><w:t>Section {i + 1} paragraph {j + 1}</w:t></w:r></w:p>"
            for j in range(PARAGRAPHS_PER_SECTION - 1)
        )
        last = f"<w:r><w:t>Section {i + 1} end</w:t></w:r>"
        if i < len(sections) - 1:
            body += f"<w:p><w:pPr>{_sect(sect, refs)}</w:pPr>{last}</w:p>"
        else:
            body += f"<w:p>{last}</w:p>{_sect(sect, refs)}"
    settings = ""
    if plan["two_sided"]:
        settings += "<w:evenAndOddHeaders/>"
    if plan["mirror_margins"]:
        settings += "<w:mirrorMargins/>"
    parts["word/document.xml"] = (
        f"{XML_DECL}<w:document {NS} {R_NS}><w:body>{body}</w:body></w:document>"
    ).encode()
    parts["word/settings.xml"] = f"{XML_DECL}<w:settings {NS}>{settings}</w:settings>".encode()
    rels += f'<Relationship Id="rIdS" Type="{REL}/settings" Target="settings.xml"/>'
    overrides += (
        '<Override PartName="/word/settings.xml" ContentType="'
        'application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
    )
    doc_rels = (
        f"{XML_DECL}"
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml", CONTENT_TYPES.replace("</Types>", overrides + "</Types>")
        )
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        for name, data in parts.items():
            zf.writestr(name, data)
    return path


def section(
    break_: str = "nextPage",
    *,
    shown: bool = True,
    start: int | None = None,
    fmt: str = "decimal",
    header: dict[str, str | None] | None = None,
    footer: dict[str, str | None] | None = None,
    first_page_different: bool = False,
) -> dict[str, Any]:
    """A plan section; ``from`` is filled in by :func:`fill_from`."""
    blank: dict[str, str | None] = {"default": None, "first": None, "even": None}
    return {
        "from": "",
        "break": break_,
        "numbering": {"shown": shown, "start": start, "format": fmt},
        "header": {**blank, **(header or {})},
        "footer": {**blank, **(footer or {})},
        "first_page_different": first_page_different,
    }


def plan(
    *sections: dict[str, Any], two_sided: bool = False, mirror_margins: bool = False
) -> dict[str, Any]:
    return {
        "version": 1,
        "two_sided": two_sided,
        "mirror_margins": mirror_margins,
        "sections": list(sections),
    }


SCENARIOS: dict[str, dict[str, Any]] = {
    "plain": plan(section(shown=False)),
    "front matter roman then arabic": plan(
        section(
            shown=False,
            fmt="lowerRoman",
            header={"default": "Title", "first": ""},
            footer={"default": "", "first": ""},
            first_page_different=True,
        ),
        section("oddPage", start=1, footer={"default": "Page {PAGE} of {NUMPAGES}"}),
    ),
    "restart per part, inherited headers": plan(
        section(footer={"default": "{PAGE}"}, header={"default": "Part one"}),
        section(start=1),
        section("continuous", start=1, header={"default": "Part three"}),
    ),
    "two-sided mirrored with chapter headers": plan(
        section(shown=False, header={"default": "", "even": ""}, footer={"default": ""}),
        section(
            "oddPage",
            start=1,
            header={"default": "{chapter}", "even": "Author Name"},
            footer={"default": "{PAGE}", "even": "{PAGE}"},
        ),
        section("evenPage", header={"default": "{chapter}\nSecond line"}),
        two_sided=True,
        mirror_margins=True,
    ),
}


def fill_from(expected: dict[str, Any], doc: Any) -> dict[str, Any]:
    """``expected`` with each section's ``from`` set to the address of the first paragraph
    the writer put in that section."""
    for i, sect in enumerate(expected["sections"]):
        sect["from"] = str(doc.index.paragraph(1 + PARAGRAPHS_PER_SECTION * i).address)
    return expected
