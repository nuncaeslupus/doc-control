"""The in-process Python API: the same operations as functions, for Python callers.

``open(path)`` parses a document once and reuses that parse for every read operation
until the file changes; a write operation through the handle drops it. Results are whole:
the response byte cap and default list limits of the CLI and MCP do not apply, so a list
comes back complete with no ``next``. Errors raise :class:`~doc_control.errors.DocControlError`.

    from doc_control import api

    with api.open("novel.docx") as doc:
        chapters = doc.run("segment", {"unit": "chapters"})
        for p in doc.paragraphs(normalized=True, pages=True):
            ...
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.inprocess import uncapped
from doc_control.model.document import Document, shared_documents
from doc_control.registry import REGISTRY, load_operations

__all__ = ["OpenDocument", "open", "run"]


def run(op: str, request: dict[str, Any]) -> Reply:
    """Run one operation uncapped; the reply carries ``result``, ``warnings`` and ``doc``."""
    load_operations()
    if op not in REGISTRY:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown operation: {op}", op=op)
    with uncapped():
        out = REGISTRY[op].run(request)
    return out if isinstance(out, Reply) else Reply(out)


class OpenDocument:
    """A document held open for several operations; see the module docstring."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._cache: dict[str, tuple[tuple[int, int, int], Document]] = {}

    def reply(self, op: str, request: dict[str, Any] | None = None) -> Reply:
        """Like :meth:`run`, with the warnings and the ``doc`` block."""
        load_operations()
        if op not in REGISTRY:
            raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown operation: {op}", op=op)
        operation = REGISTRY[op]
        req = dict(request or {})
        if "doc" in operation.schema.get("properties", {}):
            req.setdefault("doc", self.path)
        if operation.kind != "read":
            self._cache.clear()
            try:
                return run(op, req)
            finally:
                self._cache.clear()
        with shared_documents(self._cache):
            return run(op, req)

    def run(self, op: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        """The result of ``op`` on this document (``doc`` is filled in).

        Results are whole unless the request set a ``limit`` or an operation stopped at
        its time budget (``find``); then the result carries ``next``, the cursor to
        continue with, and :meth:`reply` has the warnings.
        """
        reply = self.reply(op, request)
        if reply.next_cursor is not None:
            return {**reply.result, "next": reply.next_cursor}
        return reply.result

    def paragraphs(self, *, normalized: bool = False, pages: bool = False) -> list[dict[str, Any]]:
        """Every paragraph with its address, text, style and layout facts (``paragraphs.list``)."""
        result = self.run("paragraphs.list", {"normalized": normalized, "pages": pages})
        rows: list[dict[str, Any]] = result["paragraphs"]
        return rows

    def close(self) -> None:
        self._cache.clear()

    def __enter__(self) -> OpenDocument:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def open(path: str | Path) -> OpenDocument:
    """Open ``path`` for several operations (nothing is read until the first one)."""
    return OpenDocument(path)
