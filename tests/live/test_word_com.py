"""Microsoft Word over COM (LIVE T8): the scenarios LibreOffice passes, and the parts of
the adapter that need no Word (a busy Word, a quit Word, OneDrive names, the probe).

``DOC_CONTROL_REQUIRE_LIVE_WORD=1`` turns the skip of the Word scenarios into a failure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live import word_com
from doc_control.live.dispatch import ADAPTER_MODULES, using
from doc_control.live.protocol import LiveUnavailable
from doc_control.live.word_com import Word, local_paths
from tests.helpers import make_docx
from tests.live import word_suite

BUSY, GONE, OTHER = -2147418111, -2147023174, -2146823114


def test_word_adapter_protocol_conformance(tmp_path: Path) -> None:
    reason = word_suite.unavailable()
    if reason:
        if os.environ.get("DOC_CONTROL_REQUIRE_LIVE_WORD"):
            pytest.fail(reason)
        pytest.skip(reason)
    with word_suite.hidden_word() as word:
        assert word_suite.run_all(word, tmp_path) == []  # live_word_failures


class com_error(Exception):  # pywin32's name, which the adapter looks for
    def __init__(self, hresult: int) -> None:
        super().__init__(hresult, "Call was rejected by callee.", None, None)
        self.hresult = hresult
        self.strerror = "Call was rejected by callee."


class _Doc:
    def __init__(self, name: Path | str, fails: list[int]) -> None:
        self.FullName = str(name)
        self.Saved = False
        self.fails = fails
        self.saves = 0

    def Save(self) -> None:
        self.saves += 1
        if self.fails:
            raise com_error(self.fails.pop(0))


class _App:
    """A Word that rejects the first calls for its documents."""

    def __init__(self, doc: _Doc, fails: list[int]) -> None:
        self.doc = doc
        self.fails = fails
        self.asked = 0

    @property
    def Documents(self) -> list[_Doc]:
        self.asked += 1
        if self.fails:
            raise com_error(self.fails.pop(0))
        return [self.doc]


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(word_com.time, "sleep", waits.append)
    return waits


def test_a_busy_word_is_asked_again(tmp_path: Path, no_wait: list[float]) -> None:
    doc = tmp_path / "a.docx"
    app = _App(_Doc(doc, []), [BUSY, BUSY])
    assert Word(app).dirty(doc) is True
    assert app.asked == 3 and no_wait == [0.05, 0.1]


def test_a_word_busy_for_too_long_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = tmp_path / "a.docx"
    app = _App(_Doc(doc, []), [BUSY] * 1000)
    clock = [0.0]
    monkeypatch.setattr(word_com.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(word_com.time, "sleep", lambda wait: clock.__setitem__(0, clock[0] + wait))
    with pytest.raises(LiveUnavailable, match="busy"):
        Word(app).is_open(doc)
    assert clock[0] <= word_com.BUSY_TIMEOUT and app.asked > 5


def test_a_call_that_wrote_is_not_made_twice(tmp_path: Path, no_wait: list[float]) -> None:
    doc = _Doc(tmp_path / "a.docx", [BUSY])
    with pytest.raises(LiveUnavailable, match="busy"):
        Word(_App(doc, [])).save(tmp_path / "a.docx")
    assert doc.saves == 1 and no_wait == []


def test_other_failures_are_unavailable_with_words_reason(tmp_path: Path) -> None:
    doc = tmp_path / "a.docx"
    with pytest.raises(LiveUnavailable, match="not running any more"):
        Word(_App(_Doc(doc, []), [GONE])).dirty(doc)
    with pytest.raises(LiveUnavailable, match="Word failed: Call was rejected"):
        Word(_App(_Doc(doc, [OTHER]), [])).save(doc)
    with pytest.raises(LiveUnavailable, match=r"b\.docx is not open in Word"):
        Word(_App(_Doc(doc, []), [])).save(tmp_path / "b.docx")
    with using(Word(_App(_Doc(doc, []), [GONE]))), pytest.raises(DocControlError) as err:
        api.run("live.save", {"doc": str(doc)})
    assert err.value.code is ErrorCode.ENGINE_UNAVAILABLE


class _Setting:
    """An object of Word's whose named settings cannot be put back."""

    def __init__(self, stuck: tuple[str, ...] = (), **values: Any) -> None:
        self.__dict__.update(values, _stuck=stuck, _set=0)

    def __setattr__(self, name: str, value: Any) -> None:
        state = self.__dict__
        state["_set"] += 1
        if name in state["_stuck"] and state["_set"] > 1:
            raise com_error(OTHER)
        self.__dict__[name] = value


