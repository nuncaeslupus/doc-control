"""The live scenarios LibreOffice passes (``test_lo_*``), run against Microsoft Word.

Each scenario takes the adapter and a working directory and returns what went wrong, so
``tests/live/test_word_com.py`` and ``scripts/live_word_check.py`` run the same checks.
Word is a hidden instance of its own: a window the writer has open is never touched.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from doc_control import api
from doc_control.errors import DocControlError, ErrorCode
from doc_control.live import word_com
from doc_control.live.dispatch import using
from doc_control.live.word_com import Word
from doc_control.ooxml.zipio import sha256_file
from tests.corpus.generator import KINDS, generate
from tests.helpers import make_docx

AUTHOR = "Rev Iewer"
SAMPLES = 8
ALL_MARKUP, SIMPLE_MARKUP = 2, 1  # wdRevisionsMarkupAll, wdRevisionsMarkupSimple


def unavailable() -> str | None:
    """Why Word cannot be driven here, or None when it can."""
    if sys.platform != "win32":
        return "Word is driven over COM, on Windows only"
    if importlib.util.find_spec("win32com") is None:
        return "pywin32 is not installed"
    if not word_com._installed():
        return "Word is not installed"
    return None


@contextlib.contextmanager
def hidden_word() -> Iterator[Word]:
    """An adapter on a Word of its own, hidden, quit afterwards; live calls go to it only."""
    client: Any = importlib.import_module("win32com.client")
    app = client.DispatchEx("Word.Application")
    try:
        app.Visible = False
        app.DisplayAlerts = 0  # wdAlertsNone
        adapter = Word(app)
        with using(adapter):
            yield adapter
    finally:
        with contextlib.suppress(Exception):
            for doc in list(app.Documents):
                doc.Close(0)
        with contextlib.suppress(Exception):
            app.NormalTemplate.Saved = True
            app.Quit(0)


class Failures(list[str]):
    def check(self, ok: bool, what: str, *seen: object) -> None:
        if not ok:
            self.append(f"{what}: {seen!r}" if seen else what)

    def refused(self, what: str, op: str, request: dict[str, Any], code: ErrorCode) -> None:
        try:
            _run(op, request)
        except DocControlError as err:
            self.check(err.code is code, what, err.code.value, str(err))
        else:
            self.append(f"{what}: not refused")


def _run(op: str, request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = api.run(op, request).result
    return result


def _read(doc: Path) -> list[dict[str, Any]]:
    with api.open(doc) as handle:
        paragraphs: list[dict[str, Any]] = handle.paragraphs()
    return paragraphs


def _texts(doc: Path) -> list[str]:
    return [p["text"] for p in _read(doc)]


def _type(word: Word, doc: Path, text: str, start: bool = False) -> None:
    """What the writer does: add text at the end (or the start) of the open document."""
    body = word._doc(doc).Content
    if start:
        body.InsertBefore(text)
    else:
        last = int(body.End) - 1
        word._doc(doc).Range(last, last).InsertAfter(text)


def _markup(word: Word, doc: Path, markup: int) -> None:
    word._doc(doc).ActiveWindow.View.RevisionsFilter.Markup = markup


@contextlib.contextmanager
def _opened(doc: Path) -> Iterator[dict[str, Any]]:
    base = {"doc": str(doc)}
    _run("live.open", base)
    try:
        yield base
    finally:
        _run("live.close", {**base, "discard": True})


def session(word: Word, home: Path) -> list[str]:
    """Open, attach, save, close, and the unsaved-work guard (``unsaved_work_lost == 0``)."""
    bad = Failures()
    doc = make_docx(home / "dirty.docx", "One.", "Two.")
    request = {"doc": str(doc)}
    bad.check(_run("live.status", request)["open_in"] is None, "status before open")
    opened = _run("live.open", request)
    try:
        bad.check(opened == {"app": "word", "dirty": False}, "open", opened)
        _run("live.open", request)  # attaches, no second window
        bad.check(int(word._application().Documents.Count) == 1, "open twice")
        _type(word, doc, " More.")
        bad.refused("close with unsaved work", "live.close", request, ErrorCode.UNSAVED_CHANGES)
        status = _run("live.status", request)
        bad.check(status["open_in"] == {"app": "word", "controlled": True}, "status", status)
        bad.check(status["dirty"] is True, "unsaved work lost")
        bad.check(_texts(doc) == ["One.", "Two."], "file changed before save", _texts(doc))
        closed = _run("live.close", {**request, "save": True})
        bad.check(closed == {"closed": True, "saved": True}, "close save", closed)
    finally:
        if word.is_open(doc):
            word.close(doc)
    bad.check(_run("live.status", request)["open_in"] is None, "status after close")
    bad.check(_texts(doc) == ["One.", "Two. More."], "saved text", _texts(doc))

    doc = make_docx(home / "discard.docx", "One.")
    request = {"doc": str(doc)}
    _run("live.open", request)
    try:
        _type(word, doc, " Kept.")
        bad.check(_run("live.save", request) == {"saved": True}, "save")
        bad.check(_run("live.status", request)["dirty"] is False, "dirty after save")
        saved = sha256_file(doc)
        _type(word, doc, " Dropped.")
        dropped = _run("live.close", {**request, "discard": True})
        bad.check(dropped["saved"] is False, "discard", dropped)
        bad.check(sha256_file(doc) == saved, "discard changed the file")
        _run("live.open", request)
        bad.check(word.paragraphs(doc) == ["One. Kept."], "reopened", word.paragraphs(doc))
        _run("live.close", request)
    finally:
        if word.is_open(doc):
            word.close(doc)
    return bad


def _rows(doc: Path) -> list[dict[str, Any]]:
    """The ``locate`` row and file-engine text of a sample of paragraphs."""
    paragraphs = _read(doc)

    def locate(at: str) -> dict[str, Any]:
        row: dict[str, Any] = _run("locate", {"doc": str(doc), "at": at})["paragraphs"][0]
        return row

    rows = []
    # Word recomputes a date field on load, so that paragraph reads differently in the
    # app than in the file: live.select answers STALE_ADDRESS for it, rightly.
    paragraphs = [p for p in paragraphs if not p["text"].startswith("Dated ")]
    step = max(1, len(paragraphs) // SAMPLES)
    for paragraph in [*paragraphs[::step], paragraphs[-1]]:
        row = {**locate(paragraph["at"]), "text": paragraph["text"]}
        if "context" in row:
            row["context"]["across"] = locate(row["context"]["at"]).get("find_phrase_across")
        rows.append(row)
    return rows


def select(word: Word, home: Path) -> list[str]:
    """Every corpus kind, by address, find phrase and context, with tracked deletions
    shown and hidden: the text and address are the file engine's (``live_select_mismatches``)."""
    bad = Failures()
    for kind in KINDS:
        doc = home / f"select-{kind}.docx"
        doc.write_bytes(generate(kind, 0))
        rows = _rows(doc)
        count = len(_read(doc))
        with _opened(doc) as base:
            for markup in (ALL_MARKUP, SIMPLE_MARKUP):
                _markup(word, doc, markup)
                where = f"{kind} markup {markup}"
                bad.check(len(word.paragraphs(doc)) == count, f"{where}: paragraph count")
                for row in rows:
                    want = {"text": row["text"], "at": row["at"]}
                    asks: dict[str, dict[str, Any]] = {"at": {"at": row["at"]}}
                    if row.get("find_phrase"):
                        asks["find"] = {"find_phrase": row["find_phrase"]}
                        if "find_phrase_across" in row:
                            asks["find"]["find_phrase_across"] = row["find_phrase_across"]
                    elif "context" in row:
                        context = row["context"]
                        asks["context"] = {
                            "find_phrase": context["after"],
                            "paragraphs_after": context["paragraphs_after"],
                        }
                        if context["across"]:
                            asks["context"]["find_phrase_across"] = context["across"]
                    for how, ask in asks.items():
                        try:
                            got = _run("live.select", {**base, **ask})
                            seen = _run("live.selection", base)
                        except DocControlError as err:
                            bad.append(f"{where}: {how} {row['at']}: {err.code.value}")
                            continue
                        ok = got == want and seen == {"text": want["text"], "paragraph": want}
                        bad.check(ok, f"{where}: {how} {row['at']}", got["text"][:40])
            bad.check(_run("live.status", base)["dirty"] is False, f"{kind}: select dirtied it")
    return bad


