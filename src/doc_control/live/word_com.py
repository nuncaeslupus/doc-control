"""Microsoft Word as a live app, driven over COM (Windows, ``pywin32``).

``pywin32`` is the ``doc-control[live]`` extra; it is imported on the first call that
drives Word, never by ``probe``. The adapter attaches to the running Word and starts one
only for ``open``.

Reading: Word numbers paragraphs and reports text as its window shows them. With tracked
deletions shown (All Markup) a range's text holds the deleted text; with them hidden
(Simple Markup, No Markup) it does not, and a paragraph whose mark is deleted reads as
one with the next. A table adds a paragraph per row (the end-of-row mark). The file
engine's numbering and text are rebuilt from one whole-text read, the tracked deletions
and the tables, whatever the view, and the view is left as the writer has it. Positions in
Word count deleted text, field codes and comment marks, so an offset is turned into a
position by reading, never by adding.

When Word is busy (a dialog is open, the writer is typing) it rejects calls: a call is
tried again for up to ``BUSY_TIMEOUT`` seconds, as long as it has not changed anything.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import importlib
import importlib.util
import itertools
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import unquote, urlsplit

from doc_control.errors import DocControlError, ErrorCode
from doc_control.live.protocol import LiveUnavailable, Span

BUSY_TIMEOUT = 5.0
#: Seconds a reading of the document is taken as current (see ``Word._model``).
TRUST = 0.25
#: RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER: Word is busy, ask again.
_BUSY = frozenset({-2147418111, -2147417846})
#: RPC_S_SERVER_UNAVAILABLE, RPC_S_CALL_FAILED, RPC_E_DISCONNECTED, CO_E_OBJNOTCONNECTED:
#: Word has quit.
_GONE = frozenset({-2147023174, -2147023170, -2147417848, -2147220995})
_PROGID = "Word.Application"
_ONEDRIVE_KEY = r"Software\SyncEngines\Providers\OneDrive"

_DELETED = frozenset({2, 14})  # wdRevisionDelete, wdRevisionMovedFrom
_MAIN_STORY = 1  # wdMainTextStory
_PARAGRAPH = 4  # wdParagraph
_END_OF_ROW = 31  # wdAtEndOfRowMarker
_NO_SAVE = 0  # wdDoNotSaveChanges
_COLLAPSE_START = 1  # wdCollapseStart
_MINIMIZED = 2  # wdWindowStateMinimize
_NORMAL = 0  # wdWindowStateNormal
#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: Window.GetPoint gives physical pixels.
_PER_MONITOR = -4

_CELL = "\x07"
_CELL_END = "\r\x07"  # the end of a cell or a row: one position in Word
#: Word's characters for what the file engine reads as other text.
_AS_FILE = str.maketrans(
    {
        "\x1f": "\N{SOFT HYPHEN}",
        "\x1e": "\N{NON-BREAKING HYPHEN}",
        "\x0b": "\n",
        # Footnote, endnote and comment marks, pictures, cell marks, page and column breaks.
        **dict.fromkeys("\x01\x02\x05\x07\x0c\x0e"),
    }
)

_F = TypeVar("_F", bound=Callable[..., Any])


def _hresult(err: BaseException) -> int | None:
    """The COM error code of ``err``, or None when it is not a COM error."""
    if type(err).__name__ != "com_error":
        return None
    code = getattr(err, "hresult", None)
    return code if isinstance(code, int) else 0


def _no_pywin32() -> str:
    """Why Word cannot be driven without ``pywin32``. A frozen build carries its own
    ``pywin32``, so there the writer has nothing to install: the build is at fault."""
    if getattr(sys, "frozen", False):
        return "this build of doc-control was packaged without pywin32 (please report it)"
    return "pywin32 is not installed (pip install doc-control[live])"


def _why(err: BaseException) -> str:
    info = getattr(err, "excepinfo", None)
    said = info[2] if info and len(info) > 2 and info[2] else getattr(err, "strerror", None)
    return str(said or err).strip()


def _guard(method: _F) -> _F:
    """A COM failure is ``LiveUnavailable``; a busy Word is asked again for a while."""

    @functools.wraps(method)
    def run(self: Word, *args: Any) -> Any:
        deadline = time.monotonic() + BUSY_TIMEOUT
        wait = 0.05
        quit_once = False
        while True:
            self._wrote = False
            try:
                return method(self, *args)
            except Exception as err:
                code = _hresult(err)
                if code is None:
                    raise
                if code in _BUSY:
                    if not self._wrote and time.monotonic() + wait < deadline:
                        time.sleep(wait)
                        wait = min(wait * 2, 1.0)
                        continue
                    raise LiveUnavailable(
                        "Word is busy (a dialog is open, or the writer is typing): try again"
                    ) from err
                if code in _GONE:
                    wrote = self._wrote
                    self._forget()
                    if self._given is None and not wrote and not quit_once:
                        quit_once = True  # the link was to a Word that quit: look again
                        continue
                    raise LiveUnavailable("Word is not running any more") from err
                raise LiveUnavailable(f"Word failed: {_why(err)}") from err

    return run  # type: ignore[return-value]


def _units(text: str) -> int:
    """Positions ``text`` takes in Word, which counts UTF-16 units."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _editable(doc: Any) -> None:
    """Out of Read Mode, where Word refuses every edit, as the writer's own Edit does."""
    for window in doc.Windows:
        if window.View.ReadingLayout:
            window.View.ReadingLayout = False


