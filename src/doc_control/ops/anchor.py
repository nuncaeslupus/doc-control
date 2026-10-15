"""`anchor.create` and `anchor.resolve`: quote anchors that survive edits.

The matching rules and the score are in :mod:`doc_control.model.anchor`. Both operations
only read the document.
"""

from __future__ import annotations

from typing import Any

from doc_control import outputs
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.anchor import (
    CONTEXT_CHARS,
    MAX_CONTEXT_CHARS,
    MAX_QUOTE_CHARS,
    MIN_SCORE,
    Anchor,
    Candidate,
    create,
    resolve,
    sentence_of,
)
from doc_control.model.document import Document
from doc_control.registry import operation

ANCHOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "exact": {
            "type": "string",
            "minLength": 1,
            "description": f"The quoted text (at most {MAX_QUOTE_CHARS} characters).",
        },
        "prefix": {"type": "string", "description": "Text just before the quote."},
        "suffix": {"type": "string", "description": "Text just after the quote."},
        "hint": {
            "type": "string",
            "description": "Paragraph the quote was in, like `p142`; the nearer, the better.",
        },
        "fingerprint": {"type": "string", "description": "That paragraph's fingerprint then."},
        "at": {"type": "string", "description": "The range at creation (informative)."},
        "sentence": {"type": "string", "description": "The sentence address at creation."},
    },
    "required": ["exact"],
    "additionalProperties": False,
}


_CREATE_OUTPUT = outputs.result(
    ("anchor",),
    anchor=outputs.obj(
        "The anchor: pass it whole to anchor.resolve later.",
        ("exact", "prefix", "suffix"),
        exact=outputs.string("The quoted text."),
        prefix=outputs.string("Text just before it."),
        suffix=outputs.string("Text just after it."),
        hint=outputs.string("Paragraph number then, as p<n>."),
        fingerprint=outputs.string("That paragraph's fingerprint then."),
        at=outputs.RANGE,
        sentence=outputs.string("Sentence address when the range was a sentence."),
    ),
)


