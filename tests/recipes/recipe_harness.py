"""Run a bundled recipe over synthetic paragraphs and read back what the batch leaves."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doc_control.envelope import Reply
from doc_control.model.annotations import read_revisions
from doc_control.model.document import Document
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import make_docx


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def proposal(doc: Path, recipe: str) -> dict[str, Any]:
    load_operations()
    reply = REGISTRY["recipe.run"].run({"doc": str(doc), "recipe": recipe})
    assert isinstance(reply, Reply)
    assert reply.result["applied"] is False
    batch: dict[str, Any] = reply.result["batch"]
    return batch


def apply_recipe(doc: Path, recipe: str) -> int:
    """Apply the recipe's proposal as one tracked batch; the number of operations."""
    batch = proposal(doc, recipe)
    if batch["ops"]:
        assert isinstance(REGISTRY["doc.batch"].run(batch), Reply)
    return len(batch["ops"])


def texts(doc: Path) -> list[str]:
    return [p.text for p in Document(str(doc)).index.paragraphs]


def revisions(doc: Path) -> int:
    return len(read_revisions(Document(str(doc))))


def fresh(directory: Path, *paragraphs: str) -> Path:
    """A new valid document holding ``paragraphs`` (XML-escaped)."""
    directory.mkdir(parents=True, exist_ok=True)
    return make_docx(directory / "doc.docx", *(escape(p) for p in paragraphs))


def after(directory: Path, recipe: str, *paragraphs: str) -> list[str]:
    """The paragraph texts once ``recipe`` has been applied to a fresh document."""
    doc = fresh(directory, *paragraphs)
    apply_recipe(doc, recipe)
    return texts(doc)