def _clean(text: str) -> str:
    return text.translate(_AS_FILE)


def _flat(text: str) -> str:
    """``text`` without its line and paragraph ends, to compare two readings."""
    return text.replace("\r", "").replace("\n", "")


def _whole(text: str) -> str:
    """``text`` without the first half of a character cut in two by a range's end."""
    return text[:-1] if text and "\ud800" <= text[-1] <= "\udbff" else text


def _initials(author: str) -> str:
    return "".join(word[0] for word in author.split())[:9] or "?"


def _installed() -> bool:
    if sys.platform != "win32":
        return False
    winreg: Any = importlib.import_module("winreg")
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, _PROGID + r"\CLSID"))
    except OSError:
        return False
    return True


def _onedrive_mounts() -> list[tuple[str, str]]:
    """``(URL prefix, folder)`` of each OneDrive library synced to this machine."""
    if sys.platform != "win32":
        return []
    winreg: Any = importlib.import_module("winreg")
    found: list[tuple[str, str]] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ONEDRIVE_KEY) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                with (
                    winreg.OpenKey(root, winreg.EnumKey(root, i)) as key,
                    contextlib.suppress(OSError),
                ):
                    found.append(
                        (
                            str(winreg.QueryValueEx(key, "UrlNamespace")[0]),
                            str(winreg.QueryValueEx(key, "MountPoint")[0]),
                        )
                    )
    except OSError:
        return []
    return found


def local_paths(name: str, mounts: list[tuple[str, str]]) -> list[Path]:
    """The files a document's ``FullName`` may be: itself, or, for the URL Word gives a
    file in a OneDrive folder, that file under each library's folder."""
    if "://" not in name:
        return [Path(name)]
    found: list[Path] = []
    for prefix, folder in mounts:
        if not name.lower().startswith(prefix.lower().rstrip("/") + "/"):
            continue
        parts = [unquote(part) for part in name[len(prefix.rstrip("/")) + 1 :].split("/")]
        url = urlsplit(prefix)
        if (url.hostname or "").endswith("docs.live.net") and not url.path.strip("/"):
            parts = parts[1:]  # a personal library's URL has the account id before the path
        found.append(_walk(Path(folder), parts))
    return found


#: How OneDrive spells, in the URL Word reports, characters it does not keep as they
#: are: a comma comes back as ``^J`` ("Smith, Jane" -> "Smith^J Jane").
ONEDRIVE_ESCAPES = {"^J": ","}
_CARET = re.compile(r"\^[A-Za-z]")


def _walk(folder: Path, parts: list[str]) -> Path:
    """``folder`` joined with ``parts``, undoing OneDrive's caret escapes in a part that
    is not there as written. Of the readings of a part, the first under which the rest
    of the path exists wins, so a stray folder named with the escape as written (left
    by an older release) does not hide the real one; with none, the first reading."""
    if not parts:
        return folder
    fallback: Path | None = None
    for name in _entries(folder, parts[0]):
        found = _walk(folder / name, parts[1:])
        if found.exists():
            return found
        fallback = fallback or found
    assert fallback is not None  # _entries always gives a reading
    return fallback


def _entries(folder: Path, part: str) -> list[str]:
    """The readings of ``part`` in order: as written when it exists or has no ``^X``;
    with the known escapes undone when that exists; the one entry of ``folder`` that
    fits with each ``^X`` as any character; and last the known escapes undone."""
    if not _CARET.search(part):
        return [part]
    decoded = _CARET.sub(lambda m: ONEDRIVE_ESCAPES.get(m.group(0), m.group(0)), part)
    found = [name for name in (part, decoded) if (folder / name).exists()]
    pieces = re.split(r"(\^[A-Za-z])", part)
    fit = re.compile(
        "".join("." if _CARET.fullmatch(p) else re.escape(p) for p in pieces),
        re.IGNORECASE | re.DOTALL,
    )
    try:
        names = [entry.name for entry in os.scandir(folder) if fit.fullmatch(entry.name)]
    except (OSError, ValueError):  # a NUL in an earlier part of the URL
        names = []
    if len(names) == 1:
        found.append(names[0])
    found.append(decoded)
    return list(dict.fromkeys(found))


@dataclass
class _Model:
    """The open document as the file engine numbers and reads it."""

    key: tuple[Any, ...]
    hidden: bool  # tracked deletions are left out of the text Word reports
    checked: float = 0.0  # when it was last compared with the document
    texts: list[str] = field(default_factory=list)
    #: Per paragraph: Word's paragraph (from 0) and which part of it, when deleted
    #: paragraph marks split it.
    where: list[tuple[int, int]] = field(default_factory=list)
    #: Per paragraph: None in the body, else (table, cell).
    container: list[tuple[int, int] | None] = field(default_factory=list)
    deletions: list[tuple[int, int]] = field(default_factory=list)  # positions, in order
    marks: list[int] = field(default_factory=list)  # positions of deleted paragraph marks
    #: Per Word paragraph: its paragraphs' numbers (none for an end-of-row mark).
    of_word: list[list[int]] = field(default_factory=list)


