"""A headless LibreOffice for the live tests, skipped where none can be driven.

``DOC_CONTROL_REQUIRE_LIVE_LO=1`` (CI) turns the skip into a failure.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from doc_control.live.dispatch import using
from doc_control.live.lo_uno import LibreOffice
from tests.helpers import soffice_dir


@pytest.fixture(scope="session")
def lo_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A directory LibreOffice can read: a snap-confined one cannot see /tmp."""
    with soffice_dir(tmp_path_factory.mktemp("live")) as home:
        yield home


@pytest.fixture(scope="session")
def libreoffice(lo_home: Path) -> Iterator[LibreOffice]:
    """The adapter, on its own port and profile; the instance it starts is stopped after."""
    adapter = LibreOffice()
    with socket.socket() as probe:
        probe.bind(("localhost", 0))
        port = probe.getsockname()[1]
    env = pytest.MonkeyPatch()
    env.setenv("DOC_CONTROL_LO_PORT", str(port))
    env.setenv(
        "DOC_CONTROL_LO_ARGS",
        f"--headless -env:UserInstallation={(lo_home / 'profile').as_uri()}",
    )
    found = adapter.probe()
    if not found["reachable"]:
        env.undo()
        reason = f"LibreOffice cannot be driven here: {found.get('reason')}"
        if os.environ.get("DOC_CONTROL_REQUIRE_LIVE_LO"):
            pytest.fail(reason)
        pytest.skip(reason)
    try:
        yield adapter
    finally:
        with contextlib.suppress(Exception):  # already gone, or the bridge closes mid-call
            adapter._desktop().terminate()
        env.undo()


@pytest.fixture
def live(libreoffice: LibreOffice) -> Iterator[LibreOffice]:
    """Live calls go to the test LibreOffice only."""
    with using(libreoffice):
        yield libreoffice
