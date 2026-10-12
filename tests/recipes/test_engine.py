"""The declarative recipe engine, the recipe store, `recipe.list` and `recipe.run`."""

from __future__ import annotations

import copy
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Literal

import pytest

from doc_control.cli import main
from doc_control.envelope import Reply
from doc_control.errors import DocControlError, ErrorCode
from doc_control.model.document import Document
from doc_control.ops.batch import ALL_SCHEMAS
from doc_control.recipes import store
from doc_control.recipes.schema import PROPOSABLE, validate_recipe
from doc_control.registry import REGISTRY, load_operations
from tests.helpers import make_docx

CENTRING: dict[str, Any] = {
    "recipe": "fake-centring",
    "version": 1,
    "description": "Leading spaces that fake a centred line",
    "match": {"find": {"regex": "^ {8,}(\\S.*)$", "scope": "paragraph"}},
    "propose": [
        {"op": "text.replace", "at": "$match", "text": "$1"},
        {"op": "format.apply", "at": "$para", "align": "center"},
    ],
    "repeat_until_stable": True,
    "max_passes": 4,
}

# Collapses a run of three spaces by one: needs several passes, since it only shortens it.
SHRINK: dict[str, Any] = {
    "recipe": "shrink-spaces",
    "version": 1,
    "match": {"find": {"literal": "   "}},
    "propose": [{"op": "text.replace", "at": "$match", "text": "  "}],
    "repeat_until_stable": True,
    "max_passes": 10,
}


@pytest.fixture(autouse=True)
def _ops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    load_operations()
    monkeypatch.setenv("DOC_CONTROL_HOME", str(tmp_path / "home"))


def run(doc: Path, **request: Any) -> Reply:
    reply = REGISTRY["recipe.run"].run({"doc": str(doc), **request})
    assert isinstance(reply, Reply)
    return reply


def dry(doc: Path, ops: list[dict[str, Any]]) -> None:
    assert isinstance(REGISTRY["batch"].run({"doc": str(doc), "ops": ops, "dry_run": True}), Reply)


