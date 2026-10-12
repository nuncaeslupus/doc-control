"""The `segment` read operation: chapters, scenes, sentences and dialogue lines.

``unit`` picks the list. Chapters and scenes come from :mod:`doc_control.model.structure`
(stable ids, the signals behind each detection, ``overrides`` and ``profile``);
sentences and dialogue lines from :mod:`doc_control.model.segment`. Lists are capped at
about 8,000 bytes and continue with `next`.
"""

from __future__ import annotations

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
    detect,
    paragraphs_of,
)
from doc_control.ops.paging import PAGING_PROPERTIES, paged
from doc_control.registry import operation

_TEXT = 160
_SUPPRESSED_LIMIT = 20
_UNITS = ["chapters", "scenes", "sentences", "dialogue"]
_HEX = frozenset("0123456789abcdef")

_PROFILE_PROPERTIES: dict[str, Any] = {
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
                "description": "sentences and dialogue only: paragraph address or range to cover.",
            },
            "language": {
                "type": "string",
                "enum": list(LANGUAGES),
                "description": "Sentence rules (default es).",
            },
            "overrides": {
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
            },
            "profile": {
                "type": "object",
                "description": "Detection settings for this document.",
                "properties": _PROFILE_PROPERTIES,
                "additionalProperties": False,
            },
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
    found = detect(paragraphs_of(doc), _profile(request), _overrides(request))
    base: dict[str, Any] = {"unit": unit}
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
    kind = "chapter" if unit == "chapters" else "scene"
    shown = [s for s in found.suppressed if s[1] == kind]
    if shown:
        base["suppressed_total"] = len(shown)
        base["suppressed"] = [
            {"at": str(_address(doc, n)), "kind": k, "reason": reason}
            for n, k, reason in shown[:_SUPPRESSED_LIMIT]
        ]
    if unit == "chapters":
        return paged("segment", doc, request, "chapters", _chapters(doc, found), base)
    return paged("segment", doc, request, "scenes", _scenes(doc, request, found), base)


def _address(doc: Document, number: int) -> Address:
    return doc.index.paragraph(number).address


def _profile(request: dict[str, Any]) -> Profile:
    language = request.get("language", DEFAULT_LANGUAGE)
    return Profile(**{"language": language, **request.get("profile", {})})


def _overrides(request: dict[str, Any]) -> list[Override]:
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


def _paragraphs_in(doc: Document, request: dict[str, Any]) -> range:
    if "range" not in request:
        return range(1, len(doc.index) + 1)
    resolved = doc.index.resolve_range(request["range"], request.get("language", DEFAULT_LANGUAGE))
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
