"""Paragraph alignment between two versions of a document (the engine of ``diff``).

:func:`align` takes the paragraphs of an old and a new version (anything with ``text``
and ``fp``, e.g. :class:`doc_control.model.index.Paragraph`) and returns an
:class:`Alignment`: a list of :class:`Change` records, each saying what became of one
or several paragraphs. Only visible text is compared; formatting-only edits are not seen.

Kinds
    ``unchanged``  same text (same fingerprint), possibly at another number
    ``modified``   same paragraph with edited text (``similarity`` >= MODIFIED_MIN)
    ``moved``      same text, out of order with respect to its neighbours
    ``split``      one old paragraph became several adjacent new ones
    ``merged``     several adjacent old paragraphs became one new one
    ``deleted``    only in the old version
    ``inserted``   only in the new version
A ``modified`` paragraph that also changed place carries ``moved=True``.

Algorithm (close to O(n log n) on real documents, never O(n^2) over the whole text)
    1. Exact fingerprints: anchor on fingerprints that are unique on both sides, keep
       the longest increasing chain (patience diff), recurse into the gaps between
       anchors; a gap with no anchor goes to difflib if small. The chain is "in order".
    2. Leftover paragraphs with identical text are paired (these are the moves).
    3. Split and merge: a leftover paragraph whose words reappear (>= GROUP_CONTAINMENT
       of each part's words) in a run of two or more adjacent leftover paragraphs of
       the other side, the run's words matching it at >= GROUP_MIN_SIMILARITY.
    4. Modified: candidate pairs from the same gap between anchors and from a rare-word
       index, scored by word-level difflib ratio (character-level under SHORT_WORDS
       words), assigned greedily best first at >= MODIFIED_MIN.
    Blank paragraphs (no word characters) are matched only in step 1.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

MODIFIED_MIN = 0.6  # word-level similarity from which two paragraphs are "the same, edited"
GROUP_MIN_SIMILARITY = 0.8  # similarity of one paragraph with the concatenation of its parts
GROUP_CONTAINMENT = 0.8  # share of a part's words found in the paragraph it came from
GROUP_MIN_WORDS = 6  # smaller paragraphs are never split/merge candidates
SHORT_WORDS = 6  # under this many words (either side) similarity is character-level
RARE_POSTINGS = 20  # a word in more leftover paragraphs than this does not nominate candidates
GAP_PAIRS = 400  # all pairs inside a gap are scored when the gap is at most this big
TOP_CANDIDATES = 4  # index nominations kept per paragraph
DIFFLIB_CELLS = 1_000_000  # a gap without anchors is aligned by difflib up to this many cells

UNCHANGED = "unchanged"
MODIFIED = "modified"
MOVED = "moved"
SPLIT = "split"
MERGED = "merged"
DELETED = "deleted"
INSERTED = "inserted"
KINDS = (UNCHANGED, MODIFIED, MOVED, SPLIT, MERGED, DELETED, INSERTED)

_WORD = re.compile(r"\w+")


class _Para(Protocol):
    @property
    def text(self) -> str: ...
    @property
    def fp(self) -> str: ...


@dataclass(frozen=True)
class Change:
    """What became of ``old`` paragraphs (0-based indexes) in the new version (``new``)."""

    kind: str
    old: tuple[int, ...]
    new: tuple[int, ...]
    similarity: float | None = None  # modified, split, merged
    moved: bool = False  # modified paragraphs that also changed place


@dataclass(frozen=True)
class Alignment:
    """Every paragraph of both versions appears in exactly one change, in old order."""

    changes: tuple[Change, ...]
    old_count: int
    new_count: int

    def counts(self) -> dict[str, int]:
        counter = Counter(c.kind for c in self.changes)
        return {k: counter.get(k, 0) for k in KINDS}

    def new_for_old(self) -> dict[int, tuple[int, ...]]:
        """Old index -> new indexes (empty tuple for a deleted paragraph)."""
        return {o: c.new for c in self.changes for o in c.old}


def _words(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFC", text).casefold())


def _lis(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest chain increasing in the second element, for pairs sorted by the first."""
    tails: list[int] = []  # smallest tail value of a chain of each length
    tail_at: list[int] = []  # index into pairs of that tail
    prev = [-1] * len(pairs)
    for i, (_, n) in enumerate(pairs):
        k = bisect.bisect_left(tails, n)
        if k == len(tails):
            tails.append(n)
            tail_at.append(i)
        else:
            tails[k] = n
            tail_at[k] = i
        prev[i] = tail_at[k - 1] if k else -1
    out: list[tuple[int, int]] = []
    i = tail_at[-1] if tail_at else -1
    while i >= 0:
        out.append(pairs[i])
        i = prev[i]
    out.reverse()
    return out