def recipe_with(**changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(CENTRING)
    out.update(changes)
    return out


def test_user_recipe_with_unknown_key_is_rejected(tmp_path: Path) -> None:
    """Gate: `user_recipe_capabilities_outside_batch` counts the recipes that get through."""
    doc = make_docx(tmp_path / "a.docx", "        Centred?")
    smuggled: list[dict[str, Any]] = []
    for key, value in [("exec", "os.system('x')"), ("path", "/etc/passwd"), ("url", "http://x")]:
        smuggled.append(recipe_with(**{key: value}))
    for section in ("match", "find"):
        bad = copy.deepcopy(CENTRING)
        target = bad["match"] if section == "match" else bad["match"]["find"]
        target["python"] = "__import__('os')"
        smuggled.append(bad)
    for i in range(len(CENTRING["propose"])):
        bad = copy.deepcopy(CENTRING)
        bad["propose"][i]["file"] = "x.txt"
        smuggled.append(bad)
    bad = copy.deepcopy(CENTRING)
    bad["propose"].append({"op": "shell", "at": "$para", "cmd": "ls"})  # not a batch op
    smuggled.append(bad)
    bad = copy.deepcopy(CENTRING)
    bad["propose"][0]["at"] = "p1#00000000"  # a fixed address is not a substitution
    smuggled.append(bad)

    through = 0
    for definition in smuggled:
        with pytest.raises(DocControlError) as caught:
            run(doc, definition=definition)
        assert caught.value.code is ErrorCode.RECIPE_INVALID
        try:
            validate_recipe(definition)
            through += 1
        except DocControlError:
            pass
        # the same recipe in the user store is listed as invalid, never offered
        (tmp_path / "home" / "recipes").mkdir(parents=True, exist_ok=True)
        (tmp_path / "home" / "recipes" / "fake-centring.json").write_text(json.dumps(definition))
        listing = REGISTRY["recipe.list"].run({})
        assert isinstance(listing, dict)
        assert [r for r in listing["recipes"] if r["source"] == "user"] == []
        assert len(listing["invalid"]) == 1
    assert set(PROPOSABLE) <= set(ALL_SCHEMAS)  # nothing but batch operation kinds
    assert through == 0  # user_recipe_capabilities_outside_batch == 0


def test_recipe_value_mistakes_are_rejected() -> None:
    cases = [
        recipe_with(recipe="Bad Name"),
        recipe_with(version=2),
        recipe_with(max_passes=11),
        recipe_with(repeat_until_stable=False),  # max_passes without repeat
        recipe_with(match={"find": {"regex": "a", "literal": "a"}}),
        recipe_with(match={"find": {"regex": "(a+)+"}}),
        recipe_with(match={"find": {"regex": "("}}),
        recipe_with(propose=[]),
        recipe_with(propose=[{"op": "text.replace", "at": "$match", "text": "$2"}]),
        recipe_with(propose=[{"op": "format.apply", "at": "$match", "align": "center"}]),
        recipe_with(propose=[{"op": "format.apply", "at": "$para"}]),
        recipe_with(propose=[{"op": "para.delete", "at": "$match"}]),
    ]
    for bad in cases:
        with pytest.raises(DocControlError) as caught:
            validate_recipe(bad)
        assert caught.value.code is ErrorCode.RECIPE_INVALID, bad
    validate_recipe(CENTRING)


def test_run_proposes_a_valid_batch_without_writing(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "        Chapter one", "Plain line", "          Two")
    before = doc.read_bytes()
    reply = run(doc, definition=CENTRING, dry_run=True)
    assert doc.read_bytes() == before
    result = reply.result
    assert result["applied"] is False and result["stable"] is True and result["matches"] == 2
    batch = result["batch"]
    assert batch["expect"]["sha256"] == Document(str(doc)).sha256
    kinds = [(o["op"], o["at"].split("#")[0]) for o in batch["ops"]]
    assert kinds == [
        ("text.replace", "p1"),
        ("text.replace", "p3"),
        ("format.apply", "p1"),
        ("format.apply", "p3"),
    ]
    # the edit is narrowed to what changes: the eight leading spaces go, the text stays put
    assert batch["ops"][0]["text"] == "" and batch["ops"][0]["at"].endswith(":0-8")

    preview = REGISTRY["batch"].run({**batch, "dry_run": True})
    assert isinstance(preview, Reply) and doc.read_bytes() == before
    assert preview.result["applied"] == 4
    applied = REGISTRY["batch"].run(batch)
    assert isinstance(applied, Reply) and doc.read_bytes() != before
    assert [p.text for p in Document(str(doc)).index.paragraphs][:2] == [
        "Chapter one",
        "Plain line",
    ]
    # the document is now clean: running again proposes nothing
    assert run(doc, definition=CENTRING).result["batch"]["ops"] == []


def test_repeat_until_stable_terminates_and_merges_passes(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "a" + " " * 9 + "b", "no spaces")
    result = run(doc, definition=SHRINK).result
    # 9 spaces shrink one per match and pass: stability, not the count, matters
    assert result["stable"] is True and 1 < result["passes"] <= 10
    ops = result["batch"]["ops"]
    assert len(ops) == 1 and ops[0]["op"] == "text.replace"  # one merged edit per paragraph
    new = REGISTRY["batch"].run(result["batch"])
    assert isinstance(new, Reply)
    text = Document(str(doc)).index.paragraphs[0].text
    assert "   " not in text and text.startswith("a ") and text.endswith(" b")


def test_repeat_is_capped_by_max_passes(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "xx")
    growing = {
        "recipe": "grow",
        "version": 1,
        "match": {"find": {"literal": "x"}},
        "propose": [{"op": "text.replace", "at": "$match", "text": "xx"}],
        "repeat_until_stable": True,
        "max_passes": 3,
    }
    reply = run(doc, definition=growing)
    assert reply.result["passes"] == 3 and reply.result["stable"] is False
    assert any(w.startswith("NOT_STABLE") for w in reply.warnings)
    assert isinstance(REGISTRY["batch"].run({**reply.result["batch"], "dry_run": True}), Reply)


def test_single_pass_recipe_keeps_one_edit_per_match(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "one -- two -- three")
    recipe = {
        "recipe": "dashes",
        "version": 1,
        "match": {"find": {"literal": "--"}},
        "propose": [{"op": "text.replace", "at": "$match", "text": "—"}],
    }
    ops = run(doc, definition=recipe).result["batch"]["ops"]
    assert [o["at"].split(":")[1] for o in ops] == ["4-6", "11-13"]
    assert isinstance(REGISTRY["batch"].run({"doc": str(doc), "ops": ops}), Reply)


def test_substitutions_and_other_operations(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "Price: $5 today", "Delete me", "keep")
    recipes: list[dict[str, Any]] = [
        {
            "recipe": "dollars",
            "version": 1,
            "match": {"find": {"regex": r"\$(\d)"}},
            "propose": [
                {"op": "text.replace", "at": "$match", "text": "$$$1 ($match in $para)"},
            ],
        },
        {
            "recipe": "note",
            "version": 1,
            "match": {"find": {"regex": r"\$(\d)"}},
            "propose": [{"op": "comment.add", "at": "$match", "text": "check $1"}],
        },
        {
            "recipe": "drop",
            "version": 1,
            "match": {"find": {"literal": "delete", "ignore_case": True}},
            "propose": [{"op": "para.delete", "at": "$para"}],
        },
    ]
    first = run(doc, definition=recipes[0]).result["batch"]["ops"]
    assert first[0]["text"] == " ($5 in Price: $5 today)"  # "$5" itself is not rewritten
    assert first[0]["at"].endswith(":9-9")
    dry(doc, first)
    note = run(doc, definition=recipes[1]).result["batch"]["ops"]
    assert note == [{"op": "comment.add", "at": note[0]["at"], "text": "check 5"}]
    dry(doc, note)
    second = run(doc, definition=recipes[2]).result["batch"]["ops"]
    assert [o["op"] for o in second] == ["para.delete"]
    dry(doc, second)


def test_user_store_lists_and_runs_by_name(tmp_path: Path) -> None:
    home = tmp_path / "home" / "recipes"
    home.mkdir(parents=True)
    (home / "fake-centring.json").write_text(json.dumps(CENTRING))
    (home / "wrong-name.json").write_text(json.dumps(SHRINK))  # file name must match the recipe
    (home / "broken.json").write_text("{not json")
    (home / "dup.json").write_text('{"recipe": "dup", "recipe": "dup"}')
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    shadow = {**CENTRING, "description": "bundled"}
    (bundled / "fake-centring.json").write_text(json.dumps(shadow))

    cat = store.catalogue(bundled_dir=bundled)
    assert {n: s for n, (s, _) in cat.recipes.items()} == {"fake-centring": "bundled"}
    assert sorted(b["file"] for b in cat.invalid) == [
        "broken.json",
        "dup.json",
        "fake-centring.json",
        "wrong-name.json",
    ]

    (home / "fake-centring.json").unlink()
    listing = REGISTRY["recipe.list"].run({})
    assert isinstance(listing, dict)
    assert [r for r in listing["recipes"] if r["source"] == "user"] == []
    (home / "fake-centring.json").write_text(json.dumps(CENTRING))
    listing = REGISTRY["recipe.list"].run({})
    assert isinstance(listing, dict)
    assert [(r["name"], r["source"]) for r in listing["recipes"] if r["source"] == "user"] == [
        ("fake-centring", "user")
    ]
    doc = make_docx(tmp_path / "a.docx", "        Centred")
    assert run(doc, recipe="fake-centring").result["matches"] == 1
    with pytest.raises(DocControlError) as caught:
        run(doc, recipe="nope")
    assert caught.value.code is ErrorCode.RECIPE_INVALID
    with pytest.raises(DocControlError):
        run(doc)  # neither recipe nor definition


def test_cli_recipe_run_envelope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = make_docx(tmp_path / "a.docx", "        Centred")
    code = main(["recipe.run", "--json", json.dumps({"doc": str(doc), "definition": CENTRING})])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True and out["result"]["batch"]["ops"]
    assert re.fullmatch(r"[0-9a-f]{64}", out["doc"]["sha256"])


def rx(pattern: str, *steps: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "recipe": "probe",
        "version": 1,
        "match": {"find": {"regex": pattern}},
        "propose": list(steps) or [{"op": "text.replace", "at": "$match", "text": "x"}],
        **extra,
    }


@pytest.mark.parametrize("pattern", ["(a|a)*b", "(a|aa)*b", "(a|b|ab)*c", r"(a+)\1+b", "(.*a){25}"])
def test_catastrophic_regex_is_cut_off_with_a_partial_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str
) -> None:
    from doc_control.recipes import engine

    monkeypatch.setattr(engine, "TIME_BUDGET_SECONDS", 1.0)
    doc = make_docx(tmp_path / "a.docx", "a" * 34 + "x", "ab" * 40 + "x", "fine -- line")
    try:
        reply = run(doc, definition=rx(pattern))
    except DocControlError as err:  # the static screen may stop it first: also a clean outcome
        assert err.code is ErrorCode.RECIPE_INVALID
        return
    if pattern != r"(a+)\1+b":  # that one is merely slow; the rest must be cut off
        assert any(w.startswith("TIME_BUDGET") for w in reply.warnings)
        assert reply.result["stable"] is False
    if reply.result["batch"]["ops"]:
        dry(doc, reply.result["batch"]["ops"])


