"""`find`: literal or regex search, returned as addressable ranges.

Each match is ``{"at": "p142#a7b2a7b2:10-15", "text": ..., "context": ...}``: the range
is a paragraph address plus code-point offsets into the paragraph's visible text.
Output is capped by ``limit``; when more matches exist the envelope's ``next`` is a
cursor that continues the same search (it only works on the same document bytes and
the same query options). A search that outlives its time budget stops between
paragraphs with warning ``TIME_BUDGET`` and a cursor.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import Address
from doc_control.model.index import DocumentIndex
from doc_control.model.search import Matcher, SearchOptions
from doc_control.ooxml.zipio import Package
from doc_control.registry import operation

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
DEFAULT_CONTEXT = 30
MAX_CONTEXT = 200
MAX_TEXT = 200  # characters of a match echoed back; longer matches are cut
TIME_BUDGET_SECONDS = 10.0

_CURSOR = re.compile(r"c:find:([0-9a-f]{8}):([1-9][0-9]{0,11}):(0|[1-9][0-9]{0,11})")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx file."},
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "Text to find (at most 1000 characters), or a Python regex if `regex`.",
        },
        "regex": {"type": "boolean", "default": False, "description": "Treat `query` as a regex."},
        "ignore_case": {"type": "boolean", "default": False, "description": "Ignore case."},
        "ignore_accents": {
            "type": "boolean",
            "default": False,
            "description": "Ignore accents: `maria` finds `María`. A regex must be written"
            " without accents then.",
        },
        "whole_word": {
            "type": "boolean",
            "default": False,
            "description": "Match only whole words (no letter, digit or mark on either side).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_LIMIT,
            "description": "Most matches to return; more are reached with `cursor`.",
        },
        "context": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_CONTEXT,
            "default": DEFAULT_CONTEXT,
            "description": "Characters of surrounding text on each side of a match.",
        },
        "cursor": {"type": "string", "description": "The `next` value of the previous page."},
    },
    "required": ["doc", "query"],
    "additionalProperties": False,
}


@operation(
    "find",
    "read",
    "Find text or a regex; matches come back as paragraph ranges, capped, with a cursor.",
    SCHEMA,
)
def find(request: dict[str, Any]) -> dict[str, Any]:
    options = SearchOptions(
        query=request["query"],
        regex=request.get("regex", False),
        ignore_case=request.get("ignore_case", False),
        ignore_accents=request.get("ignore_accents", False),
        whole_word=request.get("whole_word", False),
    )
    matcher = Matcher(options)
    limit: int = request.get("limit", DEFAULT_LIMIT)
    context: int = request.get("context", DEFAULT_CONTEXT)
    pkg = Package(Path(request["doc"]))
    index = DocumentIndex.from_package(pkg)
    token = _token(pkg.sha256, options)
    first, skip = _parse_cursor(request.get("cursor"), token)

    matches: list[dict[str, Any]] = []
    warnings: list[str] = []
    next_cursor: str | None = None
    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    searched = False
    for para in index.paragraphs[first - 1 :]:
        if searched and time.monotonic() > deadline:
            warnings.append(
                f"TIME_BUDGET: stopped before p{para.number} after {TIME_BUDGET_SECONDS:g}s;"
                " continue with `next`"
            )
            next_cursor = f"c:find:{token}:{para.number}:0"
            break
        searched = True
        skipping = skip if para.number == first else 0
        for k, (start, end) in enumerate(matcher.spans(para.text)):
            if k < skipping:
                continue
            if len(matches) == limit:
                next_cursor = f"c:find:{token}:{para.number}:{k}"
                break
            matches.append(_match(para.address, para.text, start, end, context))
        if next_cursor is not None:
            break
    return {
        "doc": {"path": request["doc"], "sha256": pkg.sha256, "paragraphs": len(index)},
        "matches": matches,
        "warnings": warnings,
        "next": next_cursor,
    }


def _match(address: Address, text: str, start: int, end: int, context: int) -> dict[str, Any]:
    lo, hi = max(0, start - context), min(len(text), end + context)
    shown = text[start : min(end, start + MAX_TEXT)]
    out: dict[str, Any] = {
        "at": f"{address}:{start}-{end}",
        "text": shown,
        "context": ("…" if lo > 0 else "") + text[lo:hi] + ("…" if hi < len(text) else ""),
    }
    if end - start > MAX_TEXT:
        out["text_truncated"] = True
        out["context"] = ("…" if lo > 0 else "") + text[lo:start] + shown + "…"
    return out


def _token(sha256: str, options: SearchOptions) -> str:
    """Ties a cursor to one document and one query, so it cannot resume a different search."""
    key = json.dumps([sha256, *vars(options).values()], ensure_ascii=True)
    return hashlib.blake2s(key.encode("ascii")).hexdigest()[:8]


def _parse_cursor(cursor: str | None, token: str) -> tuple[int, int]:
    if cursor is None:
        return 1, 0
    m = _CURSOR.fullmatch(cursor)
    if m is None or m[1] != token:
        raise DocControlError(
            ErrorCode.SCHEMA_INVALID,
            "cursor does not belong to this document and query; repeat the search without it",
            path="$.cursor",
        )
    return int(m[2]), int(m[3])