def _exact_pairs(a: Sequence[str], b: Sequence[str]) -> list[tuple[int, int]]:
    """Fingerprint-equal pairs, increasing in both indexes: anchors, then gaps."""
    pairs: list[tuple[int, int]] = []
    stack = [(0, len(a), 0, len(b))]
    while stack:
        alo, ahi, blo, bhi = stack.pop()
        while alo < ahi and blo < bhi and a[alo] == b[blo]:
            pairs.append((alo, blo))
            alo += 1
            blo += 1
        while alo < ahi and blo < bhi and a[ahi - 1] == b[bhi - 1]:
            ahi -= 1
            bhi -= 1
            pairs.append((ahi, bhi))
        if alo >= ahi or blo >= bhi:
            continue
        count_a = Counter(a[alo:ahi])
        count_b = Counter(b[blo:bhi])
        pos_b = {b[j]: j for j in range(blo, bhi) if count_b[b[j]] == 1}
        anchors = [
            (i, pos_b[a[i]]) for i in range(alo, ahi) if count_a[a[i]] == 1 and a[i] in pos_b
        ]
        if anchors:
            chain = _lis(anchors)
            pairs.extend(chain)
            last_a, last_b = alo, blo
            for i, j in chain:
                stack.append((last_a, i, last_b, j))
                last_a, last_b = i + 1, j + 1
            stack.append((last_a, ahi, last_b, bhi))
        elif (ahi - alo) * (bhi - blo) <= DIFFLIB_CELLS:
            matcher = SequenceMatcher(None, a[alo:ahi], b[blo:bhi], autojunk=False)
            for block in matcher.get_matching_blocks():
                pairs.extend((alo + block.a + k, blo + block.b + k) for k in range(block.size))
    pairs.sort()
    return pairs


def _ratio(a: Sequence[str], b: Sequence[str], floor: float) -> float:
    """difflib ratio of two sequences, 0.0 when it cannot reach ``floor``."""
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
        return 0.0
    return matcher.ratio()


def _runs(sorted_indexes: list[int]) -> list[list[int]]:
    """Split a sorted list into maximal runs of consecutive integers."""
    runs: list[list[int]] = []
    for m in sorted_indexes:
        if runs and runs[-1][-1] == m - 1:
            runs[-1].append(m)
        else:
            runs.append([m])
    return runs


def _trim_run(run: list[int], target: list[str], many: list[list[str]]) -> list[int]:
    """Drop end paragraphs of a run while that improves its match with ``target``."""
    while len(run) > 2:
        full = _ratio(target, [w for m in run for w in many[m]], 0.0)
        head = _ratio(target, [w for m in run[1:] for w in many[m]], 0.0)
        tail = _ratio(target, [w for m in run[:-1] for w in many[m]], 0.0)
        if max(head, tail) <= full:
            break
        run = run[1:] if head >= tail else run[:-1]
    return run


