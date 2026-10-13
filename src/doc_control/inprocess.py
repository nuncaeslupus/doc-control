"""Switches for in-process callers (:mod:`doc_control.api`).

The response caps and default list limits exist for LLM-facing transports (CLI, MCP), whose
replies land in a context window. A Python caller gets whole results instead: inside
:func:`uncapped`, :func:`budget` and :func:`limit` stop limiting.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_UNCAPPED: ContextVar[bool] = ContextVar("doc_control_uncapped", default=False)


@contextmanager
def uncapped() -> Iterator[None]:
    token = _UNCAPPED.set(True)
    try:
        yield
    finally:
        _UNCAPPED.reset(token)


def is_uncapped() -> bool:
    return _UNCAPPED.get()


def budget(capped: int) -> int:
    """The response byte budget: ``capped``, or no cap in-process."""
    return sys.maxsize if _UNCAPPED.get() else capped


def limit(request: dict[str, Any], default: int) -> int:
    """The request's ``limit``, else ``default`` (no default limit in-process)."""
    if "limit" in request:
        value: int = request["limit"]
        return value
    return sys.maxsize if _UNCAPPED.get() else default
