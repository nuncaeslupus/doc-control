"""Normalised text: NFC, no soft hyphens or zero-width characters, collapsed whitespace."""

from __future__ import annotations

import json
import random
import time
import unicodedata

import pytest

from doc_control.cli import main
from doc_control.model.normalized import (
    IGNORABLE,
    NormalizedText,
    decode_runs,
    encode_runs,
    normalize,
    normalized_text,
)

SEEDS = range(3000)
ALPHABET = [
    "a", "e", "n", "Z", "1", ".", "\u2014", "\u00f1", "\u00e9", "\u00df",
    "\ud55c", "\u1100", "\u1161", "\u11a8",
    " ", " ", "\t", "\n", "\u00a0", "\u2003", "\u2028",
    "\u00ad", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff",
    "\u0301", "\u0323", "\u0302", "\u0308", "\u0344", "\u0958", "\u212b", "\u1e9b",
    "\U0001f600", "\U0001d11e", "\U0001f1ea", "\U00020000",
]  # fmt: skip


HANGUL_RUNS = [
    "\u1100\u1161\u11a8", "\u1100\u1161", "\u1112\u1175\u11c2", "\u1161\u11a8",
    "\uac00\u11a8", "\uac01", "\u1100\u1161\u11a8\u11a8", "\u1100\u200b\u1161\u00ad\u11a8",
]  # fmt: skip


def random_text(seed: int) -> str:
    rng = random.Random(seed)
    parts: list[str] = []
    for _ in range(rng.randrange(0, 14)):
        if rng.random() < 0.15:  # deliberate Hangul runs: L, L+V, L+V+T, V+T and precomposed + T
            parts.append(rng.choice(HANGUL_RUNS))
        else:
            parts.append(rng.choice(ALPHABET))
    return "".join(parts)


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


def test_maps_are_monotonic_and_consistent() -> None:
    for seed in range(0, 3000, 5):
        check_maps(random_text(seed))


def check_maps(text: str) -> None:
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


def test_text_matches_independent_normalisation() -> None:
    for seed in range(1500):
        text = random_text(seed)
        expected = unicodedata.normalize("NFC", "".join(c for c in text if c not in IGNORABLE))
        # Whitespace collapsing is the only difference from plain NFC.
        squeezed: list[str] = []
        for ch in expected:
            if ch.isspace():
                if not squeezed or squeezed[-1] != " ":
                    squeezed.append(" ")
            else:
                squeezed.append(ch)
        assert normalize(text).text == "".join(squeezed), ascii(text)


def test_hangul_syllables_compose_from_jamo() -> None:
    lvt = normalize("\u1100\u1161\u11a8")
    assert lvt.text == "\uac01" and lvt.to_original == (0, 3) and lvt.to_normalized == (0, 0, 0, 1)
    assert normalize("\u1100\u1161").text == "\uac00"
    assert normalize("\uac00\u11a8").text == "\uac01"
    assert normalize("x\u1112\u1175\u11c2y").text == "x\ud7a3y"
    assert normalize("\u1100\u200b\u1161\u00ad\u11a8").text == "\uac01"
    assert normalize("\u1100\u1161\u11a8\u11a8").text == unicodedata.normalize(
        "NFC", "\u1100\u1161\u11a8\u11a8"
    )


def test_slice_corresponds_to_original_slice() -> None:
    """normalized[a:b] is the normalisation of original[map(a):map(b)] (at cluster edges)."""
    for seed in range(600):
        text = random_text(seed)
        result = normalize(text)
        rng = random.Random(seed)
        for _ in range(8):
            edges = boundaries(result)
            a, b = sorted(rng.choice(edges) for _ in range(2))
            piece = text[result.original_offset(a) : result.original_offset(b)]
            again = normalize(piece).text
            # Pieces may carry collapsed whitespace at their edges, so compare modulo that.
            assert again.strip(" ") == result.text[a:b].strip(" "), ascii(text)


def test_runs_round_trip() -> None:
    for seed in range(600):
        result = normalize(random_text(seed))
        for values in (result.to_original, result.to_normalized):
            runs = encode_runs(values, 0, len(values))
            assert decode_runs(runs) == dict(enumerate(values))
            half = len(values) // 2
            assert decode_runs(encode_runs(values, half, len(values))) == {
                i: values[i] for i in range(half, len(values))
            }
    assert encode_runs(list(range(5000)), 0, 5000) == [[0, 0, 5000]]


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
    original = "Ca\u00adf\u0065\u0301  au\u00a0lait"
    code = main(["text.normalized", "--json", json.dumps({"text": original})])
    env = json.loads(capsys.readouterr().out)
    assert code == 0
    (entry,) = env["result"]["paragraphs"]
    assert entry["text"] == "Caf\u00e9 au lait"
    expected = normalize(original)
    assert decode_runs(entry["offset_map"]["to_original"]) == dict(enumerate(expected.to_original))
    assert decode_runs(entry["offset_map"]["to_normalized"]) == dict(
        enumerate(expected.to_normalized)
    )


def test_text_normalized_is_registered_read_only_with_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["help", "text.normalized"]) == 0
    env = json.loads(capsys.readouterr().out)
    described = env["result"]["operations"]["text.normalized"]
    assert described["kind"] == "read"
    assert set(described["schema"]["properties"]) >= {"doc", "text", "at"}
    assert main(["text.normalized", "--json", "{}"]) == 2


def test_runs_that_never_stabilise_under_nfc_stay_linear() -> None:
    """U+0F73 decomposes into marks that reorder across the whole run: NFC of it is quadratic."""
    started = time.perf_counter()
    result = normalize("\u0f73" * 20000)
    assert time.perf_counter() - started < 1.0
    assert len(result.to_original) == len(result.text) + 1
    assert result.to_original[-1] == 20000
    started = time.perf_counter()
    normalize("\u0f71\u0f72" * 20000)
    assert time.perf_counter() - started < 1.0


def test_normalized_text_equals_normalize_text() -> None:
    """The bulk shortcut gives exactly ``normalize(...).text`` on mixed, tricky input."""
    pieces = [
        "a",
        "é",
        "e\u0301",
        "\u00ad",
        "\u200b",
        " ",
        "\t",
        "\u00a0",
        "Å",
        "\u212b",
        "가",
        "가",
        "ñ",
        "n\u0303",
        "\ufeff",
        "\n",
        "x",
        "\u0301",
    ]
    rng = random.Random(7)
    for _ in range(3000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
        assert normalized_text(text) == normalize(text).text, repr(text)


def test_no_combining_marks_past_the_third_plane() -> None:
    """``normalized_text``'s mark class stops at U+30000; Unicode has none above it."""
    assert not any(unicodedata.combining(chr(cp)) for cp in range(0x30000, 0x110000))
