"""Hand-made page-label scenarios and the PDF checker that reads them back.

A scenario is a list of sections; each section says how many pages it has, how it
starts, how its page numbers are formatted and whether they are shown. The writer
turns it into a minimal ``.docx`` whose footer prints ``pg:<PAGE>`` (``pg:-`` when the
number is not shown) and whose header prints ``hd:<text>``. LibreOffice exports it to
PDF, ``pdftotext`` gives each page's text, and the printed markers are compared with
the plan.
"""

from __future__ import annotations

import re
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from tests.corpus.validator import soffice_path
from tests.helpers import soffice_dir

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HDR_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
FTR_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"


#: LibreOffice leaves the blank verso a recto break makes out of its PDF unless told to keep it.
PDF_FILTER = 'pdf:writer_pdf_Export:{"IsSkipEmptyPages":{"type":"boolean","value":"false"}}'


@dataclass(frozen=True)
class Sec:
    pages: int
    start: int | None = None
    fmt: str = "decimal"
    shown: bool = True
    brk: str = "nextPage"
    header: str = "run"
    header_even: str | None = None  # only used when the scenario is two-sided


@dataclass(frozen=True)
class Scenario:
    name: str
    sections: list[Sec]
    expected: list[str]
    two_sided: bool = False
    expected_headers: list[str] | None = None
    notes: str = field(default="", compare=False)


def _story(tag: str, text: str, number: bool) -> str:
    field_xml = (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>0</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        if number
        else ""
    )
    lead = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
    root = "hdr" if tag == "header" else "ftr"
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:{root} xmlns:w="{W}">'
        f"<w:p>{lead}{field_xml}</w:p></w:{root}>"
    )


def build_docx(scenario: Scenario, path: Path) -> Path:
    parts: dict[str, str] = {}
    rels: list[str] = []
    overrides: list[str] = []

    def add(kind: str, idx: int, variant: str, text: str, number: bool) -> str:
        name = f"{kind}{idx}{variant}.xml"
        rid = f"rId{len(rels) + 10}"
        parts[f"word/{name}"] = _story(kind, text, number)
        rels.append(f'<Relationship Id="{rid}" Type="{R}/{kind}" Target="{name}"/>')
        content_type = HDR_CT if kind == "header" else FTR_CT
        overrides.append(f'<Override PartName="/word/{name}" ContentType="{content_type}"/>')
        return rid

    body = ""
    for i, sec in enumerate(scenario.sections):
        footer_text = "pg:" if sec.shown else "pg:-"
        foot_default = add("footer", i, "d", footer_text, sec.shown)
        head_default = add("header", i, "d", f"hd:{sec.header}", False)
        refs = (
            f'<w:headerReference w:type="default" r:id="{head_default}"/>'
            f'<w:footerReference w:type="default" r:id="{foot_default}"/>'
        )
        if scenario.two_sided:
            even = sec.header_even or sec.header
            head_even = add("header", i, "e", f"hd:{even}", False)
            foot_even = add("footer", i, "e", footer_text, sec.shown)
            refs += (
                f'<w:headerReference w:type="even" r:id="{head_even}"/>'
                f'<w:footerReference w:type="even" r:id="{foot_even}"/>'
            )
        num = f' w:fmt="{sec.fmt}"' + (f' w:start="{sec.start}"' if sec.start is not None else "")
        sect = (
            f'<w:sectPr>{refs}<w:type w:val="{sec.brk}"/>'
            '<w:pgSz w:w="12240" w:h="15840"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"'
            ' w:header="720" w:footer="720" w:gutter="0"/>'
            f"<w:pgNumType{num}/></w:sectPr>"
        )
        last = i == len(scenario.sections) - 1
        for p in range(sec.pages):
            ppr = "<w:pageBreakBefore/>" if p else ""
            if p == sec.pages - 1 and not last:
                ppr += sect
            body += f"<w:p><w:pPr>{ppr}</w:pPr><w:r><w:t>body s{i}p{p}</w:t></w:r></w:p>"
        if last:
            body += sect

    flags = "<w:evenAndOddHeaders/><w:mirrorMargins/>" if scenario.two_sided else ""
    settings = f'<w:settings xmlns:w="{W}">{flags}</w:settings>'
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}</w:body></w:document>'
    )
    word_ct = "application/vnd.openxmlformats-officedocument.wordprocessingml"
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{word_ct}.document.main+xml"/>'
        f'<Override PartName="/word/settings.xml" ContentType="{word_ct}.settings+xml"/>'
        + "".join(overrides)
        + "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId2" Type="{R}/settings" Target="settings.xml"/>'
        + "".join(rels)
        + "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", doc)
        zf.writestr("word/settings.xml", settings)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        for name, xml in parts.items():
            zf.writestr(name, xml)
    return path


def export_pdfs(docs: list[Path], outdir: Path) -> dict[str, Path]:
    """Export every document to PDF in one headless LibreOffice run, into ``outdir``."""
    soffice = soffice_path()
    assert soffice is not None
    outdir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    with soffice_dir(outdir) as readable:
        src = readable / "src"
        dst = readable / "pdf"
        src.mkdir(exist_ok=True)
        dst.mkdir(exist_ok=True)
        staged = []
        for doc in docs:
            copy = src / doc.name
            copy.write_bytes(doc.read_bytes())
            staged.append(str(copy))
        proc = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={(readable / 'profile').resolve().as_uri()}",
                "--headless",
                "--norestore",
                "--convert-to",
                PDF_FILTER,
                "--outdir",
                str(dst),
                *staged,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        for doc in docs:
            pdf = dst / (doc.stem + ".pdf")
            if not pdf.exists():
                raise RuntimeError(
                    f"soffice made no PDF for {doc.name}: {proc.stdout}{proc.stderr}"
                )
            kept = outdir / pdf.name
            kept.write_bytes(pdf.read_bytes())
            out[doc.stem] = kept
    return out


def page_texts(pdf: Path) -> list[str]:
    """Each page's text, in order (``pdftotext`` separates pages with a form feed)."""
    text = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def printed(pages: list[str], marker: str) -> list[str]:
    """The ``<marker>:<value>`` token printed on each page; ``?`` when a page has none."""
    found = []
    for page in pages:
        m = re.search(rf"{marker}:(\S*)", page)
        found.append(m.group(1) if m else "")
    return found


def label_failures(scenario: Scenario, pdf: Path) -> list[str]:
    pages = page_texts(pdf)
    failures = []
    labels = printed(pages, "pg")
    if labels != scenario.expected:
        failures.append(f"{scenario.name}: page labels {labels} != planned {scenario.expected}")
    if scenario.expected_headers is not None:
        headers = printed(pages, "hd")
        if headers != scenario.expected_headers:
            failures.append(
                f"{scenario.name}: headers {headers} != planned {scenario.expected_headers}"
            )
    return failures
