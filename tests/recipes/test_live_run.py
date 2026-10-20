"""``recipe.run`` with ``engine: "live"``: the open document's texts, a live batch back."""

from __future__ import annotations

from pathlib import Path

from doc_control.cli import execute
from doc_control.live.dispatch import using
from doc_control.live.protocol import FakeAdapter
from doc_control.registry import load_operations
from tests.helpers import make_docx


def test_live_run_reads_the_open_document_and_its_batch_applies_live(tmp_path: Path) -> None:
    # The file says otherwise: only the open document's texts count.
    doc = make_docx(tmp_path / "novel.docx", "Clean.", "Clean too.")
    fake = FakeAdapter({str(doc): ["One  space.", "", "Two   spaces  here."]})
    registry = load_operations()
    with using(fake):
        fake.open(doc)
        envelope, _ = execute(
            registry,
            "recipe.run",
            {"doc": str(doc), "recipe": "collapse-spaces", "engine": "live"},
        )
        result = envelope["result"]
        batch = result["batch"]
        assert batch["engine"] == "live" and "expect" not in batch
        assert result["applied"] is False and result["skipped"] == []
        assert [op["at"].split("#")[0] for op in batch["ops"]] == ["p1", "p3", "p3"]
        assert list(result["paragraphs"].values()) == ["One  space.", "Two   spaces  here."]
        envelope, _ = execute(registry, "doc.batch", batch)
        assert envelope["result"]["applied"] == 3
    assert fake.paragraphs(doc) == ["One space.", "", "Two spaces here."]


def test_live_run_needs_the_document_open(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "novel.docx", "One  space.")
    with using(FakeAdapter()):
        envelope, _ = execute(
            load_operations(),
            "recipe.run",
            {"doc": str(doc), "recipe": "collapse-spaces", "engine": "live"},
        )
    assert envelope["error"]["code"] == "ENGINE_UNAVAILABLE"


def test_live_run_refuses_a_recipe_that_looks_at_styles(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "novel.docx", "One.")
    fake = FakeAdapter({str(doc): ["One."]})
    with using(fake):
        fake.open(doc)
        envelope, _ = execute(
            load_operations(),
            "recipe.run",
            {"doc": str(doc), "recipe": "empty-headings", "engine": "live"},
        )
    assert envelope["error"]["code"] == "ENGINE_UNAVAILABLE"
    assert "file engine" in envelope["error"]["details"]["reason"]


def test_live_run_skips_every_edit_of_a_paragraph_it_cannot_fix_whole(tmp_path: Path) -> None:
    # fake-centring removes the spaces and centres: live cannot centre, so it does neither.
    doc = make_docx(tmp_path / "novel.docx", "One.")
    fake = FakeAdapter({str(doc): ["          Centred.", "Two  spaces."]})
    with using(fake):
        fake.open(doc)
        envelope, _ = execute(
            load_operations(),
            "recipe.run",
            {"doc": str(doc), "recipe": "fake-centring", "engine": "live"},
        )
    result = envelope["result"]
    assert result["batch"]["ops"] == [] and result["paragraphs"] == {}
    assert [(s["op"], s["reason"]) for s in result["skipped"]] == [
        ("text.replace", "paired with an edit not supported live"),
        ("format.apply", "not supported live"),
    ]
