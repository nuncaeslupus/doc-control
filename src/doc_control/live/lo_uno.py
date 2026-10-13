"""LibreOffice Writer as a live app, driven over UNO.

LibreOffice takes connections only when started with ``--accept``, so ``open`` starts a
listening instance when none answers on the port; a window the writer opened without it
cannot be attached to. ``uno`` ships with LibreOffice, not PyPI: it is imported on the
first call that drives the app (never by ``probe``), from LibreOffice's program directory
when this Python does not have it and can load it.

Environment: ``DOC_CONTROL_LO_PORT`` (default 2002), ``DOC_CONTROL_LO_PROGRAM`` (the
directory holding ``uno.py``), ``DOC_CONTROL_LO_ARGS`` (extra ``soffice`` arguments,
e.g. ``--headless``).

Reading: walking a document paragraph by paragraph costs LibreOffice about 30 ms each
(a minute for a novel), so the paragraph texts come from one whole-text read and a
paragraph is reached through LibreOffice's own search; only the paragraphs a call acts
on are walked, to read their text without tracked deletions.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import importlib
import importlib.util
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import sysconfig
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from doc_control.live.protocol import LiveUnavailable, Span

_PROGRAM_DIRS = (
    "/usr/lib/libreoffice/program",
    "/snap/libreoffice/current/lib/libreoffice/program",
    "/opt/libreoffice/program",
)
START_TIMEOUT = 90.0
_LIBPYTHON = re.compile(rb"libpython3\.(\d+)[a-z]*\.so")
NEEDLE = 80  # characters of a paragraph given to LibreOffice's search to reach it
_F = TypeVar("_F", bound=Callable[..., Any])


def _port() -> int:
    raw = os.environ.get("DOC_CONTROL_LO_PORT", "2002")
    if not raw.isdigit():
        raise LiveUnavailable(f"DOC_CONTROL_LO_PORT is not a port number: {raw!r}")
    return int(raw)


def _soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def _bridge() -> str | None:
    """Where ``uno`` can be imported from: None for this Python's own path, else
    LibreOffice's program directory. Decided without importing anything: pyuno aborts the
    whole process when loaded into a Python it was not built for."""
    if "uno" in sys.modules or importlib.util.find_spec("uno") is not None:
        return None
    here = f"Python {sys.version_info.major}.{sys.version_info.minor}"
    reason = "LibreOffice's Python bridge (uno.py) was not found"
    for program in (os.environ.get("DOC_CONTROL_LO_PROGRAM"), *_PROGRAM_DIRS):
        if not program or not Path(program, "uno.py").is_file():
            continue
        try:
            wants = _LIBPYTHON.search(Path(program, "libpyuno.so").read_bytes())
        except OSError:
            wants = None
        if wants is None:
            reason = f"{program} has no libpyuno.so naming the Python it needs"
        elif int(wants[1]) != sys.version_info.minor:
            reason = f"the bridge in {program} needs Python 3.{int(wants[1])}; this is {here}"
        elif not _libpython().is_file():
            reason = f"this {here} has no shared libpython for the bridge in {program}"
        else:
            return program
    raise LiveUnavailable(f"LibreOffice cannot be driven from this Python: {reason}")


def _libpython() -> Path:
    # ponytail: only a libpython in LIBDIR counts (uv, pyenv, conda). A distro Python uses
    # its own python3-uno package, found on the import path before this is asked.
    return Path(sysconfig.get_config_var("LIBDIR") or "") / (
        sysconfig.get_config_var("INSTSONAME") or "-"
    )


@functools.cache
def _uno() -> Any:
    """The ``uno`` module, or ``LiveUnavailable`` with why this Python cannot load it."""
    program = _bridge()
    try:
        if program is not None:
            # pyuno links libpython, which a uv or pyenv Python does not put on the loader path.
            ctypes.CDLL(str(_libpython()), mode=ctypes.RTLD_GLOBAL)
            os.environ["URE_BOOTSTRAP"] = f"vnd.sun.star.pathname:{program}/fundamentalrc"
            sys.path.append(program)
        return importlib.import_module("uno")
    except (ImportError, OSError) as err:
        if program in sys.path:
            sys.path.remove(program)
        raise LiveUnavailable(
            f"LibreOffice's Python bridge (uno) cannot be imported: {err}"
        ) from err


def _from_uno(err: Exception) -> bool:
    return any("com.sun.star" in cls.__qualname__ for cls in type(err).__mro__)


def _guard(method: _F) -> _F:
    """A UNO failure (the app closed, the bridge dropped) is ``LiveUnavailable``."""

    @functools.wraps(method)
    def run(self: LibreOffice, *args: Any) -> Any:
        try:
            return method(self, *args)
        except LiveUnavailable:
            raise
        except Exception as err:
            if not _from_uno(err):
                raise
            self._desk = None
            raise LiveUnavailable(
                f"LibreOffice failed: {type(err).__name__} {getattr(err, 'Message', '')}".strip()
            ) from err

    return run  # type: ignore[return-value]


def _walk(text: Any) -> Iterator[Any]:
    """Paragraphs of a text in file-engine order: table cells in place, row by row."""
    items = text.createEnumeration()
    while items.hasMoreElements():
        item = items.nextElement()
        if item.supportsService("com.sun.star.text.TextTable"):
            for name in item.getCellNames():
                yield from _walk(item.getCellByName(name))
        else:
            yield item


def _runs(paragraph: Any) -> Iterator[Any]:
    """The portions holding the paragraph's visible text: tracked deletions left out."""
    deleted = False
    portions = paragraph.createEnumeration()
    while portions.hasMoreElements():
        portion = portions.nextElement()
        kind = portion.TextPortionType
        if kind == "Redline":
            if portion.RedlineType == "Delete":
                deleted = portion.IsStart
        elif kind in ("Text", "TextField") and not deleted:
            yield portion


