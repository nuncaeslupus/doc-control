"""Text views: ``text.normalized``."""

from __future__ import annotations

from typing import Any

from doc_control.model.normalized import normalize
from doc_control.registry import operation


@operation(
    "text.normalized",
    "read",
    "Normalised text (NFC, soft hyphens and zero-width characters removed, whitespace "
    "collapsed) with a two-way offset map to the original code points.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Visible text to normalise."},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
)
def text_normalized(request: dict[str, Any]) -> dict[str, Any]:
    result = normalize(request["text"])
    return {
        "text": result.text,
        "offset_map": {
            "to_original": list(result.to_original),
            "to_normalized": list(result.to_normalized),
        },
    }