def test_catastrophic_regex_keeps_matches_found_before_the_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from doc_control.recipes import engine

    monkeypatch.setattr(engine, "TIME_BUDGET_SECONDS", 1.0)
    doc = make_docx(tmp_path / "a.docx", "ab", "a" * 34 + "x")
    ops = run(doc, definition=rx("(a|a)*b")).result["batch"]["ops"]
    assert [o["at"].split("#")[0] for o in ops] == ["p1"]


@pytest.mark.parametrize("form", ["NFC", "NFD"])
def test_groups_match_in_both_unicode_forms(tmp_path: Path, form: Literal["NFC", "NFD"]) -> None:
    import unicodedata

    text = unicodedata.normalize(form, "caféx y")
    doc = make_docx(tmp_path / "a.docx", text)
    step = {"op": "text.replace", "at": "$match", "text": "<$1>"}
    ops = run(doc, definition=rx("(é)x", step)).result["batch"]["ops"]
    assert len(ops) == 1
    assert unicodedata.normalize("NFC", ops[0]["text"]) == "<é>"
    assert isinstance(REGISTRY["batch"].run({"doc": str(doc), "ops": ops}), Reply)
    para = Document(str(doc)).index.paragraphs[0].text
    assert unicodedata.normalize("NFC", para) == "caf<é> y"


