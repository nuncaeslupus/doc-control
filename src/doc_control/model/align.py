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

Algorithm (near-linear on real documents; every quadratic step is capped, see the
constants: difflib only on gaps up to DIFFLIB_CELLS, split/merge runs up to
MAX_GROUP_RUN, candidate pairs per paragraph up to TOP_CANDIDATES)
    1. Anchors: paragraphs whose fingerprint is unique on both sides are paired; the
       longest increasing chain of them is "in order", the rest are moves.
    2. Repeated and wordless paragraphs (titles, separators, short replies) are paired
       by context: next to a paired neighbour, with the partner's neighbour when the
       text is equal, nearest context first, so a moved block carries its repeated
       lines. What is left is aligned in order, gap by gap, then by identical text.
    3. Split and merge: a leftover paragraph whose words reappear (>= GROUP_CONTAINMENT
       of each part's words) in a run of two or more adjacent leftover paragraphs of
       the other side, the run's words matching it at >= GROUP_MIN_SIMILARITY.
    4. Modified: candidate pairs from the same gap between anchors and from a rare-word
       index, scored by word-level difflib ratio (character-level under SHORT_WORDS
       words), assigned greedily best first at >= MODIFIED_MIN.
    Blank paragraphs (no word characters) are matched only in step 2.
"""

from __future__ import annotations

import bisect
import heapq
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
MAX_GROUP_RUN = 8  # a split/merge has at most this many parts (longer runs are not tried)
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


def _unique_pairs(a: Sequence[str], b: Sequence[str]) -> list[tuple[int, int]]:
    """Pairs of paragraphs whose fingerprint occurs exactly once on each side."""
    count_a, count_b = Counter(a), Counter(b)
    pos_b = {fp: j for j, fp in enumerate(b) if count_b[fp] == 1}
    return [(i, pos_b[fp]) for i, fp in enumerate(a) if count_a[fp] == 1 and fp in pos_b]


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
    """Drop end paragraphs of a run (at most MAX_GROUP_RUN long) while that improves the match."""
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
        unique = _unique_pairs([p.fp for p in self.old], [p.fp for p in self.new])
        chain = _lis(unique)
        in_chain = set(chain)
        self.base_old = [i for i, _ in chain]
        self.base_new = [j for _, j in chain]
        extras = [(i, j, False) for i, j in unique if (i, j) not in in_chain]
        for i, j in unique:
            self.pair(i, j)
        extras += self.extend()
        extras += self.fill_gaps()
        extras += self.exact_leftovers()
        self.groups(split=True)
        self.groups(split=False)
        extras += self.fuzzy()
        return self.assemble(extras)

    def extend(self) -> list[tuple[int, int, bool]]:
        """Pair repeated and wordless paragraphs by context.

        A paragraph next to an already paired one is paired with the partner's
        neighbour on the same side when the two have the same text, so a moved block
        carries its repeated lines (titles, separators, short replies) with it. Blocks
        claim neighbours: those next to an anchor first, then next to what was paired from
        an anchor, and so on (nearest context wins), the largest block first within a level.
        """
        run_len = [0] * len(self.old)  # length of the run of consecutive pairs each is in
        i = 0
        while i < len(self.old):
            if self.match_old[i] < 0:
                i += 1
                continue
            k = i
            while k + 1 < len(self.old) and self.match_old[k + 1] == self.match_old[k] + 1:
                k += 1
            for t in range(i, k + 1):
                run_len[t] = k - i + 1
            i = k + 1
        heap: list[tuple[int, int, int, int]] = []  # (depth, -weight, old, new)

        def push(o: int, n: int, weight: int, depth: int) -> None:
            for step in (-1, 1):
                io, jn = o + step, n + step
                if (
                    0 <= io < len(self.old)
                    and 0 <= jn < len(self.new)
                    and self.match_old[io] < 0
                    and self.match_new[jn] < 0
                    and self.old[io].fp == self.new[jn].fp
                ):
                    heapq.heappush(heap, (depth, -weight, io, jn))

        for o, n in enumerate(self.match_old):
            if n >= 0:
                push(o, n, run_len[o], 1)
        out: list[tuple[int, int, bool]] = []
        votes: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # fp -> [with next, with prev]
        deferred: list[int] = []

        def candidates(io: int) -> list[tuple[int, int]]:
            """(new index, side) for line ``io``; side 0 = with its next paragraph, 1 = prev."""
            found: list[tuple[int, int]] = []
            for side, step in ((0, 1), (1, -1)):
                nb = io + step
                if 0 <= nb < len(self.old) and self.match_old[nb] >= 0:
                    jn = self.match_old[nb] - step
                    if (
                        0 <= jn < len(self.new)
                        and self.match_new[jn] < 0
                        and self.new[jn].fp == self.old[io].fp
                    ):
                        found.append((jn, side))
            return found

        def accept(io: int, jn: int, weight: int, depth: int) -> None:
            self.pair(io, jn)
            out.append((io, jn, False))
            push(io, jn, weight, depth + 1)

        while True:
            while heap:
                depth, neg, io, jn = heapq.heappop(heap)
                if self.match_old[io] >= 0 or self.match_new[jn] >= 0:
                    continue
                found = candidates(io)
                if len({j for j, _ in found}) > 1:  # the neighbours disagree: decide later
                    deferred.append(io)
                    continue
                if len(found) == 1 and self.decisive(io, 1 - found[0][1]):
                    votes[self.old[io].fp][found[0][1]] += 1
                accept(io, jn, -neg, depth)
            # Ambiguous lines (a heading between the end of one block and the start of the
            # next): a line goes with the side its text usually goes with, headings with
            # the paragraph after them and closing lines with the one before; undecided
            # ones go with the next paragraph.
            progressed = False
            for io in deferred:
                if self.match_old[io] >= 0:
                    continue
                found = candidates(io)
                if len({j for j, _ in found}) < 2:
                    if found:
                        accept(io, found[0][0], 1, 1)
                        progressed = True
                    continue
                with_next, with_prev = votes[self.old[io].fp]
                want = 1 if with_prev > with_next else 0
                jn = next(j for j, side in found if side == want)
                accept(io, jn, 1, 1)
                progressed = True
                break  # its neighbours may now settle other lines: drain the heap first
            if not progressed:
                return out
            deferred = [i for i in deferred if self.match_old[i] < 0]

    def decisive(self, io: int, side: int) -> bool:
        """Does the neighbour on ``side`` (0 next, 1 prev) of old line ``io`` tell something?

        It does when it is paired (so its refusal to host the line counts) or when the
        document ends there; an unpaired neighbour (edited, split) says nothing.
        """
        nb = io + (1 if side == 0 else -1)
        return not 0 <= nb < len(self.old) or self.match_old[nb] >= 0

    def fill_gaps(self) -> list[tuple[int, int, bool]]:
        """Pair what is left of identical text in order, gap by gap between anchors."""
        slots_old: dict[int, list[int]] = defaultdict(list)
        slots_new: dict[int, list[int]] = defaultdict(list)
        for i in range(len(self.old)):
            if self.match_old[i] < 0:
                slots_old[bisect.bisect_left(self.base_old, i)].append(i)
        for j in range(len(self.new)):
            if self.match_new[j] < 0:
                slots_new[bisect.bisect_left(self.base_new, j)].append(j)
        out: list[tuple[int, int, bool]] = []
        for slot, olds in slots_old.items():
            news = slots_new.get(slot)
            if not news or len(olds) * len(news) > DIFFLIB_CELLS:
                continue
            matcher = SequenceMatcher(
                None,
                [self.old[i].fp for i in olds],
                [self.new[j].fp for j in news],
                autojunk=False,
            )
            for block in matcher.get_matching_blocks():
                for k in range(block.size):
                    i, j = olds[block.a + k], news[block.b + k]
                    self.pair(i, j)
                    out.append((i, j, False))
        return out

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
                if len(run) < 2 or len(run) > MAX_GROUP_RUN:
                    continue
                # ratio >= GROUP_MIN_SIMILARITY needs the parts' words within [2/3, 3/2] of
                # the paragraph's; a run with fewer words in all cannot reach it by trimming.
                if sum(len(many_words[m]) for m in run) * 3 < 2 * len(one_words[o]):
                    continue
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
