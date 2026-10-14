"""Stable error codes, each with a one-line hint on what to do next."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    DOC_NOT_FOUND = "DOC_NOT_FOUND"
    NOT_A_DOCX = "NOT_A_DOCX"
    DOC_LOCKED = "DOC_LOCKED"
    DOC_CHANGED_ON_DISK = "DOC_CHANGED_ON_DISK"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    ADDRESS_INVALID = "ADDRESS_INVALID"
    STALE_ADDRESS = "STALE_ADDRESS"
    ANCHOR_NOT_FOUND = "ANCHOR_NOT_FOUND"
    ANCHOR_AMBIGUOUS = "ANCHOR_AMBIGUOUS"
    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
    BATCH_CONFLICT = "BATCH_CONFLICT"
    VERIFY_FAILED_RESTORED = "VERIFY_FAILED_RESTORED"
    COMMENT_NOT_FOUND = "COMMENT_NOT_FOUND"
    STYLE_NOT_FOUND = "STYLE_NOT_FOUND"
    STYLE_CONFLICT = "STYLE_CONFLICT"
    RECIPE_INVALID = "RECIPE_INVALID"
    HISTORY_NOT_FOUND = "HISTORY_NOT_FOUND"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    DOC_AMBIGUOUS = "DOC_AMBIGUOUS"
    UNSAVED_CHANGES = "UNSAVED_CHANGES"
    INTERNAL = "INTERNAL"


HINTS: dict[ErrorCode, str] = {
    ErrorCode.DOC_NOT_FOUND: "Check the path; it is resolved relative to the current directory.",
    ErrorCode.NOT_A_DOCX: (
        "Pass a Word .docx file (or .txt/.md/.odt, read operations only); .doc and other"
        " formats are not supported."
    ),
    ErrorCode.DOC_LOCKED: "An editor (Word, LibreOffice) has the file open: close it, then retry.",
    ErrorCode.DOC_CHANGED_ON_DISK: "The file changed since you read it: re-read, then retry.",
    ErrorCode.SCHEMA_INVALID: "Run `doc-control help <op>` to see the request schema.",
    ErrorCode.ADDRESS_INVALID: "Use an address from a read result, e.g. p142#a7b2c3d4.",
    ErrorCode.STALE_ADDRESS: "Re-read the paragraph (para.get) or resolve its anchor, then retry.",
    ErrorCode.ANCHOR_NOT_FOUND: "Widen the quote or its context, or search with find.",
    ErrorCode.ANCHOR_AMBIGUOUS: "Pick one of the candidates, or add prefix/suffix context.",
    ErrorCode.UNSUPPORTED_CONTENT: (
        "Edit around fields, hyperlinks or controls; encrypted or ZIP64 packages are not"
        " supported: save a normal copy in Word."
    ),
    ErrorCode.BATCH_CONFLICT: "Split overlapping operations into separate batches.",
    ErrorCode.VERIFY_FAILED_RESTORED: "Nothing was changed; report the request that caused it.",
    ErrorCode.COMMENT_NOT_FOUND: "List comments with comments.list and use an existing id.",
    ErrorCode.STYLE_NOT_FOUND: "List styles with styles.list and use an existing id.",
    ErrorCode.STYLE_CONFLICT: "Rename the style or merge it with style.merge.",
    ErrorCode.RECIPE_INVALID: "Fix the recipe so it only uses the documented keys.",
    ErrorCode.HISTORY_NOT_FOUND: "List history with history.list and use an existing op id.",
    ErrorCode.ENGINE_UNAVAILABLE: (
        "details.reason says why; live.status lists the apps. Without one, use the file engine."
    ),
    ErrorCode.DOC_AMBIGUOUS: "Several open documents have that name: pass the full path, one "
    "of details.candidates.",
    ErrorCode.UNSAVED_CHANGES: "Pass save: true to keep the changes or discard: true to drop them.",
    ErrorCode.INTERNAL: "Unexpected error; nothing was written. Report it with the request.",
}


class DocControlError(Exception):
    """An error with a stable code; the CLI turns it into an error envelope."""

    def __init__(self, code: ErrorCode, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "hint": HINTS[self.code],
        }
        if self.details:
            out["details"] = self.details
        return out
