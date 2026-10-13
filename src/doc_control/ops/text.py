"""Text views: ``text.normalized``.

Two inputs, exactly one of which must be given (otherwise ``SCHEMA_INVALID``):

* ``doc`` (a ``.docx`` path), with an optional ``at`` (paragraph address or range; the
  whole document when absent). The result lists, per paragraph, its address, the
  normalised text and the offset map. For a range, the first and last paragraph are cut
  at the range offsets; ``start`` is the visible-text offset where the cut text begins,
  and the map's original offsets are relative to it (add ``start`` for paragraph offsets).
* ``text``, a string, which is normalised on its own (offsets count from its start).

The reply is paged like every list (:mod:`doc_control.ops.paging`): at most about 8,000
bytes and ``limit`` paragraphs per call, ``next`` holding the cursor to continue with the
same ``at`` (or the same ``text``). A paragraph whose map would be large is split
into several entries (see the result description in the operation's schema), so a reply
stays within the cap even for one long paragraph.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from doc_control.envelope import Reply, success
from doc_control.errors import DocControlError, ErrorCode
from doc_control.inprocess import budget
from doc_control.inprocess import limit as list_limit
from doc_control.model.document import Document
from doc_control.model.index import DocumentIndex, Paragraph
from doc_control.model.normalized import NormalizedText, encode_runs, normalize
from doc_control.ops.paging import (
    MAX_LIMIT,
    PAGING_PROPERTIES,
    RESPONSE_BUDGET,
    make_cursor,
    read_cursor,
    size,
)
from doc_control.registry import operation


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


WINDOW_BYTES = 3000  # an entry bigger than this is split, so one never nears the 8,000 cap


def _window(
    result: NormalizedText, start: int, at: str | None, a: int, b: int, last: bool
) -> list[dict[str, Any]]:
    """Entries for normalised boundaries ``[a, b)`` (``[a, len]`` for the last window).

    A window lists the original boundaries from ``to_original[a]`` up to (not including)
    ``to_original[b]``, so the windows of a paragraph partition both maps. It is split in
    halves while its JSON is over :data:`WINDOW_BYTES`.
    """
    n = len(result.text)
    top = n + 1 if last else b
    o_lo = result.to_original[a]
    o_hi = result.original_length + 1 if last else result.to_original[b]
    entry: dict[str, Any] = {} if at is None else {"at": at}
    entry |= {
        "start": start,
        "from": a,
        "text": result.text[a : n if last else b],
        "offset_map": {
            "to_original": encode_runs(result.to_original, a, top),
            "to_normalized": encode_runs(result.to_normalized, o_lo, o_hi),
        },
    }
    size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
    if size <= WINDOW_BYTES or b - a < 2:
        return [entry]
    mid = (a + b) // 2
    return _window(result, start, at, a, mid, False) + _window(result, start, at, mid, b, last)


def _entries(text: str, start: int, at: str | None) -> list[dict[str, Any]]:
    result = normalize(text)
    return _window(result, start, at, 0, len(result.text), True)


#: A cursor offset is ``paragraph * _STRIDE + entry``: the paragraph (row) to resume at and
#: which of its entries comes first, so a page builds only the paragraphs it shows.
_STRIDE = 1_000_000

Row = tuple[str, int, str | None]  # text, start, address


def _page(op: str, doc: Document, request: dict[str, Any], rows: Sequence[Row]) -> Reply:
    """Like :func:`doc_control.ops.paging.paged`, but builds entries only as the page needs."""
    row, skip = divmod(read_cursor(op, doc, request), _STRIDE)
    if row > len(rows) or (row == len(rows) and rows):
        raise _invalid("cursor is past the end", path="$.cursor")
    limit = list_limit(request, MAX_LIMIT)
    head: dict[str, Any] = {"total": len(rows), "paragraphs": []}
    placeholder = make_cursor(op, doc, request, 10**12)
    room = budget(RESPONSE_BUDGET) - size(
        success(op, head, doc=doc.envelope_doc(), next_cursor=placeholder)
    )
    taken: list[dict[str, Any]] = []
    used = 0
    resume: int | None = None
    while row < len(rows) and resume is None:
        entries = _entries(*rows[row])
        for k in range(skip, len(entries)):
            cost = len(json.dumps(entries[k], ensure_ascii=False).encode("utf-8")) + 2
            if taken and (used + cost > room or len(taken) >= limit):
                resume = row * _STRIDE + k
                break
            taken.append(entries[k])
            used += cost
        row, skip = row + 1, 0
    head["paragraphs"] = taken
    return Reply(
        head,
        doc=doc.envelope_doc(),
        next_cursor=None if resume is None else make_cursor(op, doc, request, resume),
    )


class _TextSource(Document):
    """Stands in for a document so a `text` request pages and takes cursors the same way."""

    def __init__(self, text: str) -> None:
        self.path = "-"
        self._sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self._length = len(text)

    @property
    def sha256(self) -> str:
        return self._sha256

    def envelope_doc(self) -> dict[str, Any]:
        return {"source": "text", "sha256": self._sha256, "characters": self._length}


_RESULT = (
    "Result: `paragraphs`, each {at (absent for `text`), start, from, text, offset_map}. "
    "`start` is where the entry's text begins in the paragraph's visible text (non-zero "
    "for a range cut); a long paragraph comes as several entries, `from` being the "
    "normalised offset where each entry's `text` begins. offset_map.to_original maps "
    "normalised offsets to original ones and offset_map.to_normalized the reverse, both "
    "counted in code points from `start`, as runs: [index, value, count] means entry "
    "index+k is value+k for k < count, and [index, value, count, 0] means entry index+k "
    "is value. Each entry lists its own normalised offsets (to_original) and original "
    "offsets (to_normalized); the entries of a paragraph together list every offset once."
)


OP = "text.normalized"


@operation(
    OP,
    "read",
    "Normalised text (NFC, soft hyphens and zero-width characters removed, whitespace "
    "collapsed) with a two-way offset map to the original code points, for a document "
    "(whole, or the paragraph/range `at`) or for a `text` string.",
    {
        "type": "object",
        "description": _RESULT,
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
        if "at" in request:
            raise _invalid("`at` needs `doc`")
        return _page(OP, _TextSource(request["text"]), request, [(request["text"], 0, None)])
    doc = Document(request["doc"])
    rows = [
        (para.text[start:end], start, str(para.address))
        for para, start, end in _selection(doc.index, request.get("at"))
    ]
    return _page(OP, doc, request, rows)
