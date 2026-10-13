"""The `segment` read operation: chapters, scenes, sentences and dialogue lines.

``unit`` picks the list. Chapters and scenes come from :mod:`doc_control.model.structure`
(stable ids, the signals behind each detection, ``overrides`` and ``profile``);
sentences and dialogue lines from :mod:`doc_control.model.segment`. Lists are capped at
about 8,000 bytes and continue with `next`.
"""

from __future__ import annotations

import re
from typing import Any

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.address import FINGERPRINT_LENGTH, Address
from doc_control.model.document import Document
from doc_control.model.segment import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    dialogue_lines,
    split_sentences,
)
from doc_control.model.structure import (
    ACTIONS,
    KINDS,
    Override,
    Profile,
    Structure,
    detect_in,
)
from doc_control.ops.paging import PAGING_PROPERTIES, paged
from doc_control.registry import operation

_TEXT = 160
_SUPPRESSED_LIMIT = 20
_UNITS = ["chapters", "parts", "scenes", "sentences", "dialogue"]
_HEX = frozenset("0123456789abcdef")

PROFILE_PROPERTIES: dict[str, Any] = {
    "min_chapter_words": {
        "type": "integer",
        "minimum": 0,
        "description": (
            "Words a chapter needs after its heading; a shorter one is not a chapter "
            "(default 0). Part headings and forced chapters are exempt."
        ),
    },
    "chapter_level": {
        "type": "integer",
        "minimum": 1,
        "maximum": 9,
        "description": "Deepest heading or outline level that starts a chapter (default 1).",
    },
    "scene_blank_lines": {
        "type": "integer",
        "minimum": 0,
        "maximum": 50,
        "description": "Empty paragraphs before text that start a scene; 0 = never (default).",
    },
    "heading_styles": {
        "type": "boolean",
        "description": "Heading styles and outline levels start chapters (default true).",
    },
    "chapter_patterns": {
        "type": "boolean",
        "description": "`Capítulo 3`, `Prólogo`, numerals start chapters (default true).",
    },
    "language": {
        "type": "string",
        "enum": list(LANGUAGES),
        "description": "es (default) accepts a lone `I` as a numeral; others need more signals.",
    },
    "scene_markers": {
        "type": "boolean",
        "description": "Ornament paragraphs (`* * *`) start scenes (default true).",
    },
    "chapter_styles": {
        "type": "boolean",
        "description": (
            "A body paragraph whose style name holds a chapter word (`Normal inicio "
            "capítulo`) starts an untitled chapter (default true)."
        ),
    },
    "chapter_style_words": {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "description": (
            "Words that mark a chapter-opening style name, any case and accents "
            "(default capítulo, chapter, chapitre, kapitel, capitolo, hoofdstuk)."
        ),
    },
}


OVERRIDES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": "Force or deny a chapter or scene at paragraphs by fingerprint.",
    "items": {
        "type": "object",
        "properties": {
            "fp": {
                "type": "string",
                "minLength": FINGERPRINT_LENGTH,
                "description": "Paragraph fingerprint, 8 lower-case hex digits.",
            },
            "kind": {"type": "string", "enum": list(KINDS)},
            "action": {"type": "string", "enum": list(ACTIONS)},
            "nth": {
                "type": "integer",
                "minimum": 1,
                "description": "Only the nth paragraph with this fingerprint.",
            },
        },
        "required": ["fp", "kind", "action"],
        "additionalProperties": False,
    },
}

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Detection settings for this document.",
    "properties": PROFILE_PROPERTIES,
    "additionalProperties": False,
}