class Word:
    app = "word"

    def __init__(self, application: Any = None) -> None:
        """``application``: a ``Word.Application`` to drive instead of the running one
        (tests use a hidden instance of their own)."""
        self._given = application
        self._word: Any = application
        self._wrote = False
        self._models: dict[str, _Model] = {}
        #: Per document: its state (see ``_state``) and its tracked deletions in it.
        self._kept: dict[str, tuple[tuple[str, int, int, int], list[tuple[int, int, str]]]] = {}
        self._mounts: list[tuple[str, str]] | None = None

    # -- the application ---------------------------------------------------------

    def _forget(self) -> None:
        self._word = self._given
        self._models.clear()
        self._kept.clear()

    def _application(self, start: bool = False) -> Any:
        if self._word is not None:
            return self._word
        if sys.platform != "win32":
            raise LiveUnavailable("Word is driven over COM, on Windows only")
        try:
            pythoncom: Any = importlib.import_module("pythoncom")
            client: Any = importlib.import_module("win32com.client")
        except ImportError as err:
            raise LiveUnavailable(f"{_no_pywin32()}: {err}") from err
        pythoncom.CoInitialize()
        try:
            self._word = client.GetActiveObject(_PROGID)
        except Exception as err:
            if _hresult(err) is None:
                raise
            if not start:
                raise LiveUnavailable("Word is not running: call live.open") from err
            self._word = client.Dispatch(_PROGID)
            self._word.Visible = True
        return self._word

    def probe(self) -> dict[str, Any]:
        """Never starts Word and never imports ``pywin32``."""
        out: dict[str, Any] = {
            "app": self.app,
            "available": False,
            "reachable": False,
            "version": None,
        }
        try:
            if self._given is None:
                if sys.platform != "win32":
                    return {**out, "reason": "Word is driven over COM, on Windows only"}
                if not _installed():
                    return {**out, "reason": "Word is not installed"}
                out["available"] = True
                if importlib.util.find_spec("win32com") is None:
                    return {**out, "reason": _no_pywin32()}
            out["available"] = True
            if self._word is not None:
                with contextlib.suppress(Exception):  # the writer quit it: ``open`` restarts
                    out["version"] = str(self._word.Version)
        except Exception as err:
            return {**out, "reason": str(err)}
        return {**out, "reachable": True}  # not running yet: ``open`` starts it

    def _same(self, name: str, want: Path) -> bool:
        if "://" in name and self._mounts is None:
            self._mounts = _onedrive_mounts()
        for found in local_paths(name, self._mounts or []):
            try:
                if found.samefile(want):
                    return True
            except OSError:
                if os.path.normcase(found.resolve()) == os.path.normcase(want):
                    return True
        return False

    def _find(self, path: Path) -> Any:
        """The open document at ``path``, or None (also when Word is not running)."""
        try:
            word = self._application()
        except LiveUnavailable:
            return None
        want = path.resolve()
        for doc in word.Documents:
            if self._same(str(doc.FullName), want):
                return doc
        return None

    @_guard
    def documents(self) -> list[Path]:
        try:
            word = self._application()
        except LiveUnavailable:
            return []
        found = []
        for doc in word.Documents:
            if not str(doc.Path):
                continue  # never saved
            name = str(doc.FullName)
            if "://" in name and self._mounts is None:
                self._mounts = _onedrive_mounts()
            places = local_paths(name, self._mounts or [])
            if places:
                found.append(next((p for p in places if p.exists()), places[0]))
        return found

    def _doc(self, path: Path) -> Any:
        doc = self._find(path)
        if doc is None:
            raise LiveUnavailable(f"{path.name} is not open in Word")
        return doc

    @_guard
    def is_open(self, path: Path) -> bool:
        return self._find(path) is not None

    @_guard
    def open(self, path: Path) -> None:
        word = self._application(start=True)
        if self._find(path) is not None:
            return
        self._wrote = True
        before = int(word.Documents.Count)
        # FileName, ConfirmConversions, ReadOnly, AddToRecentFiles
        doc = word.Documents.Open(str(path.resolve()), False, False, False)
        if doc.ReadOnly:
            if int(word.Documents.Count) > before:  # never a document that was open already
                doc.Close(_NO_SAVE)
            raise LiveUnavailable(
                f"Word opened {path.name} read-only (another window or program holds it)"
            )
        _editable(doc)

    @_guard
    def dirty(self, path: Path) -> bool:
        return not self._doc(path).Saved

    # -- reading -----------------------------------------------------------------

    @staticmethod
    def _read(doc: Any, start: int | None = None, end: int | None = None) -> str:
        """The text of a range (default: the whole body), whatever the window shows of
        field codes and hidden text."""
        rng = doc.Content if start is None else doc.Range(start, end)
        mode = rng.TextRetrievalMode
        mode.IncludeHiddenText = True
        mode.IncludeFieldCodes = False
        return str(rng.Text or "")

    @staticmethod
    def _index(doc: Any, position: int) -> int:
        """Word's paragraph (from 0) holding the character at ``position``."""
        return int(doc.Range(0, position + 1).Paragraphs.Count) - 1

    @staticmethod
    def _bounds(doc: Any, w: int) -> tuple[int, int]:
        """Where Word's paragraph ``w`` starts, and where its mark is."""
        rng = doc.Paragraphs(w + 1).Range
        return int(rng.Start), int(rng.End) - 1

    @staticmethod
    def _breaks(doc: Any) -> list[int]:
        """Positions of the section breaks."""
        return [int(doc.Sections(s).Range.End) - 1 for s in range(1, int(doc.Sections.Count))]

    def _pieces(self, doc: Any, text: str) -> tuple[list[str], list[bool]]:
        """Word's paragraphs, and whether each ends a table cell (or a row)."""
        count = int(doc.Paragraphs.Count)
        # A section break ends a paragraph as a mark does, and reads as a page break.
        broken = {self._index(doc, at) for at in self._breaks(doc)}
        pieces: list[str] = []
        ends: list[bool] = []
        at = 0
        while at < len(text) and len(pieces) < count:
            if text.startswith(_CELL, at):
                ends[-1] = True
                at += 1
            if len(pieces) in broken:
                size = len(self._read(doc, *self._bounds(doc, len(pieces))))
                stop = at + size
            else:
                stop = text.find("\r", at)
                if stop < 0:
                    break
            pieces.append(text[at:stop])
            ends.append(False)
            at = stop + 1
        if text.startswith(_CELL, at):
            ends[-1] = True
            at += 1
        if len(pieces) != count or at != len(text):
            raise LiveUnavailable(
                f"Word counts {count} paragraphs and its text reads as {len(pieces)}: "
                "this document cannot be addressed live"
            )
        return pieces, ends

    def _rows(self, doc: Any, pieces: list[str], ends: list[bool]) -> dict[int, Any]:
        """Per paragraph of a table: its (table, cell), or None for an end-of-row mark."""
        found: dict[int, Any] = {}
        for t in range(1, int(doc.Tables.Count) + 1):
            table = doc.Tables(t)
            span = table.Range
            first = self._index(doc, int(span.Start))
            inside = range(first, first + int(span.Paragraphs.Count))
            marks = [i for i in inside if ends[i]]
            rows: set[int] | None = None
            if table.Uniform and not table.Tables.Count:
                across = int(table.Columns.Count) + 1
                rows = {i for j, i in enumerate(marks) if j % across == across - 1}
                if len(marks) % across or any(pieces[i] for i in rows):
                    rows = None
            if rows is None:
                # ponytail: one question to Word per empty cell (15 ms); count the cells
                # of each row instead if large irregular tables matter.
                rows = set()
                for i in marks:
                    if not pieces[i]:
                        end = self._bounds(doc, i)[1]
                        if doc.Range(end, end).Information(_END_OF_ROW):
                            rows.add(i)
            cell = 0
            for i in inside:
                found[i] = None if i in rows else (t, cell)
                cell += ends[i] and i not in rows
        return found

    def _gaps(self, doc: Any, model: _Model, n: int) -> tuple[int, int, list[tuple[int, int]]]:
        """Paragraph ``n``: where it starts, where its mark is, and the stretches in
        between whose text is not deleted."""
        w, part = model.where[n]
        low, high = self._bounds(doc, w)
        if model.hidden:
            marks = [m for m in model.marks if low <= m < high]
            if part:
                low = marks[part - 1] + 1
            if part < len(marks):
                high = marks[part]
            return low, high, [(low, high)]
        return low, high, self._outside(model.deletions, low, high)

    @staticmethod
    def _outside(deletions: list[tuple[int, int]], low: int, high: int) -> list[tuple[int, int]]:
        """``low``-``high`` without the deletions, as stretches."""
        gaps = []
        at = low
        for start, end in deletions:
            if end <= at or start >= high:
                continue
            if start > at:
                gaps.append((at, start))
            at = max(at, end)
        if at < high or not gaps:
            gaps.append((min(at, high), high))
        return gaps

    def _model(self, doc: Any, fresh: bool = False) -> _Model:
        name = str(doc.FullName)
        cached = self._models.get(name)
        # Comparing takes a read of the whole text (0.1 s for a novel), and one request
        # asks several times in a row. A model compared a moment ago is used as it is:
        # ``_range`` reads the text it is about to hand out, and asks again if it differs.
        # ``paragraphs`` and ``selection``, which start a request, always compare.
        if cached is not None and not fresh and time.monotonic() - cached.checked < TRUST:
            return cached
        key = self._state(doc)
        text = key[0]
        if cached is not None and cached.key[:4] == key:
            window = doc.ActiveWindow.View.RevisionsFilter
            if cached.key[4:] == (int(window.Markup), int(window.View)):
                cached.checked = time.monotonic()
                return cached
        deleted = self._deletions(doc, name, key)
        # A range's text holds the deleted text only when the window shows it; the ends
        # of a deleted table's cells and rows show in every view, so they tell nothing.
        told = next((d for d in deleted if d[2].replace(_CELL_END, "")), None)
        hidden = told is not None and not self._read(doc, *told[:2]).replace(_CELL_END, "")
        window = doc.ActiveWindow.View.RevisionsFilter
        model = _Model((*key, int(window.Markup), int(window.View)), hidden)
        model.deletions = [(start, end) for start, end, _ in deleted]
        pieces, ends = self._pieces(doc, text)
        rows = self._rows(doc, pieces, ends) if key[3] else {}
        parts: dict[int, list[str]] = {}
        if hidden:
            # A paragraph whose mark is deleted reads as one with the next: split it.
            for start, _, gone in deleted:
                for at, char in enumerate(gone):
                    if char == "\r" and not gone.startswith(_CELL, at + 1):
                        cells = gone.count(_CELL_END, 0, at)  # one position each
                        model.marks.append(start + _units(gone[:at]) - cells)
            cuts: dict[int, list[int]] = {}
            for mark in model.marks:
                w = self._index(doc, mark)
                cuts.setdefault(w, []).append(len(self._read(doc, self._bounds(doc, w)[0], mark)))
            for w, offsets in cuts.items():
                edges = [0, *offsets, len(pieces[w])]
                parts[w] = [pieces[w][a:b] for a, b in itertools.pairwise(edges)]
        elif deleted:
            touched: set[int] = set()
            for start, end, _ in deleted:
                touched.update(range(self._index(doc, start), self._index(doc, end - 1) + 1))
            for w in touched:
                gaps = self._outside(model.deletions, *self._bounds(doc, w))
                parts[w] = ["".join(self._read(doc, a, b) for a, b in gaps)]
        for w, piece in enumerate(pieces):
            model.of_word.append([])
            if w in rows and rows[w] is None:
                continue
            for part, shown in enumerate(parts.get(w, [piece])):
                model.of_word[w].append(len(model.texts))
                model.texts.append(_clean(shown))
                model.where.append((w, part))
                model.container.append(self._container(doc, model, rows.get(w)))
        model.checked = time.monotonic()
        self._models = {name: model}
        return model

    def _state(self, doc: Any) -> tuple[str, int, int, int]:
        """What a model is kept for: the text, where it ends, and how many tracked changes
        and tables the document holds."""
        return (
            self._read(doc),
            int(doc.Content.End),
            int(doc.Content.Revisions.Count),
            int(doc.Tables.Count),
        )

    def _deletions(
        self, doc: Any, name: str, key: tuple[str, int, int, int]
    ) -> list[tuple[int, int, str]]:
        """The tracked deletions (start, end, text), in order. Word hands them out slowly
        (20-45 ms each in a novel: a manuscript under review takes seconds), so they are
        read only when the document is not as they were last kept; ``_shifting`` keeps
        them through a plain write."""
        kept = self._kept.get(name)
        if kept is not None and kept[0] == key:
            return kept[1]
        deleted: list[tuple[int, int, str]] = []
        if key[2]:
            for revision in doc.Content.Revisions:
                if int(revision.Type) in _DELETED:
                    rng = revision.Range
                    deleted.append((int(rng.Start), int(rng.End), str(rng.Text or "")))
            deleted.sort()
        self._kept = {name: (key, deleted)}
        return deleted

    @contextmanager
    def _shifting(self, doc: Any, start: int, end: int, text: str) -> Iterator[None]:
        """A plain write of ``text`` over ``start``-``end``: the tracked deletions after it
        move by what it adds, and none other changes, so the kept ones are moved rather than
        read again. Anything else (a deletion inside the range, a length Word did not
        write as asked) drops them."""
        name = str(doc.FullName)
        kept = self._kept.pop(name, None)
        before = self._state(doc) if kept else None
        yield
        if kept is None or before != kept[0]:
            return
        after = self._state(doc)
        delta = after[1] - kept[0][1]
        if after[2:] != kept[0][2:] or delta != _units(text) - (end - start):
            return
        if any(s < end and e > start for s, e, _ in kept[1]):
            return
        moved = [(s + delta, e + delta, t) if s >= end else (s, e, t) for s, e, t in kept[1]]
        self._kept[name] = (after, moved)

    def _container(self, doc: Any, model: _Model, inside: Any) -> Any:
        """The container of the paragraph last added to ``model``, whose Word paragraph is
        in ``inside``: deleted marks hidden, a paragraph before a table reads as one with
        its first cell, and only the part from the cell on is in the table."""
        if inside is None or not model.hidden:
            return inside
        low = self._gaps(doc, model, len(model.where) - 1)[0]
        return inside if low >= int(doc.Tables(inside[0]).Range.Start) else None

    def _position(self, doc: Any, gaps: list[tuple[int, int]], offset: int) -> int:
        """The position with ``offset`` characters of the paragraph's text before it."""
        seen = 0
        for start, end in gaps:
            raw = self._read(doc, start, end)
            size = len(_clean(raw))
            if offset > seen + size:
                seen += size
                continue
            want = offset - seen
            if _units(raw) == end - start:  # nothing unseen in it: count along
                at = start
                for char in raw:
                    if not want:
                        break
                    at += _units(char)
                    want -= bool(_clean(char))
                return at
            # Field codes or marks take positions the text does not show: look for it.
            low, high = start, end
            while low < high:
                middle = (low + high) // 2
                if len(_clean(_whole(self._read(doc, start, middle)))) >= want:
                    high = middle
                else:
                    low = middle + 1
            return low
        return gaps[-1][1]

    def _range(self, doc: Any, span: Span) -> tuple[Any, str]:
        """A Word range over ``span`` and the text it covers, deletions left out."""
        for fresh in (False, True):
            if fresh:
                self._models.clear()
            model = self._model(doc, fresh)
            rng, text = self._spanned(doc, model, span)
            start, end = int(rng.Start), int(rng.End)
            gaps = [(start, end)] if model.hidden else self._outside(model.deletions, start, end)
            seen = _clean("".join(self._read(doc, a, b) for a, b in gaps))
            known = [
                (max(a, start), min(b, end)) for a, b in model.deletions if a < end and b > start
            ]
            if _flat(seen) == _flat(text) and self._deleted(rng) == known:
                return rng, text
        raise LiveUnavailable(
            f"paragraph {span.start + 1} of the open document changed while it was read: try again"
        )

    @staticmethod
    def _deleted(rng: Any) -> list[tuple[int, int]]:
        """The tracked deletions inside ``rng``, as positions."""
        revisions = rng.Revisions
        if not int(revisions.Count):
            return []
        start, end = int(rng.Start), int(rng.End)
        found = []
        for revision in revisions:
            if int(revision.Type) in _DELETED:
                gone = revision.Range
                if int(gone.Start) < end and int(gone.End) > start:
                    found.append((max(int(gone.Start), start), min(int(gone.End), end)))
        return sorted(found)

    def _past_marks(self, doc: Any, at: int, stop: int) -> int:
        """``at`` moved past the marks that show no text (a note's reference, a picture,
        a page break), so that a range starting there leaves them alone."""
        while at < stop:
            raw = self._read(doc, at, at + 1)
            if not raw or raw == "\r" or _clean(raw):
                break
            at += 1
        return at

    def _spanned(self, doc: Any, model: _Model, span: Span) -> tuple[Any, str]:
        if not 0 <= span.start <= span.end < len(model.texts):
            raise LiveUnavailable(f"the open document has no paragraph {span.end + 1}")
        if len({model.container[n] for n in range(span.start, span.end + 1)}) > 1:
            # As the file engine: one range does not run from the body into a table
            # cell, or from one cell into the next.
            raise DocControlError(
                ErrorCode.UNSUPPORTED_CONTENT,
                f"paragraphs {span.start + 1} and {span.end + 1} are not in the same container",
            )
        parts = model.texts[span.start : span.end + 1]
        if span.b is not None:
            parts[-1] = parts[-1][: span.b]
        parts[0] = parts[0][span.a :]
        low, high, gaps = self._gaps(doc, model, span.start)
        start = self._position(doc, gaps, span.a) if span.a else low
        start = self._past_marks(doc, start, high)
        if span.end != span.start:
            low, high, gaps = self._gaps(doc, model, span.end)
        end = high if span.b is None else self._position(doc, gaps, span.b)
        return doc.Range(start, max(start, end)), "\n".join(parts)

    @_guard
    def paragraphs(self, path: Path) -> list[str]:
        return list(self._model(self._doc(path), fresh=True).texts)

    @_guard
    def text(self, path: Path, span: Span) -> str:
        return self._range(self._doc(path), span)[1]

    @_guard
    def select(self, path: Path, span: Span, cursor: bool = False) -> str:
        doc = self._doc(path)
        rng, text = self._range(doc, span)
        rng.Select()
        with contextlib.suppress(Exception):  # a hidden or minimised window has no layout
            _centre(doc.ActiveWindow, rng)
        if cursor:
            doc.ActiveWindow.Selection.Collapse(_COLLAPSE_START)
        return text

    @_guard
    def show(self, path: Path) -> bool:
        doc = self._doc(path)
        window = doc.ActiveWindow
        if not (self._application().Visible and window.Visible):
            return False
        if int(window.WindowState) == _MINIMIZED:
            window.WindowState = _NORMAL
        window.Activate()
        return _to_front(int(window.Hwnd))

    @_guard
    def selection(self, path: Path) -> tuple[str, int]:
        doc = self._doc(path)
        chosen = doc.ActiveWindow.Selection
        if int(chosen.StoryType) != _MAIN_STORY:
            raise LiveUnavailable("the selection is not text in the document body")
        start, end = int(chosen.Start), int(chosen.End)
        model = self._model(doc, fresh=True)
        last = int(doc.Content.End) - 1
        at = min(end - 1 if end > start else end, last)  # not the paragraph after its mark
        numbers = model.of_word[self._index(doc, at)]
        if not numbers:
            raise LiveUnavailable("the selection is not text in the document body")
        part = sum(1 for mark in model.marks if mark < at) if model.hidden else 0
        first = model.where[numbers[0]][0]
        part -= sum(1 for mark in model.marks if mark < self._bounds(doc, first)[0]) if part else 0
        gaps = [(start, end)] if model.hidden else self._outside(model.deletions, start, end)
        shown = ""
        for a, b in gaps:
            # A section break ends a paragraph, and reads as the page break it is not.
            for at in [at for at in self._breaks(doc) if a <= at < b]:
                shown += self._read(doc, a, at) + "\r"
                a = at + 1
            shown += self._read(doc, a, b)
        text = "\n".join(_clean(piece) for piece in shown.split("\r"))
        return text, numbers[min(max(part, 0), len(numbers) - 1)]

    # -- writing -----------------------------------------------------------------

    @staticmethod
    def _fields(doc: Any, start: int, end: int) -> list[tuple[int, int]]:
        """The fields (a hyperlink, a date) in the paragraphs of ``start``-``end``, each
        from its begin mark to its end mark."""
        around = doc.Range(start, end)
        around.Expand(_PARAGRAPH)
        fields = around.Fields
        if not int(fields.Count):
            return []
        return [
            (int(field.Code.Start) - 1, max(int(field.Result.End), int(field.Code.End)))
            for field in fields
        ]

    def _edited(self, doc: Any, span: Span, replace: bool) -> Any:
        """The range an edit of ``span`` takes, refused as the file engine refuses an
        edit inside a field or link (``UNSUPPORTED_CONTENT``), before anything is written."""
        rng, _ = self._range(doc, span)
        start, end = int(rng.Start), int(rng.End)
        fields = self._fields(doc, start, end)
        if not fields:
            return rng
        # An offset just after a field's text is its end mark: the text there is after it.
        closes = {close for _, close in fields}
        start += start in closes
        end = max(start, end + (end in closes))
        # An insertion needs text beside it: none at a paragraph's edge next to a field.
        around = doc.Range(start, start)
        around.Expand(_PARAGRAPH)
        edges = {int(around.Start), int(around.End) - 1}
        for begin, close in fields:
            inside = [begin < at <= close for at in (start, end)]
            if replace:
                if end > start:
                    cut = start <= close and end > begin
                else:
                    cut = inside[0] or start in edges & {begin, close + 1}
                why = "covers text of a field or link, which the live engine does not edit"
            else:
                cut = any(inside)
                why = "starts or ends inside a field or link: anchor it around the field"
            if cut:
                raise DocControlError(
                    ErrorCode.UNSUPPORTED_CONTENT, f"paragraph {span.start + 1}: the range {why}"
                )
        return doc.Range(start, end)

    @_guard
    def editable(self, path: Path, span: Span, replace: bool) -> None:
        self._edited(self._doc(path), span, replace)

    @contextmanager
    def _as(self, doc: Any, author: str, track: bool = True) -> Iterator[None]:
        """Tracked changes on (``track`` False: off, for a plain write) and ``author`` as
        the user's name, all put back after, in ``finally``.

        Word signs a change with the Office account's name unless told to use its own
        user name always, so that option is set for the edit too.
        """
        word = doc.Application
        options = word.Options
        was = [
            (doc, "TrackRevisions", doc.TrackRevisions),
            (options, "UseLocalUserInfo", options.UseLocalUserInfo),
            (word, "UserName", word.UserName),
            (word, "UserInitials", word.UserInitials),
        ]
        now = [track, True, author, _initials(author)]
        changed: list[tuple[Any, str, Any]] = []
        self._models.clear()  # the positions it holds are about to move
        _editable(doc)
        try:
            for (owner, setting, old), new in zip(was, now, strict=True):
                # Refused (tracking is locked, Word is busy) before anything of the
                # writer's changed: the call may be made again.
                setattr(owner, setting, new)
                self._wrote = True
                changed.append((owner, setting, old))
            yield
        finally:
            for owner, setting, old in reversed(changed):
                self._put_back(owner, setting, old)

    @staticmethod
    def _put_back(owner: Any, setting: str, old: Any) -> None:
        """One of the writer's settings as it was. Each on its own, and a busy Word is
        asked again: a failure here must not leave the others as the edit set them."""
        deadline = time.monotonic() + BUSY_TIMEOUT
        wait = 0.05
        while True:
            try:
                setattr(owner, setting, old)
                return
            except Exception as err:
                if _hresult(err) not in _BUSY or time.monotonic() + wait >= deadline:
                    return
                time.sleep(wait)
                wait = min(wait * 2, 1.0)

    @_guard
    def comment(self, path: Path, span: Span, text: str, author: str) -> None:
        doc = self._doc(path)
        rng = self._edited(doc, span, replace=False)
        with self._as(doc, author, track=bool(doc.TrackRevisions)):
            doc.Comments.Add(rng, text)

    @_guard
    def unresolved(self, path: Path, span: Span) -> bool:
        doc = self._doc(path)
        model = self._model(doc)
        if not 0 <= span.start <= span.end < len(model.texts):
            raise LiveUnavailable(f"the open document has no paragraph {span.end + 1}")
        low = self._gaps(doc, model, span.start)[0]
        high = self._gaps(doc, model, span.end)[1]
        around = doc.Range(low, max(low, high))
        around.Expand(_PARAGRAPH)  # the whole paragraphs, marks included
        return bool(int(around.Revisions.Count))

    @_guard
    def tracked_replace(
        self, path: Path, span: Span, text: str, author: str, tracked: bool = True
    ) -> None:
        doc = self._doc(path)
        rng = self._edited(doc, span, replace=True)
        start, end = int(rng.Start), int(rng.End)
        kept = contextlib.nullcontext() if tracked else self._shifting(doc, start, end, text)
        with self._as(doc, author, track=tracked), kept:
            if text:
                rng.Text = text
            elif int(rng.End) > int(rng.Start):  # Delete on no text takes the next character
                rng.Delete()

    @_guard
    def tracked_insert(
        self, path: Path, after: int, text: str, author: str, tracked: bool = True
    ) -> None:
        doc = self._doc(path)
        model = self._model(doc)
        if not -1 <= after < len(model.texts):
            raise LiveUnavailable(f"the open document has no paragraph {after + 1}")
        self._range(doc, Span(max(after, 0), max(after, 0)))  # the model is the document's
        low, high, _ = self._gaps(doc, self._model(doc), max(after, 0))
        at = low if after < 0 else high
        kept = contextlib.nullcontext() if tracked else self._shifting(doc, at, at, "\r" + text)
        with self._as(doc, author, track=tracked), kept:
            if after < 0:
                doc.Range(low, low).InsertAfter(text + "\r")
            else:
                doc.Range(high, high).InsertAfter("\r" + text)

    @_guard
    def save(self, path: Path) -> None:
        doc = self._doc(path)
        self._wrote = True
        doc.Save()

    @_guard
    def close(self, path: Path) -> None:
        doc = self._doc(path)
        self._wrote = True
        self._models.pop(str(doc.FullName), None)
        self._kept.pop(str(doc.FullName), None)
        doc.Close(_NO_SAVE)


