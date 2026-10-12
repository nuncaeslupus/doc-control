"""The response envelope every operation returns."""

from __future__ import annotations

from typing import Any

from doc_control import API_VERSION
from doc_control.errors import DocControlError


def success(
    op: str,
    result: dict[str, Any],
    *,
    doc: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"api": API_VERSION, "ok": True, "op": op}
    if doc is not None:
        out["doc"] = doc
    out["result"] = result
    out["warnings"] = warnings or []
    out["next"] = next_cursor
    return out


def failure(op: str, error: DocControlError) -> dict[str, Any]:
    return {"api": API_VERSION, "ok": False, "op": op, "error": error.to_dict()}