@operation(
    "segment",
    "read",
    "Chapters and scenes with stable ids and detection signals, or sentences and dialogue lines.",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            "unit": {
                "type": "string",
                "enum": _UNITS,
                "description": "What to list (default chapters).",
            },
            "chapter": {
                "type": "string",
                "minLength": 1,
                "description": "scenes only: the chapter id whose scenes to list.",
            },
            "range": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "sentences and dialogue only: what to cover — a paragraph address or "
                    "range, a run of whole paragraphs (p24#fp..p60#fp), or a chapter, "
                    "scene or part id (ch-…, sc-…, pt-…; same profile and overrides)."
                ),
            },
            "language": {
                "type": "string",
                "enum": list(LANGUAGES),
                "description": "Sentence rules (default es).",
            },
            "overrides": OVERRIDES_SCHEMA,
            "profile": PROFILE_SCHEMA,
            **PAGING_PROPERTIES,
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def segment(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    unit = request.get("unit", "chapters")
    language = request.get("language", DEFAULT_LANGUAGE)
    if unit == "sentences":
        return paged("segment", doc, request, "sentences", _sentences(doc, request, language))
    if unit == "dialogue":
        return paged("segment", doc, request, "dialogue", _dialogue(doc, request))
    found = detect_in(doc, profile_of(request), overrides_of(request))
    base: dict[str, Any] = {"unit": unit}
    if unit in ("chapters", "parts"):
        front = found.front
        base["front"] = (
            None
            if front is None
            else {
                "at": str(_address(doc, front.first)),
                "first": front.first,
                "last": front.last,
                "words": front.words,
            }
        )
    if found.unmatched_overrides:
        base["unmatched_overrides"] = [
            {
                "fp": o.fp,
                "kind": o.kind,
                "action": o.action,
                **({"nth": o.nth} if o.nth is not None else {}),
            }
            for o in found.unmatched_overrides
        ]
    kind = "scene" if unit == "scenes" else "chapter"
    shown = [s for s in found.suppressed if s[1] == kind]
    if shown:
        base["suppressed_total"] = len(shown)
        base["suppressed"] = [
            {"at": str(_address(doc, n)), "kind": k, "reason": reason}
            for n, k, reason in shown[:_SUPPRESSED_LIMIT]
        ]
    if unit == "chapters":
        return paged("segment", doc, request, "chapters", _chapters(doc, found), base)
    if unit == "parts":
        return paged("segment", doc, request, "parts", _parts(doc, found), base)
    return paged("segment", doc, request, "scenes", _scenes(doc, request, found), base)


def _address(doc: Document, number: int) -> Address:
    return doc.index.paragraph(number).address


def profile_of(request: dict[str, Any]) -> Profile:
    language = request.get("language", DEFAULT_LANGUAGE)
    settings = dict(request.get("profile", {}))
    if "chapter_style_words" in settings:
        settings["chapter_style_words"] = tuple(settings["chapter_style_words"])
    return Profile(**{"language": language, **settings})


def overrides_of(request: dict[str, Any]) -> list[Override]:
    out: list[Override] = []
    for i, raw in enumerate(request.get("overrides", [])):
        fp = raw["fp"]
        if len(fp) != FINGERPRINT_LENGTH or not set(fp) <= _HEX:
            raise DocControlError(
                ErrorCode.SCHEMA_INVALID,
                f"override fingerprint must be {FINGERPRINT_LENGTH} lower-case hex digits",
                path=f"$.overrides[{i}].fp",
            )
        out.append(Override(fp, raw["kind"], raw["action"], raw.get("nth")))
    return out


def _chapters(doc: Document, found: Structure) -> list[dict[str, Any]]:
    scenes: dict[str | None, int] = {}
    for scene in found.scenes:
        scenes[scene.chapter] = scenes.get(scene.chapter, 0) + 1
    out: list[dict[str, Any]] = []
    for chapter in found.chapters:
        para = doc.index.paragraph(chapter.first)
        out.append(
            {
                "id": chapter.id,
                "at": str(para.address),
                "kind": chapter.kind,
                "number": chapter.number,
                "text": " ".join(para.text.split())[:_TEXT],
                "first": chapter.first,
                "last": chapter.last,
                "words": chapter.words,
                "scenes": scenes.get(chapter.id, 0),
                "signals": list(chapter.signals),
                "part": chapter.part,
            }
        )
    return out


def _parts(doc: Document, found: Structure) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in found.parts:
        para = doc.index.paragraph(part.first)
        out.append(
            {
                "id": part.id,
                "at": str(para.address),
                "kind": part.kind,
                "number": part.number,
                "text": " ".join(para.text.split())[:_TEXT],
                "first": part.first,
                "last": part.last,
                "words": part.words,
                "chapters": part.chapters,
                "signals": list(part.signals),
            }
        )
    return out


def _scenes(doc: Document, request: dict[str, Any], found: Structure) -> list[dict[str, Any]]:
    wanted = request.get("chapter")
    if wanted is not None and wanted not in {c.id for c in found.chapters}:
        raise DocControlError(
            ErrorCode.ADDRESS_INVALID, f"no chapter {wanted!r} in the document", chapter=wanted
        )
    return [
        {
            "id": scene.id,
            "chapter": scene.chapter,
            "at": str(_address(doc, scene.first)),
            "first": scene.first,
            "last": scene.last,
            "words": scene.words,
            "signals": list(scene.signals),
        }
        for scene in found.scenes
        if wanted is None or scene.chapter == wanted
    ]


_UNIT_ID = re.compile(r"(?:ch|sc|pt)-[0-9a-f]{8}")


def _paragraphs_in(doc: Document, request: dict[str, Any]) -> range:
    """The paragraphs ``range`` covers: an address or range, a run of whole paragraphs
    (``p24#fp..p60#fp``), or a chapter, scene or part id (same profile and overrides)."""
    if "range" not in request:
        return range(1, len(doc.index) + 1)
    value: str = request["range"]
    if _UNIT_ID.fullmatch(value):
        found = detect_in(doc, profile_of(request), overrides_of(request))
        spans = [(u.id, u.first, u.last) for u in found.chapters]
        spans += [(u.id, u.first, u.last) for u in found.scenes]
        spans += [(u.id, u.first, u.last) for u in found.parts]
        for uid, first_n, last_n in spans:
            if uid == value:
                return range(first_n, last_n + 1)
        raise DocControlError(
            ErrorCode.ADDRESS_INVALID,
            f"no chapter, scene or part {value!r} in this document; list them with segment",
            address=value,
        )
    if ".." in value and ":" not in value:
        head, _, tail = value.partition("..")
        first = doc.index.resolve_address(head)
        last = doc.index.resolve_address(tail)
        if last.number < first.number:
            raise DocControlError(
                ErrorCode.ADDRESS_INVALID, f"{value!r} ends before it starts", address=value
            )
        return range(first.number, last.number + 1)
    resolved = doc.index.resolve_range(value, request.get("language", DEFAULT_LANGUAGE))
    return range(resolved.first.number, resolved.last.number + 1)


def _sentences(doc: Document, request: dict[str, Any], language: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for number in _paragraphs_in(doc, request):
        para = doc.index.paragraph(number)
        for k, span in enumerate(split_sentences(para.text, language), 1):
            out.append(
                {
                    "at": f"{para.address}.s{k}",
                    "start": span.start,
                    "end": span.end,
                    "text": span.text(para.text)[:_TEXT],
                }
            )
    return out


def _dialogue(doc: Document, request: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for number in _paragraphs_in(doc, request):
        para = doc.index.paragraph(number)
        for line in dialogue_lines(para.text):
            out.append(
                {
                    "at": str(para.address),
                    "start": line.start,
                    "end": line.end,
                    "parts": [
                        {"kind": part.kind, "start": part.start, "end": part.end}
                        for part in line.parts
                    ],
                }
            )
    return out
