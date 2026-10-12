"""Text views: ``text.normalized``.

Two inputs, exactly one of which must be given (otherwise ``SCHEMA_INVALID``):

* ``doc`` (a ``.docx`` path), with an optional ``at`` (paragraph address or range; the
  whole document when absent). The result lists, per paragraph, its address, the
  normalised text and the offset map. For a range, the first and last paragraph are cut
  at the range offsets; ``start`` is the visible-text offset where the cut text begins,
  and the map's original offsets are relative to it (add ``start`` for paragraph offsets).
* ``text``, a string, which is normalised on its own.

The reply is paged like every list (:mod:`doc_control.ops.paging`): at most about 8,000
bytes and ``limit`` paragraphs per call, ``next`` holding the cursor to continue with the
same ``at``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, overload

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.model.index import DocumentIndex, Paragraph
from doc_control.model.normalized import NormalizedText, normalize
from doc_control.ops.paging import PAGING_PROPERTIES, paged
from doc_control.registry import operation

def _map(result: NormalizedText) -> dict[str, list[int]]:
    return {
        "to_original": list(result.to_original),
        "to_normalized": list(result.to_normalized),
    }


def _invalid(message: str, **details: Any) -> DocControlError:
    return DocControlError(ErrorCode.SCHEMA_INVALID, message, **details)


def _selection(index: DocumentIndex, at: str | None) -> list[tuple[Paragraph, int, int]]:
    """Paragraphs to report with the cut ``[start, end)`` of each."""
    if at is None:
        return [(p, 0, len(p.text)) for p in index.paragraphs]
    resolved = index.resolve_range(at)
    first, last = resolved.first, resolved.last
    start = resolved.start_offset or 0
    end = len(last.text) if resolved.end_offset is None else resolved.end_offset
    if first is last:
        return [(first, start, end)]
    middle = index.paragraphs[first.number : last.number - 1]
    return [
        (first, start, len(first.text)),
        *((p, 0, len(p.text)) for p in middle),
        (last, 0, end),
    ]


def _entry(para: Paragraph, start: int, end: int) -> dict[str, Any]:
    result = normalize(para.text[start:end])
    return {
        "at": str(para.address),
        "start": start,
        "text": result.text,
        "offset_map": _map(result),
    }


class _Entries(Sequence[dict[str, Any]]):
    """Entries built on demand, so a page costs only the paragraphs it shows."""

    def __init__(self, selection: list[tuple[Paragraph, int, int]]) -> None:
        self._selection = selection

    def __len__(self) -> int:
        return len(self._selection)

    @overload
    def __getitem__(self, item: int) -> dict[str, Any]: ...
    @overload
    def __getitem__(self, item: slice) -> list[dict[str, Any]]: ...
    def __getitem__(self, item: int | slice) -> dict[str, Any] | list[dict[str, Any]]:
        if isinstance(item, slice):
            return [_entry(*row) for row in self._selection[item]]
        return _entry(*self._selection[item])


@operation(
    "text.normalized",
    "read",
    "Normalised text (NFC, soft hyphens and zero-width characters removed, whitespace "
    "collapsed) with a two-way offset map to the original code points, for a document "
    "(whole, or the paragraph/range `at`) or for a `text` string.",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to a .docx."},
            "text": {"type": "string", "description": "Text to normalise instead of `doc`."},
            "at": {
                "type": "string",
                "description": "Paragraph address or range, e.g. p142#a7b2a7b2:10-15.",
            },
            **PAGING_PROPERTIES,
        },
        "additionalProperties": False,
    },
)
def text_normalized(request: dict[str, Any]) -> dict[str, Any] | Reply:
    has_doc, has_text = "doc" in request, "text" in request
    if has_doc == has_text:
        raise _invalid("give exactly one of `doc` and `text`")
    if has_text:
        extra = sorted(k for k in ("at", "limit", "cursor") if k in request)
        if extra:
            raise _invalid(f"{', '.join(extra)} need `doc`")
        result = normalize(request["text"])
        return {"text": result.text, "offset_map": _map(result)}
    doc = Document(request["doc"])
    selection = _selection(doc.index, request.get("at"))
    return paged("text.normalized", doc, request, "paragraphs", _Entries(selection))
