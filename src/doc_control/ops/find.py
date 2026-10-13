"""`find`: literal or regex search, returned as addressable ranges.

Each match is ``{"at": "p142#a7b2a7b2:10-15", "text": ..., "context": ...}``: the range
is a paragraph address plus code-point offsets into the paragraph's visible text.
Output is capped by ``limit``; when more matches exist the envelope's ``next`` is a
cursor that continues the same search (it only works on the same document bytes and
the same query options). Regex search runs in a child process killed at the time
budget (Python's regex engine cannot be interrupted); a literal search checks the budget
between paragraphs. A search that outlives the budget returns what it found with warning
``TIME_BUDGET`` and a cursor at the unfinished paragraph; if not even that paragraph
could be searched, ``SCHEMA_INVALID`` asks for a simpler pattern.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.inprocess import limit as list_limit
from doc_control.model.address import Address
from doc_control.model.document import Document
from doc_control.model.search import IsolatedScan, LocalScan, Matcher, SearchOptions
from doc_control.model.wordfind import word_find
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
    "Find text or a regex; matches come back as paragraph ranges, capped, with a cursor. "
    "The first page also gives `total` (or `total_at_least` and `more: true` when the time "
    "budget stopped the count). `query_word` is the query as typed in Word's Find box (^p, ^l, "
    "^t, ^s, ^^; null past 255 characters, with ignore_accents or with a soft or no-break "
    "hyphen in the query; not for a regex), "
    "with `query_word_options` (match_case, whole_words) to tick there.",
    SCHEMA,
)
def find(request: dict[str, Any]) -> Reply:
    options = SearchOptions(
        query=request["query"],
        regex=request.get("regex", False),
        ignore_case=request.get("ignore_case", False),
        ignore_accents=request.get("ignore_accents", False),
        whole_word=request.get("whole_word", False),
    )
    matcher = Matcher(options)
    limit: int = list_limit(request, DEFAULT_LIMIT)
    context: int = request.get("context", DEFAULT_CONTEXT)
    shared = Document(request["doc"])  # one parse per open document (doc_control.api)
    pkg, index = shared.pkg, shared.index
    token = _token(pkg.sha256, options)
    first, skip = _parse_cursor(request.get("cursor"), token)

    if first > len(index):
        raise _bad_cursor()
    items = [(p.number, p.text) for p in index.paragraphs[first - 1 :] if p.text]
    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    scan: LocalScan | IsolatedScan
    # Regexes run in a child process that can be killed; literals cannot backtrack.
    scan = (
        IsolatedScan(options, items, deadline)
        if options.regex
        else LocalScan(matcher, items, deadline)
    )
    matches: list[dict[str, Any]] = []
    warnings: list[str] = []
    next_cursor: str | None = None
    remaining = 0  # matches after this page, counted so the reply can give a total
    with contextlib.closing(scan.__iter__()) as found:
        for number, spans in found:
            para = index.paragraph(number)
            skipping = skip if number == first else 0
            for k, (start, end) in enumerate(spans):
                if k < skipping:
                    continue
                if len(matches) == limit:
                    if next_cursor is None:
                        next_cursor = f"c:find:{token}:{number}:{k}"
                    if "cursor" in request:
                        break  # a later page gives no total, so it need not count
                    remaining += 1
                    continue
                matches.append(_match(para.address, para.text, start, end, context))
            if next_cursor is not None and "cursor" in request:
                break
    stopped = scan.stopped_at
    counted = len(matches) + remaining
    if stopped is not None and next_cursor is None:
        if stopped == first and not matches:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                f"search of p{stopped} did not finish within {TIME_BUDGET_SECONDS:g}s;"
                " simplify the pattern (avoid nested or overlapping repeats)",
                path="$.query",
                paragraph=stopped,
            )
        warnings.append(
            f"TIME_BUDGET: stopped at p{stopped} after {TIME_BUDGET_SECONDS:g}s;"
            " continue with `next`"
        )
        next_cursor = f"c:find:{token}:{stopped}:0"
    # The document's total, on the first page only (a later page starts mid-document).
    total: dict[str, Any] = {}
    if "cursor" not in request:
        total = (
            {"total_at_least": counted, "more": True} if stopped is not None else {"total": counted}
        )
    # The query as typed in Word's Find box, with the options that make Word's Find meet the
    # same text (not for a regex: Word's wildcards differ; null when accents are ignored:
    # Word's Find has no such option).
    word: dict[str, Any] = {}
    if not request.get("regex"):
        same = not request.get("ignore_accents")
        word["query_word"] = word_find(request["query"]) if same else None
        word["query_word_options"] = {
            "match_case": not request.get("ignore_case", False),
            "whole_words": bool(request.get("whole_word", False)),
        }
    return Reply(
        {"matches": matches, **total, **word},
        doc={
            "path": request["doc"],
            "sha256": pkg.sha256,
            "paragraphs": len(index),
            **({"source": pkg.text_source} if pkg.text_source is not None else {}),
        },
        warnings=warnings,
        next_cursor=next_cursor,
    )


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
        raise _bad_cursor()
    return int(m[2]), int(m[3])


def _bad_cursor() -> DocControlError:
    return DocControlError(
        ErrorCode.SCHEMA_INVALID,
        "cursor does not belong to this document and query; repeat the search without it",
        path="$.cursor",
    )
