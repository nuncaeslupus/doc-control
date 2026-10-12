"""Text views: ``text.normalized``.

Two inputs, exactly one of which must be given (otherwise ``SCHEMA_INVALID``):

* ``doc`` (a ``.docx`` path), with an optional ``at`` (paragraph address or range; the
  whole document when absent). The result lists, per paragraph, its address, the
  normalised text and the offset map. For a range, the first and last paragraph are cut
  at the range offsets; ``start`` is the visible-text offset where the cut text begins,
  and the map's original offsets are relative to it (add ``start`` for paragraph offsets).
* ``text``, a string, which is normalised on its own.

The reply is capped at 8,000 bytes of JSON per call (always at least one paragraph, and
at most ``limit`` when given). When paragraphs remain, ``next`` is the cursor
``c:text.normalized:<sha8>:<index>``; pass it back with the same ``at`` to continue. A
cursor from another version of the file is ``DOC_CHANGED_ON_DISK``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex, Paragraph
from doc_control.model.normalized import NormalizedText, normalize
from doc_control.ooxml.zipio import Package
from doc_control.registry import operation

RESPONSE_CAP = 8000
_CURSOR = "c:text.normalized"


def _map(result: NormalizedText) -> dict[str, list[int]]:
    return {
        "to_original": list(result.to_original),
        "to_normalized": list(result.to_normalized),
    }


def _invalid(message: str, **details: Any) -> DocControlError:
    return DocControlError(ErrorCode.SCHEMA_INVALID, message, **details)


def _parse_cursor(cursor: str, sha8: str) -> int:
    parts = cursor.split(":")
    if len(parts) != 4 or ":".join(parts[:2]) != _CURSOR or not parts[3].isdigit():
        raise _invalid(f"malformed cursor: {cursor!r}")
    if parts[2] != sha8:
        raise DocControlError(
            ErrorCode.DOC_CHANGED_ON_DISK,
            "the cursor was issued for another version of the document",
            cursor=cursor,
        )
    return int(parts[3])


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


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


@operation(
    "text.normalized",
    "read",
    "Normalised text (NFC, soft hyphens and zero-width characters removed, whitespace "
    "collapsed) with a two-way offset map to the original code points, for a document "
    "(whole, or the paragraph/range `at`) or for a `text` string.",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "description": "Path to a .docx."},
            "text": {"type": "string", "description": "Text to normalise instead of `doc`."},
            "at": {
                "type": "string",
                "description": "Paragraph address or range, e.g. p142#a7b2a7b2:10-15.",
            },
            "limit": {"type": "integer", "minimum": 1, "description": "Most paragraphs to return."},
            "cursor": {"type": "string", "description": "`next` from a previous reply."},
        },
        "additionalProperties": False,
    },
)
def text_normalized(request: dict[str, Any]) -> dict[str, Any]:
    has_doc, has_text = "doc" in request, "text" in request
    if has_doc == has_text:
        raise _invalid("give exactly one of `doc` and `text`")
    if has_text:
        extra = sorted(k for k in ("at", "limit", "cursor") if k in request)
        if extra:
            raise _invalid(f"{', '.join(extra)} need `doc`")
        result = normalize(request["text"])
        return {"text": result.text, "offset_map": _map(result)}

    pkg = Package(Path(request["doc"]))
    index = DocumentIndex.from_package(pkg)
    sha8 = pkg.sha256[:8]
    selection = _selection(index, request.get("at"))
    begin = _parse_cursor(request["cursor"], sha8) if "cursor" in request else 0
    if begin > len(selection):
        raise _invalid(f"cursor is past the end: {request['cursor']!r}")
    limit = request.get("limit", len(selection))

    entries: list[dict[str, Any]] = []
    used = 0
    pos = begin
    while pos < len(selection) and len(entries) < limit:
        entry = _entry(*selection[pos])
        used += _size(entry) + 1
        if entries and used > RESPONSE_CAP:
            break
        entries.append(entry)
        pos += 1
    more = pos < len(selection)
    return {
        "doc": {"path": request["doc"], "sha256": pkg.sha256, "paragraphs": len(index)},
        "paragraphs": entries,
        "next": f"{_CURSOR}:{sha8}:{pos}" if more else None,
    }
