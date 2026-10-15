"""DOC_CONTROL_AUTHOR sets the revision author of a write that names none."""

from pathlib import Path

import pytest

from doc_control import api
from tests.helpers import make_docx


def _insert(doc: Path, **extra: str) -> list[str]:
    (first,) = api.run("paragraphs.list", {"doc": str(doc)}).result["paragraphs"]
    op = {"op": "para.insert", "after": first["at"], "text": "Added."}
    api.run("batch", {"doc": str(doc), "ops": [op], **extra})
    revs = api.run("revisions.list", {"doc": str(doc)}).result["revisions"]
    return sorted({r["author"] for r in revs})


@pytest.mark.parametrize(
    ("env", "extra", "author"),
    [
        (None, {}, "Claude"),
        ("", {}, "Claude"),
        ("  ", {}, "Claude"),
        ("Ana Editor", {}, "Ana Editor"),
        ("Ana Editor", {"author": "Luis"}, "Luis"),
    ],
)
def test_author_comes_from_the_request_then_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: str | None,
    extra: dict[str, str],
    author: str,
) -> None:
    if env is None:
        monkeypatch.delenv("DOC_CONTROL_AUTHOR", raising=False)
    else:
        monkeypatch.setenv("DOC_CONTROL_AUTHOR", env)
    doc = make_docx(tmp_path / "d.docx", "Hello world.")
    assert _insert(doc, **extra) == [author]


def test_the_environment_never_filters_a_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = make_docx(tmp_path / "d.docx", "Hello world.")
    monkeypatch.delenv("DOC_CONTROL_AUTHOR", raising=False)
    _insert(doc)
    before = api.run("revisions.list", {"doc": str(doc)}).result["revisions"]
    monkeypatch.setenv("DOC_CONTROL_AUTHOR", "Someone Else")
    assert api.run("revisions.list", {"doc": str(doc)}).result["revisions"] == before
    assert before