class _Aligner:
    def __init__(self, old: Sequence[_Para], new: Sequence[_Para]) -> None:
        self.old = old
        self.new = new
        self.words_old = [_words(p.text) for p in old]
        self.words_new = [_words(p.text) for p in new]
        self.match_old: list[int] = [-1] * len(old)  # 1:1 partner, -1 when none
        self.match_new: list[int] = [-1] * len(new)
        self.group_old: set[int] = set()  # claimed by a split/merge
        self.group_new: set[int] = set()
        self.changes: list[Change] = []
        self.scores: dict[tuple[int, int], float] = {}
        self.base_old: list[int] = []  # anchor chain (step 1), increasing in both
        self.base_new: list[int] = []

    def free_old(self, i: int) -> bool:
        return self.match_old[i] < 0 and i not in self.group_old and bool(self.words_old[i])

    def free_new(self, j: int) -> bool:
        return self.match_new[j] < 0 and j not in self.group_new and bool(self.words_new[j])

    def pair(self, i: int, j: int) -> None:
        self.match_old[i] = j
        self.match_new[j] = i

    def in_order(self, i: int, j: int) -> bool:
        """Is (i, j) consistent with the anchor chain, i.e. not a move?"""
        k = bisect.bisect_left(self.base_old, i)
        before = self.base_new[k - 1] if k else -1
        after = self.base_new[k] if k < len(self.base_new) else len(self.new)
        return before < j < after

    def run(self) -> Alignment:
        exact = _exact_pairs([p.fp for p in self.old], [p.fp for p in self.new])
        for i, j in exact:
            self.pair(i, j)
        self.base_old = [i for i, _ in exact]
        self.base_new = [j for _, j in exact]
        extras = self.exact_leftovers()
        self.groups(split=True)
        self.groups(split=False)
        extras += self.fuzzy()
        return self.assemble(extras)

    def exact_leftovers(self) -> list[tuple[int, int, bool]]:
        """Identical text left over after step 1, as (old, new, is_modified=False)."""
        by_fp: dict[str, list[int]] = defaultdict(list)
        for j in range(len(self.new)):
            if self.free_new(j):
                by_fp[self.new[j].fp].append(j)
        taken: Counter[str] = Counter()
        out: list[tuple[int, int, bool]] = []
        for i in range(len(self.old)):
            if not self.free_old(i):
                continue
            fp = self.old[i].fp
            js = by_fp.get(fp, [])
            if taken[fp] < len(js):
                j = js[taken[fp]]
                taken[fp] += 1
                self.pair(i, j)
                out.append((i, j, False))
        return out

    def groups(self, *, split: bool) -> None:
        """Split (one old -> several new) or merge (several old -> one new) detection."""
        one_free: Callable[[int], bool]
        many_free: Callable[[int], bool]
        if split:
            one_words, many_words = self.words_old, self.words_new
            one_free, many_free = self.free_old, self.free_new
        else:
            one_words, many_words = self.words_new, self.words_old
            one_free, many_free = self.free_new, self.free_old
        index: dict[str, list[int]] = defaultdict(list)
        for m in range(len(many_words)):
            if many_free(m):
                for w in set(many_words[m]):
                    index[w].append(m)
        order = sorted(
            (
                o
                for o in range(len(one_words))
                if one_free(o) and len(one_words[o]) >= GROUP_MIN_WORDS
            ),
            key=lambda o: -len(one_words[o]),
        )
        for o in order:
            if not one_free(o):
                continue
            vocab = set(one_words[o])
            votes: Counter[int] = Counter()
            for w in vocab:
                posting = index.get(w, ())
                if len(posting) <= RARE_POSTINGS * 5:
                    votes.update(posting)
            parts = sorted(
                m
                for m in votes
                if many_free(m)
                and sum(w in vocab for w in many_words[m]) >= GROUP_CONTAINMENT * len(many_words[m])
            )
            for run in _runs(parts):
                run = _trim_run(run, one_words[o], many_words)
                if len(run) < 2:
                    continue
                joined = [w for m in run for w in many_words[m]]
                score = round(_ratio(one_words[o], joined, GROUP_MIN_SIMILARITY), 4)
                if score < GROUP_MIN_SIMILARITY:
                    continue
                key = tuple(run)
                if split:
                    self.changes.append(Change(SPLIT, (o,), key, score))
                    self.group_old.add(o)
                    self.group_new.update(key)
                else:
                    self.changes.append(Change(MERGED, key, (o,), score))
                    self.group_new.add(o)
                    self.group_old.update(key)
                break

    def fuzzy(self) -> list[tuple[int, int, bool]]:
        """Modified paragraphs: 1:1 pairs of leftovers above MODIFIED_MIN, best first."""
        olds = [i for i in range(len(self.old)) if self.free_old(i)]
        news = [j for j in range(len(self.new)) if self.free_new(j)]
        if not olds or not news:
            return []
        candidates: set[tuple[int, int]] = set()
        slot_old: dict[int, list[int]] = defaultdict(list)
        slot_new: dict[int, list[int]] = defaultdict(list)
        for i in olds:
            slot_old[bisect.bisect_left(self.base_old, i)].append(i)
        for j in news:
            slot_new[bisect.bisect_left(self.base_new, j)].append(j)
        for slot, gap_old in slot_old.items():
            gap_new = slot_new.get(slot, [])
            if len(gap_old) * len(gap_new) <= GAP_PAIRS:
                candidates.update((i, j) for i in gap_old for j in gap_new)
        index: dict[str, list[int]] = defaultdict(list)
        for i in olds:
            for w in set(self.words_old[i]):
                index[w].append(i)
        for j in news:
            votes: Counter[int] = Counter()
            for w in set(self.words_new[j]):
                posting = index.get(w, ())
                if len(posting) <= RARE_POSTINGS:
                    votes.update(posting)
            candidates.update((i, j) for i, _ in votes.most_common(TOP_CANDIDATES))
        scored = sorted(
            ((self.similarity(i, j), i, j) for i, j in candidates),
            key=lambda t: (-t[0], t[1], t[2]),
        )
        out: list[tuple[int, int, bool]] = []
        for score, i, j in scored:
            if score < MODIFIED_MIN:
                break
            if self.match_old[i] < 0 and self.match_new[j] < 0:
                self.pair(i, j)
                out.append((i, j, True))
                self.scores[(i, j)] = score
        return out

    def similarity(self, i: int, j: int) -> float:
        wa, wb = self.words_old[i], self.words_new[j]
        if min(len(wa), len(wb)) < SHORT_WORDS and max(len(wa), len(wb)) < 40:
            return _ratio(" ".join(wa), " ".join(wb), MODIFIED_MIN)
        return _ratio(wa, wb, MODIFIED_MIN)

    def assemble(self, extras: list[tuple[int, int, bool]]) -> Alignment:
        # Pairs the anchor chain did not hold are moves, unless they sit between their
        # anchors and do not cross one another.
        kept = set(_lis(sorted((i, j) for i, j, _ in extras if self.in_order(i, j))))
        modified = {(i, j) for i, j, mod in extras if mod}
        base = set(zip(self.base_old, self.base_new, strict=True))
        for i, j in sorted(base | {(i, j) for i, j, _ in extras}):
            moved = (i, j) not in base and (i, j) not in kept
            if (i, j) in modified:
                score = round(self.scores[(i, j)], 4)
                self.changes.append(Change(MODIFIED, (i,), (j,), score, moved))
            else:
                self.changes.append(Change(MOVED if moved else UNCHANGED, (i,), (j,)))
        claimed_old = {i for c in self.changes for i in c.old}
        claimed_new = {j for c in self.changes for j in c.new}
        self.changes.extend(
            Change(DELETED, (i,), ()) for i in range(len(self.old)) if i not in claimed_old
        )
        self.changes.extend(
            Change(INSERTED, (), (j,)) for j in range(len(self.new)) if j not in claimed_new
        )
        # An insertion sorts right after the old position of the new paragraph before it.
        owner_of_new: dict[int, int] = {}
        for c in self.changes:
            for j in c.new:
                if c.old:
                    owner_of_new[j] = c.old[-1]
        pred: list[int] = []
        last = -1
        for j in range(len(self.new)):
            last = owner_of_new.get(j, last)
            pred.append(last)

        def key(change: Change) -> tuple[float, int]:
            if change.old:
                return (float(change.old[0]), change.new[0] if change.new else -1)
            return (pred[change.new[0]] + 0.5, change.new[0])

        return Alignment(tuple(sorted(self.changes, key=key)), len(self.old), len(self.new))


def align(old: Sequence[_Para], new: Sequence[_Para]) -> Alignment:
    """Align the paragraphs of two versions; see the module docstring."""
    return _Aligner(old, new).run()