def _visible(paragraph: Any) -> str:
    return "".join(run.getString() for run in _runs(paragraph))


def _point(paragraph: Any, offset: int | None) -> Any:
    """A collapsed cursor at ``offset`` of the visible text (None: the paragraph's end)."""
    text = paragraph.getText()
    if offset is None:
        return text.createTextCursorByRange(paragraph.getEnd())
    seen = 0
    for run in _runs(paragraph):
        piece = run.getString()
        if offset <= seen + len(piece):
            cursor = text.createTextCursorByRange(run.getStart())
            # The cursor counts UTF-16 units.
            cursor.goRight(len(piece[: offset - seen].encode("utf-16-le")) // 2, False)
            return cursor
        seen += len(piece)
    return text.createTextCursorByRange(paragraph.getEnd())


def _around(cursor: Any) -> Any:
    """The whole paragraph the cursor is in."""
    cursor.gotoStartOfParagraph(False)
    cursor.gotoEndOfParagraph(True)
    return cursor.createEnumeration().nextElement()


class LibreOffice:
    app = "libreoffice"

    def __init__(self) -> None:
        self._desk: Any = None
        self._ctx: Any = None

    def _desktop(self, start: bool = False) -> Any:
        if self._desk is not None:
            return self._desk
        uno = _uno()
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local
        )
        port = _port()
        accept = f"socket,host=localhost,port={port};urp;"
        deadline = 0.0
        while True:
            try:
                self._ctx = resolver.resolve(f"uno:{accept}StarOffice.ComponentContext")
                break
            except Exception as err:
                if not _from_uno(err):
                    raise
                exe = _soffice()
                if not start or exe is None:
                    raise LiveUnavailable(
                        f"LibreOffice is not listening on port {port}: call live.open, "
                        f'or start it with soffice --accept="{accept}"'
                    ) from err
                if not deadline:
                    # ponytail: a localhost socket, open to this machine's other users;
                    # a named pipe if that matters (the snap's private /tmp hides it today).
                    extra = shlex.split(os.environ.get("DOC_CONTROL_LO_ARGS", ""))
                    subprocess.Popen(
                        [exe, "--norestore", "--nologo", f"--accept={accept}", *extra],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    deadline = time.monotonic() + START_TIMEOUT
                elif time.monotonic() > deadline:
                    raise LiveUnavailable(
                        f"LibreOffice did not start listening on port {port}: close the "
                        "LibreOffice windows opened without --accept and call live.open again"
                    ) from err
                time.sleep(0.2)
        self._desk = self._ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", self._ctx
        )
        return self._desk

    def _config(self, node: str) -> Any:
        self._desktop()
        provider = self._ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider", self._ctx
        )
        arg = _uno().createUnoStruct("com.sun.star.beans.PropertyValue")
        arg.Name, arg.Value = "nodepath", node
        return provider.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationUpdateAccess", (arg,)
        )

    def probe(self) -> dict[str, Any]:
        """Never imports ``uno``: a file-engine ``capabilities`` call must not load the bridge."""
        out: dict[str, Any] = {
            "app": self.app,
            "available": _soffice() is not None,
            "reachable": False,
            "listening": False,
            "version": None,
        }
        try:
            _bridge()
            with contextlib.suppress(OSError), socket.create_connection(("localhost", _port()), 1):
                out["listening"] = True
        except LiveUnavailable as err:
            return {**out, "reason": str(err)}
        if self._desk is not None:
            with contextlib.suppress(LiveUnavailable):  # the writer quit it: ``open`` restarts
                out["version"] = self._version()
        if not (out["listening"] or out["available"]):
            return {**out, "reason": "LibreOffice is not installed"}
        return {**out, "reachable": True}  # not running yet: ``open`` starts it

    @_guard
    def _version(self) -> str:
        return str(self._config("/org.openoffice.Setup/Product").ooSetupVersionAboutBox)

    def _find(self, path: Path) -> Any:
        """The open document at ``path``, or None (also when LibreOffice is not listening)."""
        try:
            desktop = self._desktop()
        except LiveUnavailable:
            return None
        want = path.resolve()
        components = desktop.Components.createEnumeration()
        while components.hasMoreElements():
            component = components.nextElement()
            url = getattr(component, "URL", "")
            if url.startswith("file:") and Path(_uno().fileUrlToSystemPath(url)) == want:
                return component
        return None

    def _doc(self, path: Path) -> Any:
        doc = self._find(path)
        if doc is None:
            raise LiveUnavailable(f"{path.name} is not open in LibreOffice")
        return doc

    @_guard
    def is_open(self, path: Path) -> bool:
        return self._find(path) is not None

    @_guard
    def open(self, path: Path) -> None:
        desktop = self._desktop(start=True)
        if self._find(path) is not None:
            return
        doc = desktop.loadComponentFromURL(path.resolve().as_uri(), "_blank", 0, ())
        if doc is None or not doc.supportsService("com.sun.star.text.TextDocument"):
            if doc is not None:
                doc.close(True)
            raise LiveUnavailable(f"LibreOffice could not open {path.name} as a text document")

    @_guard
    def dirty(self, path: Path) -> bool:
        return bool(self._doc(path).isModified())

    # -- reading -----------------------------------------------------------------

    def _texts(self, doc: Any) -> list[str]:
        """Paragraph texts, for finding a paragraph: tracked deletions and note marks are
        still in them (``text`` reads a paragraph without)."""
        search = doc.createSearchDescriptor()
        search.SearchRegularExpression, search.SearchString = True, "\\n"
        if doc.findFirst(search) is None:
            return str(doc.Text.getString()).replace("\r\n", "\n").split("\n")
        # ponytail: a line break reads like a paragraph mark in the whole text, so a
        # document with one is walked (30 ms a paragraph); split on the marks' positions
        # if long documents with line breaks matter.
        return [str(paragraph.getString()) for paragraph in _walk(doc.Text)]

    def _paragraph(self, doc: Any, texts: list[str], n: int) -> Any:
        """Paragraph ``n`` (from 0): the right one of the search hits for its first words."""
        needle = texts[n][:NEEDLE]
        search = doc.createSearchDescriptor()
        search.SearchCaseSensitive = True
        if needle:
            search.SearchString = needle
            earlier = "\n".join(texts[:n]).count(needle)
            total = earlier + "\n".join(texts[n:]).count(needle)
        else:
            search.SearchRegularExpression, search.SearchString = True, "^$"
            earlier, total = texts[:n].count(""), texts.count("")
        hits = doc.findAll(search)
        if hits.getCount() == total:
            hit = hits.getByIndex(earlier)
            cursor = hit.getText().createTextCursorByRange(hit.getStart())
            if cursor.isStartOfParagraph():
                paragraph = _around(cursor)
                if paragraph.getString() == texts[n]:
                    return paragraph
        # ponytail: the search and the texts disagree (a field, a note mark, a hit outside
        # the main text): walk to it, 5 ms a paragraph.
        return next(p for i, p in enumerate(_walk(doc.Text)) if i == n)

    def _range(self, doc: Any, span: Span) -> tuple[Any, str]:
        """A cursor over ``span`` and the visible text it covers."""
        texts = self._texts(doc)
        if not 0 <= span.start <= span.end < len(texts):
            raise LiveUnavailable(f"the open document has no paragraph {span.end + 1}")
        found = [self._paragraph(doc, texts, n) for n in range(span.start, span.end + 1)]
        parts = [_visible(paragraph) for paragraph in found]
        if span.b is not None:
            parts[-1] = parts[-1][: span.b]
        parts[0] = parts[0][span.a :]
        cursor = _point(found[0], span.a)
        cursor.gotoRange(_point(found[-1], span.b), True)
        return cursor, "\n".join(parts)

    @_guard
    def paragraphs(self, path: Path) -> list[str]:
        doc = self._doc(path)
        raw = self._texts(doc)
        texts = list(raw)
        # A paragraph holding a tracked deletion is re-read without it, so a find phrase
        # is matched against what the writer sees.
        # ponytail: several calls per tracked deletion; cache per document if a heavily
        # revised novel makes select slow.
        redlines = doc.Redlines.createEnumeration()
        while redlines.hasMoreElements():
            redline = redlines.nextElement()
            if redline.RedlineType != "Delete" or redline.IsInHeaderFooter:
                continue
            start = redline.RedlineStart
            paragraph = _around(start.getText().createTextCursorByRange(start))
            at = [n for n, text in enumerate(raw) if text == paragraph.getString()]
            if len(at) != 1:
                # A repeated paragraph cannot be placed: read them all, in order.
                return [_visible(paragraph) for paragraph in _walk(doc.Text)]
            texts[at[0]] = _visible(paragraph)
        return texts

    @_guard
    def text(self, path: Path, span: Span) -> str:
        return self._range(self._doc(path), span)[1]

    @_guard
    def select(self, path: Path, span: Span) -> str:
        doc = self._doc(path)
        cursor, text = self._range(doc, span)
        doc.CurrentController.select(cursor)
        return text

    @_guard
    def selection(self, path: Path) -> tuple[str, int]:
        doc = self._doc(path)
        try:
            return self._selection(doc)
        except (AttributeError, IndexError) as err:
            # A table, a block of cells, a footnote: nothing a paragraph address names.
            raise LiveUnavailable("the selection is not text in the document body") from err

    def _selection(self, doc: Any) -> tuple[str, int]:
        chosen = doc.CurrentController.getSelection().getByIndex(0)
        story = chosen.getText()
        paragraph = _around(story.createTextCursorByRange(chosen.getEnd()))
        own = paragraph.getString()
        at = [n for n, text in enumerate(self._texts(doc)) if text == own]
        if len(at) != 1:
            # ponytail: a repeated paragraph is told apart by walking to it.
            at = [
                n
                for n, other in enumerate(_walk(doc.Text))
                if other.getText() == story and story.compareRegionStarts(other, paragraph) == 0
            ]
        shown = story.createTextCursorByRange(chosen).createEnumeration()
        parts = []
        while shown.hasMoreElements():
            parts.append(_visible(shown.nextElement()))
        return "\n".join(parts), at[0]

    @contextmanager
    def _as(self, doc: Any, author: str) -> Iterator[None]:
        """Tracked changes on and ``author`` as the user's name, both put back after."""
        profile = self._config("/org.openoffice.UserProfile/Data")
        name, tracking = (profile.givenname, profile.sn), doc.RecordChanges
        profile.givenname, profile.sn = author, ""
        profile.commitChanges()
        doc.RecordChanges = True
        try:
            yield
        finally:
            doc.RecordChanges = tracking
            profile.givenname, profile.sn = name
            profile.commitChanges()

    @_guard
    def comment(self, path: Path, span: Span, text: str, author: str) -> None:
        doc = self._doc(path)
        cursor, _ = self._range(doc, span)
        note = doc.createInstance("com.sun.star.text.textfield.Annotation")
        now = datetime.now(UTC)
        stamp = _uno().createUnoStruct("com.sun.star.util.DateTime")
        stamp.Year, stamp.Month, stamp.Day = now.year, now.month, now.day
        stamp.Hours, stamp.Minutes, stamp.Seconds, stamp.IsUTC = (
            now.hour,
            now.minute,
            now.second,
            True,
        )
        note.Author, note.Content, note.DateTimeValue = author, text, stamp
        cursor.getText().insertTextContent(cursor, note, True)

    @_guard
    def tracked_replace(self, path: Path, span: Span, text: str, author: str) -> None:
        doc = self._doc(path)
        cursor, _ = self._range(doc, span)
        with self._as(doc, author):
            cursor.setString(text)

    @_guard
    def tracked_insert(self, path: Path, after: int, text: str, author: str) -> None:
        doc = self._doc(path)
        texts = self._texts(doc)
        if not -1 <= after < len(texts):
            raise LiveUnavailable(f"the open document has no paragraph {after + 1}")
        anchor = self._paragraph(doc, texts, max(after, 0))
        story = anchor.getText()
        break_ = _uno().getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")
        with self._as(doc, author):
            if after < 0:
                cursor = story.createTextCursorByRange(anchor.getStart())
                story.insertString(cursor, text, False)
                story.insertControlCharacter(cursor, break_, False)
            else:
                cursor = story.createTextCursorByRange(anchor.getEnd())
                story.insertControlCharacter(cursor, break_, False)
                story.insertString(cursor, text, False)

    @_guard
    def save(self, path: Path) -> None:
        self._doc(path).store()

    @_guard
    def close(self, path: Path) -> None:
        self._doc(path).close(True)


_ADAPTER = LibreOffice()


def adapter() -> LibreOffice:
    """One adapter per process, so its connection is reused."""
    return _ADAPTER