def refusals(word: Word, home: Path) -> list[str]:
    """A stale address, a missing or repeated phrase, and phrases across a paragraph mark."""
    bad = Failures()
    doc = make_docx(home / "refuse.docx", "One day.", "Twice.", "Twice.", "The end.")
    first = _read(doc)[0]["at"]
    with _opened(doc) as base:
        got = _run("live.select", {**base, "at": first, "paragraphs_after": 3})
        bad.check(got["text"] == "The end.", "paragraphs_after", got)
        _type(word, doc, "Now: ", start=True)
        asks: list[tuple[dict[str, Any], ErrorCode]] = [
            ({"at": first}, ErrorCode.STALE_ADDRESS),
            ({"at": "p9#00000000"}, ErrorCode.STALE_ADDRESS),
            ({"find_phrase": "nowhere"}, ErrorCode.ANCHOR_NOT_FOUND),
            ({"find_phrase": "twice."}, ErrorCode.ANCHOR_AMBIGUOUS),
            ({"find_phrase": "the END.", "paragraphs_after": 1}, ErrorCode.ANCHOR_NOT_FOUND),
            ({}, ErrorCode.SCHEMA_INVALID),
        ]
        for ask, code in asks:
            bad.refused(f"select {ask}", "live.select", {**base, **ask}, code)
        # A phrase across the mark between the two repeated paragraphs names the second.
        got = _run("live.select", {**base, "find_phrase": "Twice.\rTwice.\rThe"})
        bad.check(got["at"].startswith("p2#"), "phrase across marks", got)
        ask = {"find_phrase": "Twice.\rThe", "find_phrase_across": "previous"}
        got = _run("live.select", {**base, **ask})
        bad.check(got["at"].startswith("p4#"), "phrase across previous", got)
    return bad


