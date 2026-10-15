"""The in-process Python API: the same operations as functions, for Python callers.

``open(path)`` parses a document once and reuses that parse for every read operation
until the file changes; a write operation through the handle drops it. Results are whole:
the response byte cap and default list limits of the CLI and MCP do not apply, so a list
comes back complete with no ``next``. Errors raise :class:`~doc_control.errors.DocControlError`.

    from doc_control import api

    with api.open("novel.docx") as doc:
        chapters = doc.run("doc.segment", {"unit": "chapters"})
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
from doc_control.ooxml.plaintext import TEXT_MODES, text_mode
from doc_control.registry import get_operation
from doc_control.safety.store import stores_in

__all__ = ["OpenDocument", "open", "run"]


def run(op: str, request: dict[str, Any]) -> Reply:
    """Run one operation uncapped; the reply carries ``result``, ``warnings`` and ``doc``."""
    operation = get_operation(op)
    if operation is None:
        raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown operation: {op}", op=op)
    with uncapped():
        out = operation.run(request)
    return out if isinstance(out, Reply) else Reply(out)


class OpenDocument:
    """A document held open for several operations; see the module docstring."""

    def __init__(
        self,
        path: str | Path,
        store: str | Path | None = None,
        text_mode: str | None = None,
        engine: str = "file",
    ) -> None:
        self.path = str(path)
        self.engine = engine
        self.store = Path(store) if store is not None else None
        self.text_mode = text_mode
        self._cache: dict[str, tuple[tuple[int, int, int], Document]] = {}

    def reply(self, op: str, request: dict[str, Any] | None = None) -> Reply:
        """Like :meth:`run`, with the warnings and the ``doc`` block."""
        if self.text_mode is None:
            return self._reply(op, request)
        with text_mode(self.text_mode):
            return self._reply(op, request)

    def _reply(self, op: str, request: dict[str, Any] | None) -> Reply:
        operation = get_operation(op)
        if operation is None:
            raise DocControlError(ErrorCode.SCHEMA_INVALID, f"unknown operation: {op}", op=op)
        req = dict(request or {})
        if "doc" in operation.schema.get("properties", {}):
            req.setdefault("doc", self.path)
        if self.engine == "live" and "engine" in operation.schema.get("properties", {}):
            req.setdefault("engine", "live")
        if operation.kind != "read":
            self._cache.clear()
            try:
                if self.store is None:
                    return run(op, req)
                with stores_in(self.store):
                    return run(op, req)
            finally:
                self._cache.clear()
        if self.store is not None:
            with stores_in(self.store), shared_documents(self._cache):
                return run(op, req)
        with shared_documents(self._cache):
            return run(op, req)

    def run(self, op: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        """The result of ``op`` on this document (``doc`` is filled in).

        Results are whole unless the request set a ``limit`` or an operation stopped at
        its time budget (``text.find``); then the result carries ``next``, the cursor to
        continue with, and :meth:`reply` has the warnings.
        """
        reply = self.reply(op, request)
        if reply.next_cursor is not None:
            return {**reply.result, "next": reply.next_cursor}
        return reply.result

    def paragraphs(self, *, normalized: bool = False, pages: bool = False) -> list[dict[str, Any]]:
        """Every paragraph with its address, text, style and layout facts (``para.list``).

        The page header (``page_basis``, ``rendered_marks``, ``page_count``) comes with
        ``run("para.list", {"pages": True})``.
        """
        result = self.run("para.list", {"normalized": normalized, "pages": pages})
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


def open(
    path: str | Path,
    store: str | Path | None = None,
    text_mode: str | None = None,
    engine: str = "file",
    app: str | None = None,
) -> OpenDocument:
    """Open ``path`` for several operations (nothing is read until the first one).

    ``store`` is the folder for backups and history (one subfolder per document), so
    writes leave nothing next to the document but the document itself. ``text_mode``
    (``auto``, ``blocks`` or ``lines``) says how a .txt file splits into paragraphs (see
    :mod:`doc_control.ooxml.plaintext`); the ``DOC_CONTROL_TEXT_MODE`` environment
    variable sets it for the CLI and the MCP server.

    ``engine="live"`` attaches to the Word or LibreOffice window holding ``path`` (opening
    the file there when none does; ``app`` picks ``word`` or ``libreoffice``), and the
    handle then runs ``doc.batch`` and the ``live.*`` operations on that open document.
    """
    if text_mode is not None and text_mode not in TEXT_MODES:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID, f"text_mode must be one of {', '.join(TEXT_MODES)}"
        )
    if engine not in ("file", "live"):
        raise DocControlError(ErrorCode.SCHEMA_INVALID, "engine must be file or live")
    handle = OpenDocument(path, store, text_mode, engine)
    if engine == "live":
        handle.run("live.open", {"app": app} if app else {})
    return handle