def _point(window: Any, rng: Any) -> tuple[int, ...]:
    """``Window.GetPoint``: the range's left, top, width and height in screen pixels (its
    arguments are out-parameters, which late binding cannot read)."""
    ole = window._oleobj_
    dispid = ole.GetIDsOfNames("GetPoint")
    out_long = (16387, 2)  # VT_BYREF | VT_I4, out
    found = ole.InvokeTypes(
        dispid, 0, 1, (24, 0), (out_long,) * 4 + ((9, 1),), 0, 0, 0, 0, rng._oleobj_
    )
    return tuple(int(v) for v in found)


@contextmanager
def _physical_pixels() -> Iterator[None]:
    """Window rectangles in the physical pixels ``GetPoint`` gives, on this thread only."""
    user32: Any = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    before = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(_PER_MONITOR))
    try:
        yield
    finally:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(before))


def _centre(window: Any, rng: Any) -> None:
    """Scroll the window so the range sits in the middle of the page pane (its top a line
    below the pane's top when it is taller than the pane)."""
    win32gui: Any = importlib.import_module("win32gui")
    with _physical_pixels():
        panes: list[tuple[int, int, int, int]] = []

        def found(hwnd: int, _: Any) -> None:
            if win32gui.GetClassName(hwnd) == "_WwG":  # the page pane
                panes.append(win32gui.GetWindowRect(hwnd))

        win32gui.EnumChildWindows(int(window.Hwnd), found, None)
        if not panes:
            return
        _, top, _, bottom = panes[0]
        before = _point(window, rng)[1]
        window.SmallScroll(0, 1)  # one line up: how far a line moves the text
        _, now, _, height = _point(window, rng)
        line = now - before
        if line <= 0:
            return  # at the start of the document already
        target = top + max((bottom - top - height) / 2, line)
        lines = round((target - now) / line)
        if lines > 0:
            window.SmallScroll(0, lines)
        elif lines < 0:
            window.SmallScroll(-lines, 0)


def _to_front(hwnd: int) -> bool:
    """Bring a window to the front. Windows lets a background process do that only while
    it shares the foreground window's input, so it joins that input when refused. A window
    Word has only just opened can lose the focus again while Word settles: a few tries."""
    user32: Any = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
    for attempt in range(4):
        if attempt:
            time.sleep(0.15)
        if user32.SetForegroundWindow(hwnd) and user32.GetForegroundWindow() == hwnd:
            return True
        theirs = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        ours = ctypes.windll.kernel32.GetCurrentThreadId()  # type: ignore[attr-defined,unused-ignore]
        user32.AttachThreadInput(ours, theirs, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(ours, theirs, False)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


_ADAPTER = Word()


def adapter() -> Word:
    """One adapter per process, so its link to Word is reused."""
    return _ADAPTER