def test_groups_follow_a_folded_match_without_dropping_it(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "caf\u00e9x")
    recipe = {
        "recipe": "probe",
        "version": 1,
        "match": {"find": {"regex": "(cafe)x", "ignore_accents": True}},
        "propose": [{"op": "text.replace", "at": "$match", "text": "[$1]"}],
    }
    ops = run(doc, definition=recipe).result["batch"]["ops"]
    assert [o["text"] for o in ops] == ["[caf\u00e9]"]  # groups read from the original text


def test_repeat_proposes_other_operations_once_from_the_original_text(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "aabb")
    recipe = rx(
        "(a)b",
        {"op": "text.replace", "at": "$match", "text": "$1"},
        {"op": "comment.add", "at": "$para", "text": "was: $para"},
        repeat_until_stable=True,
    )
    result = run(doc, definition=recipe).result
    comments = [o for o in result["batch"]["ops"] if o["op"] == "comment.add"]
    assert len(comments) == 1 and comments[0]["text"] == "was: aabb"
    assert result["stable"] is True and result["passes"] > 1


def test_definition_size_is_capped(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "x")
    big = {**SHRINK, "description": "d" * (store.MAX_RECIPE_BYTES + 1)}
    with pytest.raises(DocControlError) as caught:
        run(doc, definition=big)
    assert caught.value.code is ErrorCode.RECIPE_INVALID


def test_user_store_does_not_follow_symlinks(tmp_path: Path) -> None:
    home = tmp_path / "home" / "recipes"
    home.mkdir(parents=True)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({**SHRINK, "recipe": "linked"}))
    try:
        (home / "linked.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks not available")
    cat = store.catalogue(bundled_dir=tmp_path / "none")
    assert cat.recipes == {} and [b["file"] for b in cat.invalid] == ["linked.json"]


def test_proposal_on_a_paragraph_with_a_field_and_hidden_run(tmp_path: Path) -> None:
    from tests.helpers import CONTENT_TYPES, RELS

    body = (
        '<w:p><w:r><w:t xml:space="preserve">pre -- </w:t></w:r>'
        '<w:fldSimple w:instr=" PAGE "><w:r><w:t>5</w:t></w:r></w:fldSimple>'
        "<w:r><w:rPr><w:vanish/></w:rPr><w:t>hid</w:t></w:r>"
        '<w:r><w:t xml:space="preserve"> post -- end</w:t></w:r></w:p>'
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    doc = tmp_path / "f.docx"
    with zipfile.ZipFile(doc, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", xml)
    recipe = {
        "recipe": "dashes",
        "version": 1,
        "match": {"find": {"literal": "--"}},
        "propose": [{"op": "text.replace", "at": "$match", "text": "—"}],
    }
    ops = run(doc, definition=recipe).result["batch"]["ops"]
    assert len(ops) == 2
    try:
        reply = REGISTRY["batch"].run({"doc": str(doc), "ops": ops})
    except DocControlError as err:  # refused cleanly is acceptable
        assert err.code is ErrorCode.UNSUPPORTED_CONTENT
    else:
        assert isinstance(reply, Reply)


def test_edits_are_narrowed_to_the_characters_that_change(tmp_path: Path) -> None:
    doc = make_docx(tmp_path / "a.docx", "see: a-b and c-d.")
    one = rx("(a)-(b)", {"op": "text.replace", "at": "$match", "text": "$1+$2"})
    ops = run(doc, definition=one).result["batch"]["ops"]
    assert [(o["at"].split(":")[1], o["text"]) for o in ops] == [("6-7", "+")]


def test_repeat_proposes_one_small_edit_per_change(tmp_path: Path) -> None:
    line = "one -two- three -four- five -six- end"
    doc = make_docx(tmp_path / "a.docx", line)
    hyphens = rx(
        "^(.*?)-",
        {"op": "text.replace", "at": "$match", "text": "$1~"},
        repeat_until_stable=True,
        max_passes=10,
    )
    ops = run(doc, definition=hyphens).result["batch"]["ops"]
    assert [o["text"] for o in ops] == ["~"] * 6
    spans = [tuple(map(int, o["at"].split(":")[1].split("-"))) for o in ops]
    assert all(end - start == 1 for start, end in spans)