def deletions(word: Word, home: Path) -> list[str]:
    """A phrase is matched against what the writer sees, past tracked deletions and across
    a deleted paragraph mark, whether the window shows the deletions or not."""
    bad = Failures()
    doc = make_docx(
        home / "deleted.docx",
        "Alpha bravo charlie.",
        "Other text here.",
        "Zed alpha charlie end.",
        "Quiet hidden unicorn tail.",
    )
    with api.open(doc) as handle:
        at = [p["at"] for p in handle.paragraphs()]
        cuts = [(0, 5, 11), (2, 3, 17), (3, 5, 20)]
        ops = [{"op": "text.replace", "at": f"{at[n]}:{a}-{b}", "text": ""} for n, a, b in cuts]
        handle.run("batch", {"ops": ops})
        rows = handle.run("locate", {})["paragraphs"]
    with _opened(doc) as base:
        for markup in (ALL_MARKUP, SIMPLE_MARKUP):
            _markup(word, doc, markup)
            got = _run("live.select", {**base, "find_phrase": "alpha charlie"})
            bad.check(got["at"] == rows[0]["at"], f"markup {markup}: past a deletion", got)
            got = _run("live.select", {**base, "find_phrase": "Quiet tail"})
            bad.check(got["text"] == "Quiet tail.", f"markup {markup}: deleted middle", got)
            chosen = _run("live.selection", base)
            bad.check(chosen["text"] == "Quiet tail.", f"markup {markup}: selection", chosen)
            for row in rows:
                got = _run("live.select", {**base, "find_phrase": row["find_phrase"]})
                bad.check(got["at"] == row["at"], f"markup {markup}: {row['at']}", got)
            asks: list[tuple[dict[str, Any], ErrorCode]] = [
                ({"find_phrase": "hidden unicorn"}, ErrorCode.ANCHOR_NOT_FOUND),
                (
                    {"find_phrase": "Other", "find_phrase_across": "previous"},
                    ErrorCode.SCHEMA_INVALID,
                ),
            ]
            for ask, code in asks:
                bad.refused(f"markup {markup}: {ask}", "live.select", {**base, **ask}, code)

    doc = make_docx(
        home / "crossing.docx",
        "Lead.",
        "Keep one gone tail",
        "unicorn head stays two.",
        "Third unicorn here.",
    )
    with api.open(doc) as handle:
        at = [p["at"] for p in handle.paragraphs()]
        cut = {"op": "text.replace", "at": f"{at[1]}:8..{at[2]}:13", "text": ""}
        handle.run("batch", {"ops": [cut]})
        found = handle.paragraphs()
    original = doc.read_bytes()
    for markup in (ALL_MARKUP, SIMPLE_MARKUP):
        with _opened(doc) as base:
            _markup(word, doc, markup)
            where = f"crossing, markup {markup}"
            for ask, n, text in [
                ({"find_phrase": "stays"}, 2, "stays two."),
                ({"at": found[2]["at"]}, 2, "stays two."),
                ({"find_phrase": "unicorn"}, 3, "Third unicorn here."),
                ({"at": found[1]["at"]}, 1, "Keep one"),
            ]:
                got = _run("live.select", {**base, **ask})
                bad.check(got == {"text": text, "at": found[n]["at"]}, f"{where}: {ask}", got)
                chosen = _run("live.selection", base)
                bad.check(chosen["paragraph"] == got, f"{where}: selection of {ask}", chosen)
            # Edits land where the address says, with deleted text before them.
            ops = [
                {"op": "text.replace", "at": f"{found[2]['at']}:6-9", "text": "2"},
                {"op": "comment.add", "at": f"{found[1]['at']}:5-8", "text": "Which?"},
                {"op": "para.insert", "after": found[1]["at"], "text": "Between."},
            ]
            _run("batch", {**base, "engine": "live", "author": AUTHOR, "ops": ops})
            _run("live.save", base)
        want = ["Lead.", "Keep one", "Between.", "stays 2.", "Third unicorn here."]
        bad.check(_texts(doc) == want, f"{where}: after edits", _texts(doc))
        comments = _run("comments.list", {"doc": str(doc)})["comments"]
        signed = [(c["author"], c["text"]) for c in comments]
        bad.check(signed == [(AUTHOR, "Which?")], f"{where}: comments", comments)
        doc.write_bytes(original)
    return bad