def test_each_setting_is_put_back_though_another_fails(no_wait: list[float]) -> None:
    app: Any = _Setting(UserName="Ana", UserInitials="A", Options=_Setting(UseLocalUserInfo=False))
    doc: Any = _Setting(("TrackRevisions",), TrackRevisions=False, Application=app)
    with pytest.raises(RuntimeError), Word(app)._as(doc, "Rev Iewer"):
        assert (app.UserName, app.UserInitials, doc.TrackRevisions) == ("Rev Iewer", "RI", True)
        raise RuntimeError("mid-edit")
    assert (app.UserName, app.UserInitials) == ("Ana", "A")
    assert app.Options.UseLocalUserInfo is False

    # Tracking that cannot be turned on (it is locked) changes nothing of the writer's.
    doc = _Setting(("TrackRevisions",), TrackRevisions=False, Application=app)
    doc._set = 1
    adapter = Word(app)
    with pytest.raises(com_error), adapter._as(doc, "Rev Iewer"):
        pytest.fail("the edit ran")
    assert (app.UserName, app.Options.UseLocalUserInfo, adapter._wrote) == ("Ana", False, False)


class _Docs(list[Any]):
    """Word's documents, where opening a file gives back one that is open already."""

    @property
    def Count(self) -> int:
        return len(self)

    def Open(self, *args: Any) -> Any:
        return self[0]


def test_open_never_closes_a_document_that_was_open(tmp_path: Path) -> None:
    held: Any = _Doc("https://unmapped.example/a.docx", [])
    held.ReadOnly, held.closed = True, False
    held.Close = lambda save: setattr(held, "closed", True)
    app: Any = _App(held, [])
    app.__class__ = type("_Opening", (_App,), {"Documents": _Docs([held])})
    with pytest.raises(LiveUnavailable, match="read-only"):
        Word(app).open(make_docx(tmp_path / "a.docx", "One."))
    assert held.closed is False


def test_a_onedrive_name_is_the_local_file(tmp_path: Path) -> None:
    mounts = [
        ("https://contoso.sharepoint.com/sites/Books", str(tmp_path / "Books")),
        ("https://d.docs.live.net", str(tmp_path / "OneDrive")),
    ]
    name = "https://d.docs.live.net/0123456789abcdef/dev/My%20novel.docx"
    assert tmp_path / "OneDrive" / "dev" / "My novel.docx" in local_paths(name, mounts)
    name = "https://contoso.sharepoint.com/sites/Books/Drafts/a.docx"
    # Only there: not also the file of that name in the folder above.
    assert local_paths(name, mounts) == [tmp_path / "Books" / "Drafts" / "a.docx"]
    assert local_paths("https://elsewhere.example/a.docx", mounts) == []
    assert local_paths(r"D:\Work\a.docx", mounts) == [Path(r"D:\Work\a.docx")]

    doc = make_docx(tmp_path / "a.docx", "One.")
    adapter = Word(_App(_Doc("https://d.docs.live.net/01/a.docx", []), []))
    adapter._mounts = [("https://d.docs.live.net", str(tmp_path))]
    assert adapter.is_open(doc) and not adapter.is_open(tmp_path / "b.docx")


def test_probe_never_imports_pywin32(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ADAPTER_MODULES["word"] == "word_com" and word_com.adapter().app == "word"

    def loaded(name: str) -> Any:
        raise AssertionError(f"probe imported {name}")

    monkeypatch.setattr(word_com.importlib, "import_module", loaded)
    monkeypatch.setattr(word_com, "_installed", lambda: False)
    probe = Word().probe()
    assert probe["app"] == "word" and probe["reachable"] is False and probe["reason"]
    if sys.platform == "win32":
        assert probe["reason"] == "Word is not installed"
        monkeypatch.setattr(word_com, "_installed", lambda: True)
        monkeypatch.setattr(word_com.importlib.util, "find_spec", lambda name: None)
        probe = Word().probe()
        assert probe["available"] and not probe["reachable"] and "pywin32" in probe["reason"]
    else:
        assert "Windows" in probe["reason"]
        with pytest.raises(LiveUnavailable, match="Windows"):
            Word().open(Path("a.docx"))
        assert Word().is_open(Path("a.docx")) is False


def test_word_error_when_frozen_names_no_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frozen build missing pywin32 says so; it never tells a writer to pip install."""

    def missing(name: str) -> Any:
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(word_com.sys, "platform", "win32")
    monkeypatch.setattr(word_com.sys, "frozen", True, raising=False)
    monkeypatch.setattr(word_com.importlib, "import_module", missing)
    with pytest.raises(LiveUnavailable) as raised:
        Word()._application()
    assert "pip install" not in str(raised.value)
    assert "packaged without pywin32" in str(raised.value)

    monkeypatch.setattr(word_com, "_installed", lambda: True)
    monkeypatch.setattr(word_com.importlib.util, "find_spec", lambda name: None)
    probe = Word().probe()
    assert "pip install" not in probe["reason"] and "packaged without pywin32" in probe["reason"]

    monkeypatch.delattr(word_com.sys, "frozen")
    with pytest.raises(LiveUnavailable, match="pip install"):
        Word()._application()
