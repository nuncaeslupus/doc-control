"""`locate`: where a paragraph is (chapter, scene, estimated page) and how to find it again.

For each paragraph the result carries

* ``chapter`` (id, title, number, kind) and ``scene`` (id, ordinal within its chapter), from
  :mod:`doc_control.model.structure` (same ``overrides`` and ``profile`` as `segment`, so the
  ids agree); ``null`` before the first chapter or scene;
* ``page`` and ``page_label`` from :mod:`doc_control.model.pages`: always an estimate;
* a *locator phrase*: the shortest ``head … tail`` made of the paragraph's first N and last M
  words that no other paragraph of the document also has.

Locator phrase
    Words are the paragraph text split on whitespace (punctuation stays on the word). Two
    words are the same when they fold the same under case- and accent-insensitive search
    (:func:`doc_control.model.search.fold_text`), so the phrase stays unique under the loosest
    `find` options. A paragraph *matches* ``(head, tail)`` when it has at least N + M words,
    starts with the head words and ends with the tail words. The search tries N + M = 2, 3, ...
    (N and M at least 1, at most :data:`MAX_SIDE` each, larger N first among equals) and takes
    the first pair only this paragraph matches. When N + M reaches the paragraph's word count the
    phrase is the whole text (``complete: true``) and matches by equality. The phrase is
    ``null`` with a ``reason``: ``empty`` (no words), ``duplicate`` (another paragraph has the
    same words) or ``not_unique_within_limit`` (long, and every head/tail of up to
    :data:`MAX_SIDE` words is shared). In the phrase, runs of whitespace are single spaces.

Cost: the indexes (prefix and suffix word tuples, up to ``MAX_SIDE`` long), the structure and
the page estimates are built once per call, then each paragraph is a handful of set
lookups. Whole-document mode is paged and each response builds only its own rows.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, overload

from doc_control.envelope import Reply
from doc_control.model.document import Document
from doc_control.model.pages import estimate_pages
from doc_control.model.search import fold_text
from doc_control.model.structure import detect, paragraphs_of
from doc_control.ops.paging import PAGING_PROPERTIES, paged
from doc_control.ops.segment import (
    OVERRIDES_SCHEMA,
    PROFILE_SCHEMA,
    overrides_of,
    profile_of,
)
from doc_control.registry import operation

MAX_SIDE = 12
SEPARATOR = " … "
_TITLE = 160


@dataclass(frozen=True)
class Locator:
    phrase: str | None
    head: int = 0
    tail: int = 0
    complete: bool = False
    reason: str | None = None


class LocatorIndex:
    """Word prefixes and suffixes of every paragraph, to answer 'who else starts/ends so?'."""

    def __init__(self, texts: list[str]) -> None:
        self.words = [t.split() for t in texts]
        self.keys = [tuple(fold_text(w) for w in ws) for ws in self.words]
        self.prefixes: dict[tuple[str, ...], set[int]] = {}
        self.suffixes: dict[tuple[str, ...], set[int]] = {}
        self.whole: dict[tuple[str, ...], int] = {}
        for i, key in enumerate(self.keys):
            if not key:
                continue
            self.whole[key] = self.whole.get(key, 0) + 1
            for n in range(1, min(len(key), MAX_SIDE) + 1):
                self.prefixes.setdefault(key[:n], set()).add(i)
                self.suffixes.setdefault(key[-n:], set()).add(i)

    def matches(self, i: int, head: int, tail: int) -> int:
        """Paragraphs with at least ``head + tail`` words that start and end like paragraph i."""
        key = self.keys[i]
        starts = self.prefixes[key[:head]]
        ends = self.suffixes[key[-tail:]]
        small, big = (starts, ends) if len(starts) <= len(ends) else (ends, starts)
        need = head + tail
        return sum(1 for j in small if j in big and len(self.keys[j]) >= need)

    def locator(self, i: int) -> Locator:
        key = self.keys[i]
        count = len(key)
        if count == 0:
            return Locator(None, reason="empty")
        if self.whole[key] > 1:
            return Locator(None, reason="duplicate")
        words = self.words[i]
        for total in range(2, min(count, 2 * MAX_SIDE + 1)):
            for head in range(min(total - 1, MAX_SIDE), 0, -1):
                tail = total - head
                if tail > MAX_SIDE:
                    break
                if self.matches(i, head, tail) == 1:
                    phrase = " ".join(words[:head]) + SEPARATOR + " ".join(words[-tail:])
                    return Locator(phrase, head, tail)
        if count <= 2 * MAX_SIDE:
            return Locator(" ".join(words), count, 0, complete=True)
        return Locator(None, reason="not_unique_within_limit")


class _Rows(Sequence[dict[str, Any]]):
    """Rows built only for the slice a page asks for; the indexes behind them are shared."""

    def __init__(self, numbers: list[int], build: Callable[[int], dict[str, Any]]) -> None:
        self._numbers = numbers
        self._build = build

    def __len__(self) -> int:
        return len(self._numbers)

    @overload
    def __getitem__(self, item: int) -> dict[str, Any]: ...
    @overload
    def __getitem__(self, item: slice) -> list[dict[str, Any]]: ...
    def __getitem__(self, item: int | slice) -> dict[str, Any] | list[dict[str, Any]]:
        if isinstance(item, slice):
            return [self._build(n) for n in self._numbers[item]]
        return self._build(self._numbers[item])


@operation(
    "locate",
    "read",
    "Chapter, scene, estimated page and the shortest unique first-and-last-words phrase of "
    "each paragraph (one, a range, or the whole document, paged).",
    {
        "type": "object",
        "properties": {
            "doc": {"type": "string", "minLength": 1, "description": "Path to the .docx."},
            "at": {
                "type": "string",
                "minLength": 1,
                "description": "Paragraph address or range (offsets ignored), e.g. p142#a7b2a7b2; "
                "default the whole document (empty paragraphs left out).",
            },
            "overrides": OVERRIDES_SCHEMA,
            "profile": PROFILE_SCHEMA,
            **PAGING_PROPERTIES,
        },
        "required": ["doc"],
        "additionalProperties": False,
    },
)
def locate(request: dict[str, Any]) -> Reply:
    doc = Document(request["doc"])
    index = doc.index
    count = len(index)
    texts = [index.paragraph(n).text for n in range(1, count + 1)]
    found = detect(paragraphs_of(doc), profile_of(request), overrides_of(request))
    pages = estimate_pages(doc)
    locators = LocatorIndex(texts)

    chapter_at = [0] * (count + 2)  # 1-based paragraph -> 1-based chapter, 0 for none
    for k, chapter in enumerate(found.chapters, 1):
        for n in range(chapter.first, chapter.last + 1):
            chapter_at[n] = k
    scene_at = [0] * (count + 2)
    ordinal: dict[str | None, int] = {}
    ordinals = [0]
    for k, scene in enumerate(found.scenes, 1):
        ordinal[scene.chapter] = ordinal.get(scene.chapter, 0) + 1
        ordinals.append(ordinal[scene.chapter])
        for n in range(scene.first, scene.last + 1):
            scene_at[n] = k

    if "at" in request:
        resolved = index.resolve_range(request["at"])
        numbers = range(resolved.first.number, resolved.last.number + 1)
    else:
        numbers = range(1, count + 1)
    wanted = [n for n in numbers if "at" in request or texts[n - 1].strip()]

    def row(n: int) -> dict[str, Any]:
        page = pages.rows[n - 1]
        chapter = found.chapters[chapter_at[n] - 1] if chapter_at[n] else None
        scene = found.scenes[scene_at[n] - 1] if scene_at[n] else None
        loc = locators.locator(n - 1)
        out: dict[str, Any] = {
            "at": str(index.paragraph(n).address),
            "chapter": None
            if chapter is None
            else {
                "id": chapter.id,
                "title": " ".join(index.paragraph(chapter.first).text.split())[:_TITLE],
                "number": chapter.number,
                "kind": chapter.kind,
            },
            "scene": None if scene is None else {"id": scene.id, "ordinal": ordinals[scene_at[n]]},
            "page": page.page,
            "page_label": page.label,
            "phrase": loc.phrase,
        }
        if loc.phrase is None:
            out["reason"] = loc.reason
        else:
            out["head_words"] = loc.head
            out["tail_words"] = loc.tail
            if loc.complete:
                out["complete"] = True
        return out

    head: dict[str, Any] = {"estimated": True, "page_count": pages.pages, "basis": pages.basis}
    return paged("locate", doc, request, "paragraphs", _Rows(wanted, row), head)
