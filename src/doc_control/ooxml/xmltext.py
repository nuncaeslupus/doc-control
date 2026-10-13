"""``escape``, ``unescape`` and ``quoteattr`` with the exact output of ``xml.sax.saxutils``.

Copied so that importing doc-control does not import ``xml.sax``, which pulls in
``urllib.request`` and ``http.client`` (about 30 ms of every cold start).
"""

from __future__ import annotations

from collections.abc import Mapping


def escape(data: str, entities: Mapping[str, str] | None = None) -> str:
    """``&``, ``<`` and ``>`` as entities (ampersand first), then each of ``entities``."""
    data = data.replace("&", "&amp;").replace(">", "&gt;").replace("<", "&lt;")
    for key, value in (entities or {}).items():
        data = data.replace(key, value)
    return data


def unescape(data: str, entities: Mapping[str, str] | None = None) -> str:
    """``&lt;``, ``&gt;``, each of ``entities``, then ``&amp;`` back to characters."""
    data = data.replace("&lt;", "<").replace("&gt;", ">")
    for key, value in (entities or {}).items():
        data = data.replace(key, value)
    return data.replace("&amp;", "&")


def quoteattr(data: str) -> str:
    """An attribute value, escaped and quoted as ``xml.sax.saxutils.quoteattr`` does."""
    data = escape(data).replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")
    if '"' in data:
        if "'" in data:
            return '"{}"'.format(data.replace('"', "&quot;"))
        return f"'{data}'"
    return f'"{data}"'
