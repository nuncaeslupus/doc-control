"""The corpus validator itself: unreadable input, LibreOffice failures, text coverage."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest

from tests.corpus import test_generator, validator
from tests.corpus.generator import generate, generate_parts
from tests.corpus.structural import validate_structure

REPO = Path(__file__).resolve().parents[2]

FAKE_SOFFICE = """\
import os, pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index("--outdir") + 1])
ext = args[args.index("--convert-to") + 1].split(":")[0]
body = os.environ.get("FAKE_SOFFICE_OUTPUT")
for src in (a for a in args if a.endswith(".docx")):
    if body is not None:
        (out / (pathlib.Path(src).stem + "." + ext)).write_text(body, encoding="utf-8")
sys.stdout.write("fake soffice stdout line\\n")
sys.stderr.write("Error: fake soffice exploded on purpose\\n")
"""

FakeSoffice = Callable[[str | None], None]


@pytest.fixture
def fake_soffice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSoffice:
    """Point the validator at a stub soffice that writes ``output`` for every input."""
    if os.name == "nt":  # no shebangs: a .cmd wrapper runs the stub with this Python
        (tmp_path / "fake_soffice.py").write_text(FAKE_SOFFICE, encoding="utf-8")
        script = tmp_path / "fake-soffice.cmd"
        script.write_text(f'@"{sys.executable}" "%~dp0fake_soffice.py" %*\r\n', encoding="utf-8")
    else:
        script = tmp_path / "fake-soffice"
        script.write_text(f"#!{sys.executable}\n{FAKE_SOFFICE}", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(validator, "soffice_path", lambda: str(script))
    monkeypatch.delenv(validator.REQUIRE_ENV, raising=False)

    def configure(output: str | None) -> None:
        if output is None:
            monkeypatch.delenv("FAKE_SOFFICE_OUTPUT", raising=False)
        else:
            monkeypatch.setenv("FAKE_SOFFICE_OUTPUT", output)

    return configure


def _all_text(data: bytes) -> str:
    """Every ``w:t`` in the main story and footnotes, as a crude HTML page."""
    with zipfile.ZipFile(BytesIO(data)) as zf:
        xml = "".join(
            zf.read(n).decode("utf-8")
            for n in ("word/document.xml", "word/footnotes.xml")
            if n in zf.namelist()
        )
    return "<html><body>" + " ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)) + "</body></html>"


def _rebuild(parts: dict[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


# -- 1. unreadable packages are reported, never crash the run ------------


def test_non_zip_and_partial_docx_are_reported_not_crashing(
    fake_soffice: FakeSoffice, tmp_path: Path
) -> None:
    good = generate("plain", 0)
    fake_soffice(_all_text(good))  # soffice "converts" everything, RTF included
    docs = {"rtf": rb"{\rtf1\ansi Hello}", "partial": good[: len(good) // 2], "good": good}
    results = validator.validate_documents(docs, tmp_path)
    assert results["good"] == []
    assert any("not a zip package" in p for p in results["rtf"]), results["rtf"]
    assert any("unreadable" in p for p in results["rtf"]), results["rtf"]
    assert results["partial"], results["partial"]


def test_structural_lint_reports_corrupt_member_data() -> None:
    data = bytearray(_rebuild(generate_parts("plain", 0), zipfile.ZIP_STORED))
    at = data.index(b"<w:body>")
    data[at : at + 8] = b"<w:bodX>"  # CRC no longer matches; the zip index still reads
    problems = validate_structure(bytes(data))
    assert any("unreadable zip package" in p for p in problems), problems


# -- 2. a lint-clean document LibreOffice cannot load --------------------


def _lint_clean_but_unloadable() -> dict[str, bytes]:
    wrong_rel = generate_parts("plain", 0)
    wrong_rel["_rels/.rels"] = wrong_rel["_rels/.rels"].replace(
        b'relationships/officeDocument"', b'relationships/notADocument"'
    )
    as_calc = generate_parts("plain", 0)
    as_calc["[Content_Types].xml"] = as_calc["[Content_Types].xml"].replace(
        b"wordprocessingml.document.main+xml", b"spreadsheetml.sheet.main+xml"
    )
    return {"wrong-rel": _rebuild(wrong_rel), "as-calc": _rebuild(as_calc)}


@pytest.mark.skipif(validator.soffice_path() is None, reason="LibreOffice (soffice) not found")
def test_validator_rejects_lint_clean_document_libreoffice_cannot_load(tmp_path: Path) -> None:
    docs = _lint_clean_but_unloadable()
    for data in docs.values():
        assert validate_structure(data) == []  # only LibreOffice can catch these
    results = validator.validate_documents({**docs, "good": generate("plain", 0)}, tmp_path)
    assert results["good"] == []
    for doc_id in docs:
        assert any("LibreOffice" in p for p in results[doc_id]), (doc_id, results[doc_id])


# -- 3. no silent fallback to lint-only ----------------------------------


def test_require_soffice_fails_when_libreoffice_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOC_CONTROL_NO_SOFFICE", "1")
    monkeypatch.delenv(validator.REQUIRE_ENV, raising=False)
    assert "lint only" in validator.validator_name()
    assert validator.validate_documents({"plain": generate("plain", 0)}, tmp_path) == {"plain": []}
    monkeypatch.setenv(validator.REQUIRE_ENV, "1")
    with pytest.raises(RuntimeError, match=validator.REQUIRE_ENV):
        validator.validate_documents({"plain": generate("plain", 0)}, tmp_path)


# -- 4. all of the text, not the first 40 characters ---------------------


def test_missing_later_paragraph_is_flagged(fake_soffice: FakeSoffice, tmp_path: Path) -> None:
    data = generate("tables", 0)
    segments = validator.expected_segments(data)
    fake_soffice("<p>" + " ".join(s.text for s in segments[:1]) + "</p>")
    results = validator.validate_documents({"tables": data}, tmp_path)
    assert any(
        f"lacks {len(segments) - 1} of {len(segments)} text segments" in p
        for p in results["tables"]
    ), results["tables"]


def test_whitespace_differences_are_tolerated(fake_soffice: FakeSoffice, tmp_path: Path) -> None:
    data = generate("kitchen_sink", 0)
    page = _all_text(data)
    fake_soffice(page.replace(" ", "\n  \t").replace("<body>", "<head><title>t</title></head>"))
    assert validator.validate_documents({"ks": data}, tmp_path) == {"ks": []}


def test_expected_segments_cover_tables_footnotes_insertions_not_deletions_or_fields() -> None:
    segments = validator.expected_segments(generate("kitchen_sink", 0))
    texts = " | ".join(s.text for s in segments)
    stories = {s.story for s in segments}
    assert stories == {"body", "footnote"}
    assert "R1C1" in texts  # table cell
    assert "new wording" in texts  # inserted text
    assert "old wording" not in texts  # deleted text is not visible
    assert "12 October 2026" not in texts  # field results are recomputed by LibreOffice
    assert "Page" in texts and " of the report." in texts  # text around a field stays
    assert "Bookmarked target paragraph." in texts
    # a footnote reference splits its paragraph: "…The.1 More." in LibreOffice output
    assert any(s.text.strip() == "More." for s in segments)


# -- 5. collection does not read the private corpus ----------------------


def test_collection_does_not_read_private_corpus() -> None:
    probe = (
        "import sys, pytest\n"
        "import tests.corpus.private as p\n"
        "def boom():\n"
        "    raise SystemExit('private corpus read during collection')\n"
        "p.private_documents = boom\n"
        "sys.exit(pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider',\n"
        "                      'tests/corpus/test_generator.py']))\n"
    )
    env = {**os.environ, "DOC_CONTROL_PRIVATE_CORPUS": str(REPO / "tests")}
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "test_private_corpus_documents_pass_validator" in proc.stdout


# -- 6. wall-clock budgets live under the bench marker -------------------


def test_timing_assertion_is_a_registered_bench_test() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_opts = config["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("bench:") for m in pytest_opts["markers"])
    assert "not bench" in pytest_opts["addopts"]
    marks = getattr(test_generator.test_novel_generation_is_fast, "pytestmark", [])
    assert "bench" in {m.name for m in marks}


# -- 7. LibreOffice's own words survive into the failure reason ----------


def test_libreoffice_output_is_kept_in_failure_reason(
    fake_soffice: FakeSoffice, tmp_path: Path
) -> None:
    fake_soffice(None)  # converts nothing
    results = validator.validate_documents({"plain": generate("plain", 0)}, tmp_path)
    reason = " ".join(results["plain"])
    assert "fake soffice exploded on purpose" in reason, reason
    assert len(reason) < 2000


@pytest.mark.skipif(validator.soffice_path() is None, reason="LibreOffice not installed")
def test_round_trip_converts_every_document_across_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many documents go through several soffice runs and none is dropped."""
    monkeypatch.setattr(validator, "BATCH_SIZE", 2)
    docs = {f"doc-{seed}": generate("plain", seed) for seed in range(5)}
    results = validator.libreoffice_round_trip(docs, tmp_path)
    # An empty problem list means the document's HTML output exists and holds its text.
    assert results == {doc_id: [] for doc_id in docs}


@pytest.mark.skipif(validator.soffice_path() is None, reason="LibreOffice not installed")
def test_story_kinds_round_trip_through_libreoffice(tmp_path: Path) -> None:
    from tests.corpus.generator import STORY_KINDS

    docs = {kind: generate(kind, seed) for kind in STORY_KINDS for seed in (0,)}
    stories = {
        kind: {seg.story for seg in validator.expected_segments(data)}
        for kind, data in docs.items()
    }
    assert stories == {
        "notes": {"body", "footnote", "endnote"},
        "headers": {"body", "header", "footer"},
    }
    # story_fixture_failures == 0: no note, header or footer text is lost.
    assert validator.validate_documents(docs, tmp_path) == {kind: [] for kind in docs}


@pytest.mark.skipif(validator.soffice_path() is None, reason="LibreOffice not installed")
def test_header_text_missing_from_libreoffice_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "headers.docx"
    path.write_bytes(generate("headers", 0))
    kept = validator.Segment("header", 1, "Chapter header, odd pages")
    lost = validator.Segment("header", 1, "Not in any page style")
    soffice = validator.soffice_path()
    assert soffice is not None
    missing = validator._missing_page_segments(soffice, path, [kept, lost], tmp_path)
    assert missing == [lost]