def roundtrip(word: Word, home: Path) -> list[str]:
    """What the live engine writes, the file engine reads back (``live_roundtrip_failures``),
    and Word's own settings are as the writer left them."""
    bad = Failures()
    texts = ["First paragraph here.", "Second paragraph stays.", "Third one to change.", "Last."]
    doc = make_docx(home / "roundtrip.docx", *texts)
    at = [p["at"] for p in _read(doc)]
    app = word._application()

    def settings() -> tuple[Any, ...]:
        return (
            str(app.UserName),
            str(app.UserInitials),
            bool(app.Options.UseLocalUserInfo),
            bool(word._doc(doc).TrackRevisions),
        )

    with _opened(doc) as base:
        before = settings()
        ops = [
            {"op": "comment.add", "at": f"{at[0]}:0-5", "text": "Why first?"},
            {"op": "para.insert", "after": at[1], "text": "Inserted line."},
            {"op": "text.replace", "at": f"{at[2]}:0-5", "text": "3rd"},
            {"op": "para.insert", "before": at[0], "text": "Title."},
        ]
        done = _run("batch", {**base, "engine": "live", "author": AUTHOR, "ops": ops})
        bad.check(done["applied"] == 4 and done["author"] == AUTHOR, "batch", done)
        bad.check(settings() == before, "settings after a batch", settings(), before)
        try:
            with word._as(word._doc(doc), AUTHOR):
                raise RuntimeError("mid-edit")
        except RuntimeError:
            pass
        bad.check(settings() == before, "settings after a failed edit", settings(), before)
        last = at[3].replace("p4#", "p6#")  # two paragraphs were inserted above it
        refused: list[tuple[dict[str, Any], ErrorCode]] = [
            ({"ops": [{"op": "text.replace", "at": at[2], "text": "x"}]}, ErrorCode.STALE_ADDRESS),
            ({"ops": [{"op": "para.delete", "at": last}]}, ErrorCode.ENGINE_UNAVAILABLE),
            ({"ops": [], "dry_run": True}, ErrorCode.ENGINE_UNAVAILABLE),
            (
                {
                    "ops": [
                        {"op": "text.replace", "at": f"{last}:0-3", "text": "x"},
                        {"op": "comment.add", "at": f"{last}:2-4", "text": "y"},
                    ]
                },
                ErrorCode.BATCH_CONFLICT,
            ),
        ]
        for request, code in refused:
            bad.refused(f"batch {request}", "batch", {**base, "engine": "live", **request}, code)
        _run("live.save", base)

    want = ["Title.", *texts[:2], "Inserted line.", "3rd one to change.", texts[3]]
    bad.check(_texts(doc) == want, "text", _texts(doc))  # unintended_text_changes
    base = {"doc": str(doc)}
    comments = _run("comments.list", base)["comments"]
    signed = [(c["author"], c["text"]) for c in comments]
    bad.check(signed == [(AUTHOR, "Why first?")], "comments", comments)
    revisions = _run("revisions.list", base)["revisions"]
    kinds = {r["type"] for r in revisions}
    bad.check({r["author"] for r in revisions} == {AUTHOR}, "revision authors", revisions)
    bad.check({"insert", "delete"} <= kinds, "revision kinds", kinds)
    history = _run("history.list", base)["entries"]
    bad.check([e.get("engine") for e in history[:4]] == ["live"] * 4, "history", history[:4])
    return bad


