"""Normalised text: NFC, no soft hyphens or zero-width characters, collapsed whitespace."""

from __future__ import annotations

import json
import random
import unicodedata

import pytest

from doc_control.cli import main
from doc_control.model.normalized import IGNORABLE, NormalizedText, normalize

SEEDS = range(3000)
ALPHABET = [
    "a", "e", "n", "Z", "1", ".", "\u2014", "\u00f1", "\u00e9", "\u00df",
    "\ud55c", "\u1100", "\u1161", "\u11a8",
    " ", " ", "\t", "\n", "\u00a0", "\u2003", "\u2028",
    "\u00ad", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff",
    "\u0301", "\u0323", "\u0302", "\u0308", "\u0344", "\u0958", "\u212b", "\u1e9b",
    "\U0001f600", "\U0001d11e", "\U0001f1ea", "\U00020000",
]  # fmt: skip


def random_text(seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randrange(0, 14)))


def boundaries(result: NormalizedText) -> list[int]:
    """Normalised boundaries that have an original counterpart (not inside a cluster)."""
    t = result.to_original
    return [i for i in range(len(t)) if not 0 < i < len(t) - 1 or t[i] != t[i - 1]]


def roundtrip_mismatches(text: str) -> int:
    result = normalize(text)
    return sum(
        1 for i in boundaries(result) if result.normalized_offset(result.original_offset(i)) != i
    )


def test_offset_map_roundtrip_collapsed_to_visible() -> None:
    """Every normalised offset maps to the visible text and back unchanged."""
    mismatches = sum(roundtrip_mismatches(random_text(seed)) for seed in SEEDS)
    checked = sum(len(boundaries(normalize(random_text(seed)))) for seed in SEEDS)
    print(f"offset_map_roundtrip_mismatches={mismatches}")
    print(f"offsets_checked={checked}")
    assert checked > 10000
    assert mismatches == 0


@pytest.mark.parametrize("seed", range(0, 3000, 5))
def test_maps_are_monotonic_and_consistent(seed: int) -> None:
    text = random_text(seed)
    result = normalize(text)
    to_o, to_n = result.to_original, result.to_normalized
    assert len(to_o) == len(result.text) + 1
    assert len(to_n) == len(text) + 1
    assert to_o[0] == 0
    assert to_o[-1] == (len(text) if result.text else 0)
    assert to_n[0] == 0 and to_n[-1] == len(result.text)
    assert list(to_o) == sorted(to_o)
    assert list(to_n) == sorted(to_n)
    # A boundary maps back at or before itself, and mapping is then stable.
    for i in range(len(to_o)):
        assert to_n[to_o[i]] <= i
        assert to_o[to_n[to_o[i]]] == to_o[i]
    for o in range(len(to_n)):
        assert to_o[to_n[o]] <= o
        assert to_n[to_o[to_n[o]]] == to_n[o]


@pytest.mark.parametrize("seed", range(600))
def test_text_matches_independent_normalisation(seed: int) -> None:
    text = random_text(seed)
    expected = unicodedata.normalize("NFC", "".join(c for c in text if c not in IGNORABLE))
    expected = " ".join(expected.replace("\u00a0", " ").split()) if False else expected
    got = normalize(text).text
    # Whitespace collapsing is the only difference from plain NFC.
    squeezed: list[str] = []
    for ch in expected:
        if ch.isspace():
            if not squeezed or squeezed[-1] != " ":
                squeezed.append(" ")
        else:
            squeezed.append(ch)
    assert got == "".join(squeezed)


@pytest.mark.parametrize("seed", range(600))
def test_slice_corresponds_to_original_slice(seed: int) -> None:
    """normalized[a:b] is the normalisation of original[map(a):map(b)] (at cluster edges)."""
    text = random_text(seed)
    result = normalize(text)
    rng = random.Random(seed)
    for _ in range(8):
        edges = boundaries(result)
        a, b = sorted(rng.choice(edges) for _ in range(2))
        piece = text[result.original_offset(a) : result.original_offset(b)]
        again = normalize(piece).text
        # Pieces may carry collapsed whitespace at their edges, so compare modulo that.
        assert again.strip(" ") == result.text[a:b].strip(" ")


def test_astral_characters_count_as_one_offset() -> None:
    text = "a\U0001f600\u00adb\U00020000 c"
    result = normalize(text)
    assert result.text == text.replace("\u00ad", "")
    assert result.to_original == (0, 1, 2, 4, 5, 6, 7)
    assert result.to_normalized == (0, 1, 2, 2, 3, 4, 5, 6)


def test_combining_sequences_compose_and_map_to_the_cluster() -> None:
    result = normalize("xe\u0301y")
    assert result.text == "x\u00e9y"
    assert result.to_original == (0, 1, 3, 4)
    assert result.to_normalized == (0, 1, 1, 2, 3)  # offset 2 sits inside the cluster


def test_canonical_reordering_of_marks() -> None:
    result = normalize("q\u0301\u0323")
    assert result.text == "q\u0323\u0301"
    assert result.to_original == (0, 0, 0, 3)


def test_removed_characters_attach_to_the_next_one() -> None:
    result = normalize("\ufeffa\u200bb\u200c\u200d")
    assert result.text == "ab"
    assert result.to_original == (0, 2, 6)
    assert result.to_normalized == (0, 0, 1, 1, 1, 1, 2)


def test_composition_across_a_removed_character() -> None:
    result = normalize("e\u00ad\u0301")
    assert result.text == "\u00e9"
    assert result.to_original == (0, 3)


def test_whitespace_runs_collapse_including_nbsp_tabs_and_newlines() -> None:
    result = normalize("a \u00a0\t\n\u200bb  c")
    assert result.text == "a b c"
    assert result.text[1] == " "
    assert result.original_offset(2) == 5
    assert result.normalized_offset(3) == 1  # inside the run: before the space
    assert normalize(" ").text == " "
    assert normalize("").text == ""


def test_expanding_character_has_no_interior_boundary() -> None:
    result = normalize("a\u0958b")
    assert result.text == "a\u0915\u093cb"
    assert result.to_original == (0, 1, 1, 2, 3)
    assert result.to_normalized == (0, 1, 3, 4)


def test_out_of_range_offsets_raise() -> None:
    result = normalize("abc")
    with pytest.raises(IndexError):
        result.original_offset(4)
    with pytest.raises(IndexError):
        result.normalized_offset(-1)


def test_text_normalized_operation_via_cli(capsys: pytest.CaptureFixture[str]) -> None:
    request = json.dumps({"text": "Ca\u00adfe\u0301  au\u00a0lait"})
    code = main(["text.normalized", "--json", request])
    env = json.loads(capsys.readouterr().out)
    assert code == 0
    assert env["result"]["text"] == "Caf\u00e9 au lait"
    mapping = env["result"]["offset_map"]
    assert len(mapping["to_original"]) == len("Caf\u00e9 au lait") + 1
    assert mapping["to_original"][-1] == len("Ca\u00adfe\u0301  au\u00a0lait")


def test_text_normalized_is_registered_read_only_with_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["help", "text.normalized"]) == 0
    env = json.loads(capsys.readouterr().out)
    described = env["result"]["operations"]["text.normalized"]
    assert described["kind"] == "read"
    assert described["schema"]["required"] == ["text"]
    assert main(["text.normalized", "--json", "{}"]) == 2
