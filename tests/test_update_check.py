"""The opt-in update notice (D-P7): no request unless switched on, one a day, quiet failures."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

from doc_control import __version__, update_check
from doc_control.ops import live, meta

RELEASE = {
    "tag_name": "v9.9.9",
    "assets": [
        {"name": "doc-control-9.9.9.mcpb", "browser_download_url": "https://example.test/x.mcpb"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.test/sums"},
    ],
}


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Counter:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.error = error

    def __call__(self, request: Any, timeout: float | None = None) -> _Response:
        self.calls.append((request.full_url, timeout))
        if self.error is not None:
            raise self.error
        return _Response(json.dumps(RELEASE).encode())


def _caps() -> dict[str, Any]:
    return cast("dict[str, Any]", meta.capabilities({}))


def _status() -> dict[str, Any]:
    return cast("dict[str, Any]", live.status({}))


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Counter:
    c = Counter()
    monkeypatch.setattr(live, "apps", lambda: [])  # no probe of a real Word
    monkeypatch.setattr(live, "_open_documents", lambda _apps: [])
    monkeypatch.setattr(urllib.request, "urlopen", c)
    monkeypatch.setenv("DOC_CONTROL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("DOC_CONTROL_UPDATE_CHECK", raising=False)
    return c


def test_no_request_when_off(counter: Counter, tmp_path: Path) -> None:
    assert "update" not in _caps()
    assert "update" not in _status()
    assert counter.calls == []
    assert not (tmp_path / "cache").exists()


def test_second_check_within_a_day_uses_cache(
    counter: Counter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOC_CONTROL_UPDATE_CHECK", "1")
    first = update_check.update_info(now=1_000_000.0)
    second = update_check.update_info(now=1_000_000.0 + 3600)
    assert len(counter.calls) == 1
    assert counter.calls[0][0].startswith("https://api.github.com/repos/nuncaeslupus/")
    assert counter.calls[0][1] == 3
    for info in (first, second):
        assert info is not None
        assert info["latest"] == "9.9.9"
        assert info["current"] == __version__
        assert info["mcpb_url"] == "https://example.test/x.mcpb"
    update_check.update_info(now=1_000_000.0 + 25 * 3600)
    assert len(counter.calls) == 2
    assert "update" in _caps()


def test_failed_check_is_quiet(counter: Counter, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_CONTROL_UPDATE_CHECK", "1")
    counter.error = urllib.error.URLError("offline")
    info = update_check.update_info()
    assert info is not None and set(info) == {"error"}
    assert "update" in _caps()
    assert "error" in _status()["update"]