def refused_batch(word: Word, home: Path) -> list[str]:
    """Refusals come before the first edit: the open document stays as it was."""
    bad = Failures()
    doc = home / "refused.docx"
    doc.write_bytes(generate("tables", 1))
    at = [p["at"] for p in _read(doc)]
    comment = {"op": "comment.add", "at": at[-1], "text": "Sure?"}
    insert = {"op": "para.insert", "after": at[0], "text": "New."}

    def across(a: int, b: int) -> dict[str, Any]:
        return {"op": "text.replace", "at": f"{at[a]}:2..{at[b]}:1", "text": "X"}

    with _opened(doc) as base:
        refused: list[tuple[dict[str, Any], ErrorCode]] = [
            # p6 is the heading before the table, p7 and p8 its first two cells.
            ({"ops": [comment, across(5, 6)]}, ErrorCode.UNSUPPORTED_CONTENT),
            ({"ops": [comment, across(6, 7)]}, ErrorCode.UNSUPPORTED_CONTENT),
            ({"ops": [comment, insert, insert]}, ErrorCode.BATCH_CONFLICT),
            ({"ops": [comment], "expect": {"sha256": "0" * 64}}, ErrorCode.ENGINE_UNAVAILABLE),
        ]
        for request, code in refused:
            bad.refused(f"batch {request}", "batch", {**base, "engine": "live", **request}, code)
            bad.check(_run("live.status", base)["dirty"] is False, f"dirty after {request}")
    return bad


SCENARIOS: list[Callable[[Word, Path], list[str]]] = [
    session,
    select,
    refusals,
    deletions,
    roundtrip,
    refused_batch,
]


def run_all(word: Word, home: Path) -> list[str]:
    """Every scenario; one that breaks is a failure, and the rest still run."""
    failures: list[str] = []
    for scenario in SCENARIOS:
        folder = home / scenario.__name__
        folder.mkdir()
        try:
            found = scenario(word, folder)
        except Exception as err:
            found = [f"{type(err).__name__}: {err}"]
        failures += [f"{scenario.__name__}: {failure}" for failure in found]
    return failures
