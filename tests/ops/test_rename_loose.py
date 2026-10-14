"""`term.rename` with `match_level` (FIND T8): folded renames, each change tagged.

The gate ``loose_report_missing == 0`` counts preview changes at a folded level that do
not carry their ``match_level``, and replies whose ``matches_by_level`` does not add up to
``counts.matches``. ``regression_failures == 0`` counts rename replies without
``match_level`` that differ from the golden file taken before ``match_level`` existed. The
golden leaves out what depends on the document's bytes or the clock (``sha256``,
``backup``, ``date``). Regenerate it on purpose with
``uv run python -m tests.ops.test_rename_loose``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import make_docx

GOLDEN = Path(__file__).parent / "data" / "rename_golden.json"
PORTABLE_DROP = ("sha256", "backup", "date")

DIALOGUE = (
    "-¿Lucía? -dijo Marta-. Lucia no está.",
    "--LUCIA, ¿vienes? Lucías y lucía-hija.",
    "Lucía llegó; LUCÍA gritó, y lucía sonrió.",
    "Cuesta 5$ el kilo y 5 la docena.",
)

PLAIN = (
    "Lucía llegó. LUCÍA gritó, y lucía sonrió.",
    "La Lucíana no cambia, ni Lucías, ni Lucíao.",
    "Lucia sin tilde, LUCIA en voz alta.",
)

#: Golden cases: (document, request without ``doc``).
CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("plain", {"from": "Lucía", "to": "Elena", "dry_run": True}),
    ("plain", {"from": "Lucía", "to": "Elena"}),
    ("plain", {"from": "Lucía", "to": "Elena", "dry_run": True, "case_sensitive": True}),
    ("plain", {"from": "Lucía", "to": "Elena", "dry_run": True, "preserve_case": False}),
    ("plain", {"from": "Lucia", "to": "Elena", "dry_run": True, "ignore_accents": True}),
    ("plain", {"from": "Lucía", "to": "Elena", "dry_run": True, "whole_word": False}),
    (
        "plain",
        {"from": "Lucía", "to": "Elena", "dry_run": True, "inflections": ["s", {"from": "o"}]},
    ),
    ("plain", {"from": "Lucía", "to": "Elena", "dry_run": True, "preview_limit": 1}),
    ("dialogue", {"from": "Lucía", "to": "Elena", "dry_run": True}),
    ("dialogue", {"from": "lucia", "to": "elena", "dry_run": True, "ignore_accents": True}),
    ("dialogue", {"from": "5$", "to": "6$", "dry_run": True}),
)


@pytest.fixture(autouse=True)
def _ops() -> None:
    load_operations()


def run(doc: Path, old: str, new: str, **options: Any) -> dict[str, Any]:
    reply = REGISTRY["term.rename"].run({"doc": str(doc), "from": old, "to": new, **options})
    assert isinstance(reply, Reply)
    return {**reply.result, "warnings": reply.warnings}


def tags(result: dict[str, Any]) -> list[tuple[str, str]]:
    return [(c["before"], c["match_level"]) for c in result["preview"]]


def missing(result: dict[str, Any]) -> int:
    """Changes without their level, plus one when the levels do not add up."""
    untagged = sum("match_level" not in c for c in result["preview"])
    by_level = result.get("matches_by_level")
    adds_up = by_level is not None and sum(by_level.values()) == result["counts"]["matches"]
    return untagged + (not adds_up)


def test_rename_default_level_tags_every_change(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", *DIALOGUE)
    out = run(doc, "Lucía", "Elena", match_level="default", dry_run=True)
    assert tags(out) == [
        ("Lucía", "exact"),
        ("Lucia", "default"),
        ("LUCIA", "default"),
        ("lucía", "case"),
        ("Lucía", "exact"),
        ("LUCÍA", "case"),
        ("lucía", "case"),
    ]
    assert out["matches_by_level"] == {"exact": 2, "case": 3, "default": 2}
    assert out["counts"]["matches"] == 7 and out["counts"]["replaced"] == 7
    assert [c["after"] for c in out["preview"]][:3] == ["Elena", "Elena", "ELENA"]
    loose = run(doc, "5$", "6$", match_level="loose", dry_run=True)
    assert tags(loose) == [("5$", "exact"), ("5", "loose")]
    assert loose["matches_by_level"] == {"exact": 1, "loose": 1}
    exact = run(doc, "Lucía", "Elena", match_level="exact", dry_run=True)
    assert exact["matches_by_level"] == {"exact": 2, "case": 3}
    # loose_report_missing == 0
    assert sum(missing(r) for r in (out, loose, exact)) == 0


def test_rename_folded_level_writes_tracked_changes(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", *DIALOGUE)
    out = run(doc, "Lucía", "Elena", match_level="default")
    assert out["counts"]["replaced"] == 7 and "sha256" in out
    again = run(doc, "Lucía", "Elena", match_level="default", dry_run=True)
    assert again["counts"]["matches"] == 0 and again["matches_by_level"] == {}


def test_rename_folded_skips_count_toward_levels(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", "Lucía y lucía.")
    out = run(doc, "lucía", "lucía", match_level="default", dry_run=True)
    assert out["counts"] == {**out["counts"], "matches": 2, "unchanged": 2, "replaced": 0}
    assert out["matches_by_level"] == {"exact": 1, "case": 1}


@pytest.mark.parametrize(
    ("level", "options", "path"),
    [
        ("default", {"case_sensitive": True}, "$.case_sensitive"),
        ("loose", {"case_sensitive": True}, "$.case_sensitive"),
        ("default", {"ignore_accents": False}, "$.ignore_accents"),
    ],
)
def test_rename_contradicting_flags_refused(
    tmp_path: Path, level: str, options: dict[str, Any], path: str
) -> None:
    doc = make_docx(tmp_path / "d.docx", *DIALOGUE)
    with pytest.raises(DocControlError) as err:
        run(doc, "Lucía", "Elena", match_level=level, dry_run=True, **options)
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert err.value.details["path"] == path


def test_rename_phrase_empty_after_folding_refused(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "d.docx", *DIALOGUE)
    with pytest.raises(DocControlError) as err:
        run(doc, "--", "-", match_level="default", dry_run=True)
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def _portable(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k not in PORTABLE_DROP}


def snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for n, (kind, request) in enumerate(CASES):
            paragraphs = PLAIN if kind == "plain" else DIALOGUE
            doc = make_docx(Path(tmp) / f"{n}.docx", *paragraphs)
            options = dict(request)
            old, new = options.pop("from"), options.pop("to")
            key = f"{n}:{kind}:{json.dumps(request, ensure_ascii=False)}"
            out[key] = _portable(run(doc, old, new, **options))
    return out


def test_rename_without_match_level_unchanged() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    current = snapshot()
    failures = [case for case in golden if current.get(case) != golden[case]]
    assert sorted(golden) == sorted(current)
    assert failures == []  # regression_failures == 0
    assert all("matches_by_level" not in r for r in current.values())


if __name__ == "__main__":
    load_operations()
    GOLDEN.write_text(
        json.dumps(snapshot(), ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sys.exit(0)