@operation(
    "anchor.create",
    "read",
    (
        "Make an anchor for a passage so it can be found again after edits shift or change "
        "addresses: the quoted text, `context` characters before and after (default 32, max 200), "
        "and the paragraph's number and fingerprint then. `at` is a range in one paragraph "
        "(p142#a7b2a7b2:10-15), a whole paragraph (p142#a7b2a7b2) or a sentence "
        "(p142#a7b2a7b2.s3). Use it to bookmark passages you will come back to; find them later "
        "with anchor.resolve (text.find searches for any text instead). Read-only; STALE_ADDRESS "
        "when `at` is outdated, SCHEMA_INVALID for a range across paragraphs."
    ),
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx file."},
            "at": {
                "type": "string",
                "minLength": 1,
                "description": "Range inside one paragraph, e.g. p142#a7b2a7b2:10-15,"
                " or a whole paragraph or sentence (p142#a7b2a7b2.s3).",
            },
            "context": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_CONTEXT_CHARS,
                "default": CONTEXT_CHARS,
                "description": "Characters of prefix and of suffix to keep.",
            },
        },
        "required": ["doc", "at"],
        "additionalProperties": False,
    },
    output=_CREATE_OUTPUT,
)
def anchor_create(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    anchor = create(doc.index, request["at"], request.get("context", CONTEXT_CHARS))
    return Reply({"anchor": anchor.to_dict()}, doc=doc.envelope_doc())


_RESOLVE_OUTPUT = outputs.result(
    ("at", "score", "match"),
    at=outputs.RANGE,
    score=outputs.number("Match score, 0-1."),
    match=outputs.string("exact, normalized or fuzzy."),
    match_level=outputs.string("Strictest fold level at which the quote equals the matched text."),
    matched_text=outputs.string("The text matched, when not character for character."),
    sentence=outputs.string("Sentence address, for a sentence anchor."),
)


@operation(
    "anchor.resolve",
    "read",
    (
        "Find an anchor's passage in the document as it is now: its current range, a 0-1 score and "
        "the match kind (exact, normalized or fuzzy). Pass the `anchor` object anchor.create "
        "returned, or just {exact, prefix, suffix}. mode best (default) falls back to a fuzzy "
        "match scoring at least 0.80; mode exact accepts only verbatim, normalised or folded "
        "(match_level, default `default`: case, accents, punctuation) matches. Fails with "
        "ANCHOR_NOT_FOUND, or ANCHOR_AMBIGUOUS listing up to 5 tied candidates. Use it to "
        "re-address a passage after edits; doc.diff maps addresses between two files instead. "
        "Read-only."
    ),
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx file."},
            "anchor": {
                **ANCHOR_SCHEMA,
                "description": "An anchor from anchor.create, or just {exact, prefix, suffix}.",
            },
            "mode": {
                "enum": ["best", "exact"],
                "default": "best",
                "description": "best: verbatim matches first, then fuzzy ones (match says which)."
                " exact: only places holding the quote verbatim or, when none does, in the"
                " normalised text (soft hyphens and zero-width characters ignored, whitespace"
                " collapsed, as text.normalize) or at a fold level of match_level; anything"
                " else is ANCHOR_NOT_FOUND.",
            },
            "match_level": {
                "enum": ["exact", "default", "loose"],
                "default": "default",
                "description": "How far the quote is folded when neither it nor its"
                " normalised text occurs. exact: today's rule (verbatim, then normalised, then"
                " fuzzy in mode best). default: then ignoring case, accents and punctuation"
                " (case, then default level), and failing that symbols too (loose). loose: the"
                " same ladder. The reply's match_level is the strictest level at which the"
                " quote equals the matched text (exact: character for character; case:"
                " normalised text with case ignored), with matched_text when it is not exact;"
                " a fuzzy match has neither. Not the reply's match kind `match` (exact,"
                " normalized, fuzzy), which keeps its meaning.",
            },
        },
        "required": ["doc", "anchor"],
        "additionalProperties": False,
    },
    output=_RESOLVE_OUTPUT,
)
def anchor_resolve(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    anchor = Anchor.from_dict(request["anchor"])
    exact_only = request.get("mode", "best") == "exact"
    match_level = request.get("match_level", "default")
    outcome = resolve(doc.index, anchor, exact_only=exact_only, match_level=match_level)
    best = outcome.best
    if outcome.status == "ambiguous":
        raise DocControlError(
            ErrorCode.ANCHOR_AMBIGUOUS,
            f"{len(outcome.candidates)} places match the anchor about equally well",
            candidates=[
                _levelled(doc, c, {**c.to_dict(), "match": c.kind}) for c in outcome.candidates
            ],
            mode="exact" if exact_only else "best",
        )
    if best is None or outcome.status == "not_found":
        raise DocControlError(
            ErrorCode.ANCHOR_NOT_FOUND,
            (
                "the quote occurs neither verbatim nor in the normalised text"
                + ("" if match_level == "exact" else " nor at any fold level")
                if exact_only
                else f"no place scores {MIN_SCORE:.2f} or more against the anchor"
            ),
            min_score=MIN_SCORE,
            mode="exact" if exact_only else "best",
            best_score=round(best.score, 4) if best is not None else 0.0,
            **({"best_at": best.at} if best is not None else {}),
        )
    result = _levelled(
        doc, best, {"at": best.at, "score": round(best.score, 4), "match": best.kind}
    )
    if anchor.sentence is not None:
        result["sentence"] = sentence_of(doc.index, best)
    return Reply(result, doc=doc.envelope_doc())


def _levelled(doc: Document, candidate: Candidate, out: dict[str, Any]) -> dict[str, Any]:
    """``out`` with the candidate's ``match_level``, and its ``matched_text`` when not exact."""
    if candidate.level is not None:
        out["match_level"] = candidate.level
        if candidate.level != "exact":
            text = doc.index.paragraphs[candidate.number - 1].text
            out["matched_text"] = text[candidate.start : candidate.end]
    return out
