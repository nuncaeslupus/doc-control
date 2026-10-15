"""The `doc.batch` operation: all-or-nothing, dry run, conflicts, address map, stale addresses.

The gate ``edits_applied_to_wrong_paragraph == 0``: whenever a document changed between
the read that produced the addresses and the write, a batch is either refused or lands
exactly where intended; it never edits a paragraph that is not the one addressed.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import pytest

from doc_control.cli import main
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.index import DocumentIndex, build_index
from doc_control.ooxml.zipio import Package
from doc_control.ops.batch import batch
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import make_docx

TEXTS = [f"Párrafo número {i} con texto propio." for i in range(12)]


def read_index(path: Path) -> DocumentIndex:
    return build_index(Package(path).read("word/document.xml"))


def addresses(path: Path) -> list[str]:
    return [str(p.address) for p in read_index(path).paragraphs]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def texts(path: Path) -> list[str]:
    return [p.text for p in read_index(path).paragraphs]


def run(path: Path, ops: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    reply = batch({"doc": str(path), "ops": ops, **extra})
    assert isinstance(reply, Reply)
    return {"result": reply.result, "doc": reply.doc}


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    return make_docx(tmp_path / "d.docx", *TEXTS)


def test_batch_is_registered_and_only_the_direct_kinds_are_standalone() -> None:
    registry = load_operations()
    assert registry["doc.batch"].kind == "write"
    for kind in ("text.replace", "para.insert", "para.delete"):
        assert registry[kind].kind == "write"  # direct write tools (ops/direct.py)
    for kind in ("para.move", "style.apply", "format.apply", "comment.reply"):
        assert kind not in registry
    assert "text.replace" in registry["doc.batch"].schema["properties"]["ops"]["description"]


def test_batch_applies_all_operations_in_one_write(doc: Path) -> None:
    a = addresses(doc)
    out = run(
        doc,
        [
            {"op": "text.replace", "at": a[1], "text": "dos"},
            {"op": "para.insert", "after": a[3], "text": "nuevo"},
            {"op": "para.delete", "at": a[5]},
            {"op": "para.move", "at": a[7], "after": a[9]},
        ],
        author="Ana",
        tag="crit",
    )
    result = out["result"]
    assert result["applied"] == 4 and not result["dry_run"]
    assert result["author"] == "Ana (crit)"
    assert result["sha256"] == digest(doc) == out["doc"]["sha256"]
    assert result["address_map"]
    new = addresses(doc)
    for old, now in result["address_map"].items():
        assert old != now and now in new
    assert texts(doc)[1] == "dos"


def test_dry_run_previews_without_writing(doc: Path) -> None:
    a = addresses(doc)
    before = doc.read_bytes()
    ops = [
        {"op": "text.replace", "at": f"{a[2]}:0-7", "text": "Línea"},
        {"op": "para.delete", "at": a[4]},
    ]
    out = run(doc, ops, dry_run=True)
    assert doc.read_bytes() == before
    result = out["result"]
    assert result["dry_run"] and "backup" not in result
    assert result["preview"][0] == {
        "op": 0,
        "at": f"{a[2]}:0-7",
        "before": "Párrafo",
        "after": "Línea",
    }
    assert result["preview"][1]["after"] == "(deleted)"
    assert "p3" in result["footprint"]["word/document.xml"]
    assert out["doc"]["sha256"] == digest(doc)
    # the same request for real writes what the preview said
    real = run(doc, ops)
    assert real["result"]["address_map"] == result["address_map"]


def test_all_or_nothing_any_bad_operation_leaves_document_untouched(doc: Path) -> None:
    a = addresses(doc)
    before = doc.read_bytes()
    bad: list[tuple[dict[str, Any], ErrorCode]] = [
        ({"op": "text.replace", "at": "p3#00000000", "text": "x"}, ErrorCode.STALE_ADDRESS),
        ({"op": "text.replace", "at": "p99#00000000", "text": "x"}, ErrorCode.STALE_ADDRESS),
        ({"op": "text.replace", "at": "nonsense", "text": "x"}, ErrorCode.ADDRESS_INVALID),
        ({"op": "para.insert", "text": "x"}, ErrorCode.SCHEMA_INVALID),
        ({"op": "para.nope", "at": a[0]}, ErrorCode.SCHEMA_INVALID),
    ]
    for op, code in bad:
        with pytest.raises(DocControlError) as err:
            run(doc, [{"op": "text.replace", "at": a[0], "text": "ok"}, op])
        assert err.value.code is code
        assert doc.read_bytes() == before
    with pytest.raises(DocControlError) as err:
        run(doc, [{"op": "para.insert", "text": "x"}])
    assert err.value.details["op_index"] == 0


def test_overlapping_operations_report_both_indexes(doc: Path) -> None:
    a = addresses(doc)
    before = doc.read_bytes()
    cases = [
        [
            {"op": "text.replace", "at": f"{a[1]}:0-5", "text": "A"},
            {"op": "text.replace", "at": f"{a[1]}:3-9", "text": "B"},
        ],
        [
            {"op": "text.replace", "at": a[1], "text": "A"},
            {"op": "para.delete", "at": f"{a[0]}..{a[2]}"},
        ],
        [
            {"op": "para.insert", "after": a[4], "text": "A"},
            {"op": "para.insert", "after": a[4], "text": "B"},
        ],
    ]
    for ops in cases:
        with pytest.raises(DocControlError) as err:
            run(doc, [{"op": "text.replace", "at": a[8], "text": "free"}, *ops], dry_run=True)
        assert err.value.code is ErrorCode.BATCH_CONFLICT
        assert err.value.details["edits"] == [1, 2]
        assert doc.read_bytes() == before


def test_expect_sha256_must_match(doc: Path) -> None:
    a = addresses(doc)
    good = digest(doc)
    with pytest.raises(DocControlError) as err:
        run(doc, [{"op": "para.delete", "at": a[1]}], expect={"sha256": "0" * 64})
    assert err.value.code is ErrorCode.DOC_CHANGED_ON_DISK
    assert digest(doc) == good
    run(doc, [{"op": "para.delete", "at": a[1]}], expect={"sha256": good})
    assert digest(doc) != good


def test_untracked_live_is_refused(doc: Path) -> None:
    # plain writes (D-21) are file engine only for now; see tests/ops/test_plain_writes.py
    with pytest.raises(DocControlError) as err:
        run(doc, [{"op": "para.delete", "at": addresses(doc)[1]}], tracked=False, engine="live")
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert "live plain writes come later" in err.value.message


def test_cli_batch_envelope_and_error(doc: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = addresses(doc)
    req = {"doc": str(doc), "dry_run": True, "ops": [{"op": "para.delete", "at": a[2]}]}
    assert main(["doc.batch", "--json", json.dumps(req)]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] and env["op"] == "doc.batch" and env["result"]["applied"] == 1
    req["ops"] = [{"op": "para.delete", "at": "p3#deadbeef"}]
    assert main(["doc.batch", "--json", json.dumps(req)]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "STALE_ADDRESS"
    assert "doc.batch" in REGISTRY


def mutate(path: Path, rng: random.Random) -> None:
    """Change the document the way a writer or another tool might between read and write."""
    current = texts(path)
    kind = rng.choice(["insert", "delete", "edit", "swap", "append"])
    if kind == "insert":
        current.insert(rng.randrange(len(current) + 1), f"Intrusa {rng.random()}")
    elif kind == "delete":
        del current[rng.randrange(len(current) - 1)]
    elif kind == "edit":
        current[rng.randrange(len(current))] += " editado"
    elif kind == "swap":
        i, j = rng.sample(range(len(current)), 2)
        current[i], current[j] = current[j], current[i]
    else:
        current.append("Final añadido")
    make_docx(path, *current)


def test_stale_address_after_structural_change_is_rejected(tmp_path: Path) -> None:
    """Random changes between read and write: refused when stale, never applied elsewhere."""
    rng = random.Random(12)
    wrong = refused = applied = 0
    for round_ in range(300):
        path = make_docx(tmp_path / f"f{round_}.docx", *TEXTS)
        read = addresses(path)
        picks = sorted(rng.sample(range(len(TEXTS) - 1), rng.randint(1, 4)))
        ops = [
            {"op": "text.replace", "at": read[i], "text": f"EDITADO {k}"}
            for k, i in enumerate(picks)
        ]
        for _ in range(rng.choice([0, 1, 1, 2])):
            mutate(path, rng)
        before = path.read_bytes()
        now = {str(p.address) for p in read_index(path).paragraphs}
        fresh = all(read[i] in now for i in picks)  # every address still names its paragraph
        try:
            run(path, ops)
        except DocControlError as err:
            refused += 1
            if err.code is not ErrorCode.STALE_ADDRESS or fresh or path.read_bytes() != before:
                wrong += 1
            continue
        applied += 1
        after = texts(path)
        edited = [t for t in after if t.startswith("EDITADO")]
        # unique texts: the addressed paragraph's text must be gone, nothing else touched
        gone = all(TEXTS[i] not in after for i in picks)
        if not fresh or len(edited) != len(picks) or not gone:
            wrong += 1
    print(f"edits_applied_to_wrong_paragraph={wrong}")
    assert wrong == 0
    assert refused > 50 and applied > 50


def test_ops_relative_to_a_moved_or_deleted_paragraph_conflict(doc: Path) -> None:
    a = addresses(doc)
    before = doc.read_bytes()
    move = {"op": "para.move", "at": a[2], "after": a[4]}
    delete = {"op": "para.delete", "at": a[3]}
    insert_2 = {"op": "para.insert", "after": a[2], "text": "X"}
    insert_3 = {"op": "para.insert", "after": a[3], "text": "X"}
    cases: list[list[dict[str, Any]]] = [
        [move, insert_2],
        [{"op": "para.insert", "before": a[2], "text": "X"}, move],
        [move, {"op": "text.replace", "at": f"{a[2]}:0-3", "text": "X"}],
        [move, {"op": "para.move", "at": a[7], "after": a[2]}],
        [{"op": "para.move", "at": a[7], "before": a[2]}, move],
        [delete, insert_3],
        [insert_3, delete],
        [delete, {"op": "para.move", "at": a[7], "after": a[3]}],
    ]
    cross = {"op": "text.replace", "at": f"{a[2]}:5..{a[5]}:3", "text": "M"}
    for other in (
        {"op": "para.insert", "after": a[3], "text": "X"},
        {"op": "para.insert", "before": a[4], "text": "X"},
        {"op": "para.insert", "after": a[2], "text": "X"},
        {"op": "para.move", "at": a[8], "after": a[3]},
        {"op": "para.move", "at": a[8], "before": a[2]},
    ):
        cases += [[cross, other], [other, cross]]
    for ops in cases:
        with pytest.raises(DocControlError) as err:
            run(doc, ops, dry_run=True)
        assert err.value.code is ErrorCode.BATCH_CONFLICT, ops
        assert err.value.details["edits"] == [0, 1]
    assert doc.read_bytes() == before


def test_address_map_reports_the_new_place_of_a_moved_paragraph(doc: Path) -> None:
    a = addresses(doc)
    ops = [{"op": "para.move", "at": a[2], "after": a[5]}]
    new_address = run(doc, ops, dry_run=True)["result"]["address_map"][a[2]]
    run(doc, ops)
    moved = [p for p in read_index(doc).paragraphs if str(p.address) == new_address]
    assert len(moved) == 1 and moved[0].text == TEXTS[2]
    assert moved[0].number > 5


def test_stale_fuzz_duplicates_ranges_and_structure(tmp_path: Path) -> None:
    """Duplicate texts, offsets and structural ops: refused, or applied where addressed."""
    rng = random.Random(77)
    base = [f"Texto {i % 4} repetido {i % 3} aquí" for i in range(10)] + ["Último párrafo"]
    wrong = refused = applied = 0
    for round_ in range(300):
        path = make_docx(tmp_path / f"g{round_}.docx", *base)
        read = addresses(path)
        picks = sorted(rng.sample(range(len(base) - 1), rng.randint(1, 3)))
        ops: list[dict[str, Any]] = []
        expected: dict[int, str] = {}
        for i in picks:
            s = rng.randrange(0, 5)
            e = s + rng.randrange(1, 4)
            if rng.random() < 0.6:
                ops.append({"op": "text.replace", "at": f"{read[i]}:{s}-{e}", "text": "ZZ"})
                expected[i] = base[i][:s] + "ZZ" + base[i][e:]
            else:
                ops.append({"op": "para.insert", "after": read[i], "text": "NUEVO"})
        current = list(base)
        for _ in range(rng.choice([0, 1, 1, 2])):
            kind = rng.choice(["swap", "insert", "append"])
            if kind == "swap":  # duplicates swapping places keep some addresses valid
                i, j = rng.sample(range(len(current)), 2)
                current[i], current[j] = current[j], current[i]
            elif kind == "insert":
                current.insert(rng.randrange(len(current) + 1), "Intrusa")
            else:  # a change in another paragraph: numbering of the rest stays
                current[-1] += " más"
        make_docx(path, *current)
        before = path.read_bytes()
        now = set(addresses(path))
        fresh = all(read[i] in now for i in picks)
        try:
            run(path, ops)
        except DocControlError as err:
            refused += 1
            if err.code is not ErrorCode.STALE_ADDRESS or path.read_bytes() != before or fresh:
                wrong += 1
            continue
        applied += 1
        after = texts(path)
        inserted = [o["after"] for o in ops if o["op"] == "para.insert"]
        if not fresh or len(after) != len(current) + len(inserted):
            wrong += 1
            continue
        if after.count("NUEVO") != len(inserted):
            wrong += 1
        for i, text in expected.items():
            shift = sum(1 for at in inserted if read.index(at) < i)
            if after[i + shift] != text:
                wrong += 1
    print(f"edits_applied_to_wrong_paragraph={wrong}")
    assert wrong == 0
    assert refused > 30 and applied > 30


def test_stale_fuzz_with_moves_deletes_and_cross_paragraph_ranges(tmp_path: Path) -> None:
    """Random structural batches: applied only when fresh, STALE only when something is stale."""
    rng = random.Random(5)
    base = [f"Párrafo único {i} del libro" for i in range(14)]
    wrong = stale = applied = 0
    for round_ in range(300):
        path = make_docx(tmp_path / f"h{round_}.docx", *base)
        read = addresses(path)
        used: list[int] = []
        ops: list[dict[str, Any]] = []
        for _ in range(rng.randint(1, 4)):
            kind = rng.choice(["move", "delete", "cross", "insert"])
            i = rng.randrange(0, 11)
            j = rng.randrange(0, 12)
            if kind == "move":
                ops.append({"op": "para.move", "at": read[i], "after": read[j]})
                used += [i, j]
            elif kind == "delete":
                ops.append({"op": "para.delete", "at": f"{read[i]}..{read[i + 1]}"})
                used += [i, i + 1]
            elif kind == "cross":
                at = f"{read[i]}:3..{read[i + 1]}:4"
                ops.append({"op": "text.replace", "at": at, "text": "C"})
                used += [i, i + 1]
            else:
                ops.append({"op": "para.insert", "before": read[j], "text": "NUEVO"})
                used.append(j)
        current = list(base)
        for _ in range(rng.choice([0, 1, 2])):
            if rng.random() < 0.5:
                current.insert(rng.randrange(len(current) + 1), "Intrusa")
            else:
                del current[rng.randrange(len(current) - 1)]
        make_docx(path, *current)
        before = path.read_bytes()
        now = set(addresses(path))
        fresh = all(read[i] in now for i in used)
        try:
            run(path, ops)
        except DocControlError as err:
            if path.read_bytes() != before:
                wrong += 1
            if err.code is ErrorCode.STALE_ADDRESS:
                stale += 1
                wrong += fresh
            continue
        applied += 1
        wrong += not fresh
    print(f"edits_applied_to_wrong_paragraph={wrong}")
    assert wrong == 0
    assert stale > 30 and applied > 5
