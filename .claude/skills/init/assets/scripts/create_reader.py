#!/usr/bin/env python3
"""create_reader.py — generate an annotatable HTML + Markdown reader from a spec or plan.

Auto-discovers spec source(s):
  1. --input FILE      single specification file
  2. --input-dir DIR   scans DIR/*/spec.md (workspace mode)
  3. auto (no flags): looks for arsenal/project/*/spec.md,
                      then falls back to status/specification.md

Outputs are named `<repo>-<doc>-v<N>-<date>` so a downloaded reader says what it
is: <repo> is the git toplevel's directory name, <doc> is `spec` / `plan` for the
canonical documents and the input stem for any other (so documents sharing a
directory keep separate readers), N is the document's `**Revision**:` header line
(1 without one), and the date is today's. Written to --output-dir (default: the
single input's directory, or docs/spec-reader/ in workspace mode):
  <name>-reader.html    self-contained HTML reader, one note field after each section
                        that has body text (none on a bare heading); notes auto-save
                        in the browser and the Export button saves a Markdown file
  <name>-annotated.md   same document as Markdown with the same note slots
--output FILE names the HTML explicitly instead (the Markdown goes beside it).
Earlier generated readers of the same document in that directory are removed, and
so is an earlier annotated file whose slots are all still empty.

The Export button names its download the same way, with the export's date:
`<repo>-<doc>-v<N>-<date>-notes.md`.
A fenced ```drawspec block (a drawspec JSON document) is validated and rendered to
inline SVG in the HTML; the annotated Markdown keeps the JSON source. The command is
$ARSENAL_DRAWSPEC when set, else `drawspec` on PATH, else `uvx --from
git+https://github.com/nuncaeslupus/drawspec drawspec`. A diagram that fails
validation, or no way to run drawspec, fails the run — nothing is written. A document
with no drawspec fence never runs it.
The HTML carries a digest of every source it rendered, which is how
`reader_check.py` tells a current reader from a stale one.
A returned export belongs in {output-dir} beside the reader — it is part of the
project, not a scratch file. Seed a rebuilt reader from one with
`--notes <that file>`; it is Markdown with the note data embedded in a trailing
comment, and a plain JSON object is accepted too ({output-dir}/notes.json is
still read when --notes is not given). Notes are keyed by stable section IDs and are
re-injected into both output artifacts.

Requires: pip install markdown   (or: uv run --with markdown python3 create_reader.py)

Usage (run from repo root):
    uv run --with markdown python3 claude-arsenal/scripts/create_reader.py
    uv run --with markdown python3 claude-arsenal/scripts/create_reader.py \\
        --input status/specification.md --output-dir status
    uv run --with markdown python3 claude-arsenal/scripts/create_reader.py \\
        --input-dir arsenal/project --output-dir docs/spec-reader
    uv run --with markdown python3 claude-arsenal/scripts/create_reader.py \\
        --input status/plan.md --output-dir status
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

try:
    import markdown
except ImportError:
    print(
        "✗ 'markdown' package not found. Install it: pip install markdown\n"
        "  or run: uv run --with markdown python3 create_reader.py",
        file=sys.stderr,
    )
    sys.exit(2)

_md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", "attr_list"])

HEADING_RE = re.compile(r"^(#{2,3})\s+(.*)$")
NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+(.*)$")
HR_RE = re.compile(r"^-{3,}\s*$")


# ---------------------------------------------------------------- Markdown parsing


def normalize_list_indent(text: str) -> str:
    """Promote 1-3 space list indents to 4 spaces for python-markdown (HTML path only)."""
    out = []
    in_code_block = False
    for ln in text.split("\n"):
        if ln.strip().startswith("```"):
            in_code_block = not in_code_block
        if not in_code_block:
            m = re.match(r"^( {1,3})([-*+]|\d+[.)])(\s)", ln)
            if m:
                ln = "    " + ln[len(m.group(1)) :]
        out.append(ln)
    return "\n".join(out)


# ---------------------------------------------------------------- drawspec diagrams

# A spec or plan draws its pictures as drawspec documents — JSON that says what the
# diagram means, laid out and rendered by the tool — rather than as hand-written SVG
# or ASCII art. The HTML reader shows the render; the Markdown keeps the source.
DRAWSPEC_ENV = "ARSENAL_DRAWSPEC"
DRAWSPEC_REPO = "git+https://github.com/nuncaeslupus/drawspec"
DRAWSPEC_OPEN_RE = re.compile(r"^(?P<ind>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*drawspec[ \t]*$")
_drawspec_cmd: list[str] | None = None


class DrawspecError(Exception):
    """A diagram that cannot be drawn: the run fails and writes nothing."""


def drawspec_command() -> list[str]:
    """$ARSENAL_DRAWSPEC, else `drawspec` on PATH, else uvx from the git repo."""
    global _drawspec_cmd
    if _drawspec_cmd is not None:
        return _drawspec_cmd
    override = os.environ.get(DRAWSPEC_ENV, "").strip()
    if override:
        cmd = shlex.split(override)
        if not shutil.which(cmd[0]):
            raise DrawspecError(f"${DRAWSPEC_ENV} is set to {override!r}, which is not runnable")
    elif shutil.which("drawspec"):
        cmd = ["drawspec"]
    elif shutil.which("uvx"):
        cmd = ["uvx", "--quiet", "--from", DRAWSPEC_REPO, "drawspec"]
    else:
        raise DrawspecError(
            "this document has ```drawspec diagrams, and drawspec cannot be run here. "
            f"Install uv (uvx runs it from {DRAWSPEC_REPO}), put `drawspec` on PATH, "
            f"or set ${DRAWSPEC_ENV} to the command"
        )
    _drawspec_cmd = cmd
    return cmd


def run_drawspec(action: str, source: str, where: str) -> str:
    """Run `drawspec <action> -` on source; stdout on success, DrawspecError otherwise."""
    cmd = drawspec_command()
    try:
        proc = subprocess.run(
            [*cmd, action, "-"],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DrawspecError(f"{where}: `{' '.join(cmd)} {action}` did not run — {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip() or f"exit {proc.returncode}"
        raise DrawspecError(f"{where}: drawspec {action} refused it —\n{detail}")
    return proc.stdout


def extract_drawspec(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Swap each ```drawspec fence for a placeholder line; return (text, [(token, json)])."""
    lines = text.split("\n")
    out: list[str] = []
    blocks: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        m = DRAWSPEC_OPEN_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        fence = m.group("fence")
        j = i + 1
        while j < len(lines):
            close = lines[j].strip()
            if close and set(close) == {fence[0]} and len(close) >= len(fence):
                break
            j += 1
        token = f"DRAWSPEC-FIGURE-{len(blocks)}-END"
        body = [
            ln[len(m.group("ind")) :] if ln.startswith(m.group("ind")) else ln
            for ln in lines[i + 1 : j]
        ]
        blocks.append((token, "\n".join(body)))
        out += ["", m.group("ind") + token, ""]
        i = j + 1
    return "\n".join(out), blocks


def render_md(text: str, where: str = "") -> str:
    text, diagrams = extract_drawspec(text)
    _md.reset()
    html = _md.convert(normalize_list_indent(text))
    for n, (token, source) in enumerate(diagrams, 1):
        label = f"{where}, drawspec block {n}" if where else f"drawspec block {n}"
        run_drawspec("validate", source, label)
        svg = run_drawspec("render", source, label).strip()
        if not svg.startswith("<svg"):
            raise DrawspecError(f"{label}: drawspec render produced no SVG")
        figure = f'<figure class="drawspec">{svg}</figure>'
        html = html.replace(f"<p>{token}</p>", figure).replace(token, figure)
    return html


def render_inline(text: str) -> str:
    _md.reset()
    out = _md.convert(text).strip()
    if out.startswith("<p>") and out.endswith("</p>"):
        out = out[3:-4]
    return out


def strip_inline_md(text: str) -> str:
    text = re.sub(r"`(.*?)`", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"_(.*?)_", r"\1", text)
    return text.strip()


def slug(text: str) -> str:
    """A filename-safe stem. Never empty, and never the same for two titles.

    It names the export file and the reader's storage namespace, so a title
    that is entirely non-ASCII or punctuation ("项目") stripped down to nothing
    produced `-spec-notes-….md`. Falling back to a fixed `doc` fixed the empty
    name and gave every such title the SAME name: two readers then shared an
    export filename and a localStorage namespace, so one project's notes came
    up under another's headings. The digest keeps the fallback stable across
    runs — the namespace has to survive regeneration — while telling two of
    them apart.
    """
    text_slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if text_slug:
        return text_slug
    return "doc-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]  # a name, not a signature


def parse_doc(raw: str) -> tuple[str, str, list[dict]]:
    """Return (h1_title, intro_md, [sections])."""
    lines = raw.split("\n")
    h1 = ""
    i = 0
    h1_found = False
    in_code_block = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
        if not in_code_block:
            m = re.match(r"^#\s+(.*)$", line)
            if m:
                h1 = m.group(1).strip()
                i += 1
                h1_found = True
                break
        i += 1
    if not h1_found:
        i = 0
    intro_lines: list[str] = []
    in_code_block = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
        if not in_code_block and HEADING_RE.match(line):
            break
        intro_lines.append(line)
        i += 1
    sections: list[dict] = []
    cur: dict | None = None
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
        m = HEADING_RE.match(line) if not in_code_block else None
        if m:
            if cur:
                sections.append(cur)
            cur = {"level": len(m.group(1)), "heading": m.group(2).strip(), "body": []}
        else:
            if cur is not None:
                cur["body"].append(line)
        i += 1
    if cur:
        sections.append(cur)

    def clean(block_lines: list[str]) -> str:
        kept = [ln for ln in block_lines if not HR_RE.match(ln)]
        while kept and kept[0].strip() == "":
            kept.pop(0)
        while kept and kept[-1].strip() == "":
            kept.pop()
        return "\n".join(kept)

    intro_md = clean(intro_lines)
    for s in sections:
        s["body_md"] = clean(s["body"])
    return h1, intro_md, sections


def build_part(file_md: str, code: str, part_label: str, title: str, source: str = "") -> dict:
    """Parse one document into a structured part with stable ids/labels."""
    where = source or title
    h1, intro_md, sections = parse_doc(file_md)
    items: list[dict] = []
    used: set[str] = set()

    def mk(domid_base: str) -> str:
        d = "s-" + slug(domid_base)
        n = d
        k = 1
        while n in used:
            k += 1
            n = f"{d}-{k}"
        used.add(n)
        return n

    if intro_md.strip():
        domid = mk(f"{code}-intro")
        items.append(
            {
                "domid": domid,
                "key": f"{code} · intro",
                "chip": "overview",
                "title_html": "Preamble &amp; scope",
                "label": "Preamble & scope",
                "level": 2,
                "body_html": render_md(intro_md, f"{where} § preamble"),
                "toc": "Preamble & scope",
                "raw_body": intro_md,
            }
        )
    for s in sections:
        heading = s["heading"]
        m = NUM_RE.match(heading)
        if m:
            num, rest = m.group(1), m.group(2)
            chip = f"§{num}"
            key = f"{code} §{num}"
            title_html = render_inline(rest)
            label = f"§{num} {strip_inline_md(rest)}"
            toc = f"§{num} {strip_inline_md(rest)}"
            domid = mk(f"{code}-{num}")
        else:
            chip = "▸"
            title_html = render_inline(heading)
            plain = strip_inline_md(heading)
            key = f"{code} › {plain}"
            label = plain
            toc = plain
            domid = mk(f"{code}-{slug(plain)[:32]}")
        items.append(
            {
                "domid": domid,
                "key": key,
                "chip": chip,
                "title_html": title_html,
                "label": label,
                "level": s["level"],
                "body_html": render_md(s["body_md"], f"{where} § {strip_inline_md(heading)}"),
                "toc": toc,
                "raw_body": s["body_md"],
            }
        )
    return {
        "code": code,
        "part_label": part_label,
        "title": title,
        "h1": h1,
        "items": items,
    }


# ---------------------------------------------------------------- Source discovery

# The input stem decides which document this is, the same way it already decides the
# title. A plan and a spec want different badges and different output filenames, and
# nothing else about the reader changes between them.
DOC_KINDS = {"plan": ("PLAN", "Plan", "plan")}
# The header line `specify` / `design` write, read only above the first section.
REVISION_RE = re.compile(r"^\*\*Revision\*\*:\s*r?(\d+)\b", re.MULTILINE)


def doc_revision(raw: str) -> int | None:
    """The document's `**Revision**: N`, or None when its header has none."""
    header = re.split(r"^## ", raw, maxsplit=1, flags=re.MULTILINE)[0]
    m = REVISION_RE.search(header)
    return int(m.group(1)) if m else None


def source_digest(raw: str) -> str:
    """What `reader_check.py` compares against: sha256 of the text as read."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


DEFAULT_DOC_KIND = ("SPEC", "Specification", "spec")
# Stems whose reader is `<kind>-reader.html`; any other stem gets `<stem>-reader.html`.
CANONICAL_STEMS = {"spec", "specification", "plan"}


def doc_kind(stem: str) -> tuple[str, str, str]:
    """Return (badge code, part label, output basename) for a document stem."""
    return DOC_KINDS.get(stem.lower().replace("_", "-"), DEFAULT_DOC_KIND)


def doc_version(parts: list[tuple[str, dict]]) -> int:
    """The `**Revision**` of a single document, else 1.

    A workspace reader renders several documents, so it has no one revision.
    """
    revision = parts[0][1].get("revision") if len(parts) == 1 else None
    return revision or 1


def repo_name(cwd: Path) -> str:
    """The repository's name for filenames: the git toplevel's directory, else cwd's."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        top = ""
    return Path(top).name if top else cwd.resolve().name


def output_stem(repo: str, doc: str, version: int, gen_date: str) -> str:
    """`<repo>-<doc>-v<N>-<date>`: what a reader is, readable from a Downloads folder."""
    return f"{slug(repo)}-{slug(doc)}-v{version}-{gen_date}"


GENERATED_SUFFIXES = ("-reader.html", "-annotated.md")
NOTE_SLOT_RE = re.compile(r"^> \*\*✎ Notes\*\*", re.MULTILINE)
EMPTY_SLOT = "> _(your notes here — replace this line)_"


def superseded(out_dir: Path, prefix: str, legacy: str, keep: set[Path]) -> list[Path]:
    """Earlier readers of the same document in out_dir: other versions and dates, and
    the undated `<legacy>-reader.html` / `-annotated.md` names used before."""
    dated = re.compile(
        rf"^{re.escape(prefix)}-v\d+-\d{{4}}-\d{{2}}-\d{{2}}-(?:reader\.html|annotated\.md)$"
    )
    found = {f for f in out_dir.glob(f"{prefix}-v*") if dated.match(f.name)}
    found |= {
        out_dir / f"{legacy}{s}" for s in GENERATED_SUFFIXES if (out_dir / f"{legacy}{s}").is_file()
    }
    kept = {k.resolve() for k in keep}
    return sorted(f for f in found if f.is_file() and f.resolve() not in kept)


def remove_superseded(paths: list[Path]) -> None:
    """Delete earlier generated readers — but never an annotated file someone typed into.

    The HTML is pure output (its notes live in the browser, keyed by a namespace
    that does not change with the filename). An annotated Markdown file is where a
    reviewer may have written notes by hand, so it goes only while every slot is
    still the placeholder.
    """
    for f in paths:
        if f.suffix == ".md":
            text = f.read_text(encoding="utf-8", errors="replace")
            slots = len(NOTE_SLOT_RE.findall(text))
            if not slots or text.count(EMPTY_SLOT) != slots:
                print(
                    f"ℹ  kept {f}: notes were typed into it — move them, then delete it",
                    file=sys.stderr,
                )
                continue
        f.unlink()
        print(f"ℹ  removed superseded {f}", file=sys.stderr)


def collect_parts_single(spec_path: Path) -> list[tuple[str, dict]]:
    """Single-file mode: one part from the document at spec_path."""
    code, label, basename = doc_kind(spec_path.stem)
    raw = spec_path.read_text(encoding="utf-8")
    part = build_part(raw, code, label, spec_path.stem.replace("-", " ").title(), str(spec_path))
    part["source_sha256"] = source_digest(raw)
    part["revision"] = doc_revision(raw)
    return [(basename, part)]


def collect_parts_workspace(workspace_dir: Path) -> list[tuple[str, dict]]:
    """Workspace mode: scan workspace_dir/*/spec.md alphabetically."""
    parts = []
    for spec_file in sorted(workspace_dir.glob("*/spec.md")):
        ws_name = spec_file.parent.name
        code = ws_name.upper()[:8]
        raw = spec_file.read_text(encoding="utf-8")
        part = build_part(raw, code, "Workspace", ws_name.replace("-", " ").title(), str(spec_file))
        part["source_sha256"] = source_digest(raw)
        parts.append(("workspace", part))
    return parts


def auto_discover(cwd: Path) -> tuple[str, list[tuple[str, dict]]]:
    """Detect mode and collect parts; returns (mode, parts)."""
    # arsenal/ first: that is where specs live now, in the host's own tree. The
    # old location is still checked so a repo that has not run the migration yet
    # gets its spec read rather than a "no spec found" that looks like an empty
    # project.
    for ws_dir in (cwd / "arsenal" / "project", cwd / "claude-arsenal" / "project"):
        if ws_dir.is_dir() and any(ws_dir.glob("*/spec.md")):
            return "workspace", collect_parts_workspace(ws_dir)
    single = cwd / "status" / "specification.md"
    if single.is_file():
        return "single", collect_parts_single(single)
    print(
        "✗ No spec found. Run from the repo root with a status/specification.md or "
        "arsenal/project/*/spec.md present, or pass --input / --input-dir.",
        file=sys.stderr,
    )
    sys.exit(1)


def infer_title(cwd: Path) -> str:
    """Infer project title from git remote name or directory name."""
    try:
        import subprocess

        remote = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=cwd, stderr=subprocess.DEVNULL, text=True
        ).strip()
        match = re.search(r"([^/:]+?)(?:\.[gG][iI][tT])?/?$", remote)
        name = match.group(1) if match else ""
        if name:
            return name.replace("-", " ").replace("_", " ").title()
    except Exception:
        pass
    return cwd.name.replace("-", " ").replace("_", " ").title()


# The Export button writes Markdown for a human to read, with the note data
# embedded verbatim in a trailing HTML comment so that the same file can be read
# back. Seeding used to demand a `notes.json` nothing produces — a reviewer who
# followed the instructions with the file the page had just handed them got a
# JSON decode error.
# Anchored to the start of a line, because that is what separates the block the
# export writes from a QUOTATION of it. Both places a marker can be quoted put it
# mid-line — a note is rendered as `> …` prose, and a note that mentions the
# marker also carries it inside a JSON string, which cannot hold a raw newline.
MARKER_RE = re.compile(r"^[ \t]*<!--[ \t]*SPEC-NOTES-DATA\s*", re.MULTILINE)


def read_notes(path: Path) -> dict:
    """Notes from an exported reader file: raw JSON, or the block the export embeds."""
    text = path.read_text(encoding="utf-8")
    try:
        loaded = json.loads(text)
    except ValueError:
        # The LAST marker, not the first: the block is trailing, and the notes
        # above it are the reviewer's own prose. Searching forward found a note
        # that quoted the marker, decoded nothing there, and rejected the whole
        # export — so the reader was regenerated without the notes it was handed
        # (#239).
        matches = list(MARKER_RE.finditer(text))
        if not matches:
            raise ValueError(
                "no notes found — expected a JSON object, or a Markdown export "
                "carrying its SPEC-NOTES-DATA block"
            ) from None
        # Where the object ENDS is the decoder's answer, not a regex's. Matching
        # `{.*?}` up to a `-->` ended the block at the first `}` a note happened
        # to contain, and truncated JSON reads as unparseable — so a reviewer
        # whose note quoted a `-->` got their notes dropped, silently enough
        # that the regenerated file overwrote them.
        try:
            loaded, _ = json.JSONDecoder().raw_decode(text, matches[-1].end())
        except ValueError as exc:
            raise ValueError(f"the SPEC-NOTES-DATA block is not valid JSON — {exc}") from None
    if not isinstance(loaded, dict):
        raise ValueError("the notes are not a JSON object")
    # Every note is text, and `build_markdown` calls `.strip()` on it. A file
    # spelling one as a number or an object raised an AttributeError out of the
    # build; it is bad input, and it gets the message every other bad input gets.
    bad = [k for k, v in loaded.items() if not isinstance(v, str)]
    if bad:
        raise ValueError(f"note values must be strings; not for: {', '.join(sorted(bad)[:5])}")
    return loaded


# ---------------------------------------------------------------- HTML build


def has_note_field(it: dict, seed_notes: dict) -> bool:
    """One note field per section with something to comment on — never on a bare heading.

    Splitting on ## and ### gives each option its own slot, but a heading
    followed straight by a subheading has no body of its own: a field there sat
    under a title with nothing to annotate, and read as a field placed at
    random. A seeded note keeps its field, so a note written against an earlier
    revision is never dropped by a rebuild.
    """
    return bool(it["body_html"].strip()) or bool(seed_notes.get(it["domid"], "").strip())


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_html(
    parts: list[tuple[str, dict]],
    title: str,
    gen_date: str,
    seed_notes: dict | None = None,
    single_label: str = "Specification",
    doc_slug: str = "spec",
    ns_key: str | None = None,
    export_stem: str | None = None,
) -> str:
    """The self-contained HTML reader.

    `export_stem` is `<repo>-<doc>-v<N>`, the part of the reader's own filename
    before the date; the Export button appends the export's date and `-notes.md`.
    """
    seed_notes = seed_notes or {}
    total_notes = sum(has_note_field(it, seed_notes) for _, p in parts for it in p["items"])
    # `ns_key` is the output stem when it is not the kind: two design docs in
    # one folder are two readers, and a namespace keyed on the kind alone let
    # one's notes surface under the other's headings.
    ls_ns = slug(title) + f"-{ns_key or doc_slug}-v1:"

    toc = ['<details class="toc" open><summary>Contents</summary>']
    for _kind, p in parts:
        head = esc(p["title"])
        if p["part_label"] != single_label:
            head = f"{esc(p['part_label'])} — {head}"
        toc.append('<details class="toc-part">')
        toc.append(f"<summary>{head}</summary><ul>")
        for it in p["items"]:
            toc.append(f'<li><a href="#{it["domid"]}">{esc(it["toc"])}</a></li>')
        toc.append("</ul></details>")
    toc.append("</details>")
    toc_html = "\n".join(toc)

    body = []
    for kind, p in parts:
        part_head = esc(p["title"])
        if p["part_label"] != single_label:
            part_head = f"{esc(p['part_label'])} — {part_head}"
        badge_cls = kind
        badge_label = p["part_label"]
        body.append('<section class="part">')
        body.append(
            f'<h2 class="part-title"><span class="badge {badge_cls}">{esc(badge_label)}</span>'
            f"{part_head}</h2>"
        )
        for it in p["items"]:
            tag = "h3" if it["level"] == 2 else "h4"
            noted = has_note_field(it, seed_notes)
            cls = f"sec l{it['level']}" + ("" if noted else " bare")
            body.append(f'<article class="{cls}" id="{it["domid"]}">')
            body.append(
                f'<{tag} class="sec-h"><span class="chip">{esc(it["chip"])}</span>'
                f'<span class="sec-title">{it["title_html"]}</span></{tag}>'
            )
            # A heading with no body of its own gets neither a body wrapper nor
            # a note field (see has_note_field): the field goes after the
            # content it is about, once per section.
            if it["body_html"]:
                body.append(f'<div class="sec-body">{it["body_html"]}</div>')
            if noted:
                body.append(
                    f'<div class="note" data-for="{it["domid"]}">'
                    f'<label class="note-head" for="n-{it["domid"]}">'
                    f'<span class="note-ico" aria-hidden="true">✎</span>'
                    f'<span class="note-label">Your note</span>'
                    f'<span class="note-ref">{esc(it["key"])}</span></label>'
                    f'<textarea class="note-ta" data-key="{esc(it["domid"])}" id="n-{it["domid"]}" '
                    f'data-label="{esc(it["label"])}" data-part="{part_head}" '
                    f'rows="1" placeholder="Add a note on this section…"></textarea>'
                    f"</div>"
                )
            body.append("</article>")
        body.append("</section>")
    body_html = "\n".join(body)

    page = HTML_TEMPLATE
    page = page.replace("__TITLE__", esc(title))
    page = page.replace(
        "__SOURCE_SHA__", " ".join(p["source_sha256"] for _, p in parts if "source_sha256" in p)
    )
    page = page.replace("__GEN_DATE__", gen_date)
    page = page.replace("__TOTAL__", str(total_notes))
    page = page.replace("__TOC__", toc_html)
    page = page.replace("__BODY__", body_html)
    page = page.replace("__CSS__", CSS)
    page = page.replace("__DOC_LABEL__", esc(single_label))
    # The export lands in the reviewer's Downloads beside everything else they saved
    # that week, so its name says what it annotates the way the reader's does:
    # `yourproject-spec-v3-2026-10-12-notes.md`, not a bare `spec-notes-…`.
    if export_stem is None:
        export_stem = f"{slug(title)}-{doc_slug}-v{doc_version(parts)}"
    page = page.replace(
        "__JS__",
        JS.replace("__LS_NS__", ls_ns)
        .replace("__EXPORT_STEM__", export_stem)
        .replace("__DOC_LABEL__", single_label),
    )
    page = page.replace(
        "__SEED_NOTES__",
        json.dumps(seed_notes, ensure_ascii=False).replace("<", "\\u003c"),
    )
    return page


# ---------------------------------------------------------------- Markdown build


def build_markdown(
    parts: list[tuple[str, dict]],
    title: str,
    gen_date: str,
    seed_notes: dict | None = None,
    single_label: str = "Specification",
) -> str:
    """Render the annotated Markdown edition: the document, with a note slot per section.

    The counterpart to the HTML reader, for a reviewer who would rather annotate
    in an editor than a browser. A heading with no body of its own gets no slot,
    the same rule the HTML follows. `seed_notes` re-seeds slots from a previous
    export, keyed by DOM id, so a rebuilt reader does not discard the notes
    someone already wrote against an earlier revision.
    """
    seed_notes = seed_notes or {}
    out = [f"# {title} — {single_label} (annotated edition)", ""]
    out.append(
        f"> Generated {gen_date}. This is the document with a **note slot** after every "
        "section that has text of its own. Read it in any Markdown app. To annotate, replace the `_(your notes…)_` "
        "placeholder under any section. When done, send the file back — notes are acted on."
    )
    out += ["", "---", ""]
    for _, p in parts:
        title_line = (
            f"# {p['part_label']} — {p['title']}"
            if p["part_label"] != single_label
            else f"# {p['title']}"
        )
        out.append(title_line)
        out.append("")
        for it in p["items"]:
            hashes = "##" if it["level"] == 2 else "###"
            out.append(f"{hashes} {it['label']}")
            out.append("")
            if not has_note_field(it, seed_notes):
                continue
            out.append(it["raw_body"])
            out.append("")
            # A section whose body ends in a blockquote would sit one blank line
            # above the note blockquote, which markdownlint reads as one quote
            # broken in half (MD028) rather than two. An HTML comment separates
            # them for the linter and renders as nothing.
            out.append("<!-- -->")
            out.append("")
            out.append(f"> **✎ Notes** · `{it['key']}`")
            note_text = seed_notes.get(it["domid"], "").strip()
            if note_text:
                for line in note_text.split("\n"):
                    out.append(f"> {line}")
            else:
                out.append("> _(your notes here — replace this line)_")
            out.append("")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- CSS / JS / HTML template

CSS = r"""
:root{
  --bg:#fbfaf7;--fg:#1d1c1a;--muted:#5f5c56;--line:#e3ded4;
  --card:#ffffff;--accent:#0f766e;--accent2:#9a4a0b;--chip:#e8efed;
  --note:#fffaf0;--note-line:#d9a441;--note-fg:#7a3b06;--code:#f1eee8;--link:#0f766e;
  --measure:70ch;
}
@media(prefers-color-scheme:dark){
  :root{
    --bg:#16151a;--fg:#ebe9e4;--muted:#aaa69f;--line:#34313a;
    --card:#1e1d23;--accent:#5eead4;--accent2:#fbbf24;--chip:#24302f;
    --note:#221d12;--note-line:#8a6a22;--note-fg:#fcd34d;--code:#26242b;--link:#7ff0dc;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--bg);color:var(--fg);
  font:17px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  padding-bottom:96px;
}
.wrap{max-width:calc(var(--measure) + 120px);margin:0 auto;padding:0 16px}
a{color:var(--link);text-underline-offset:2px}
code{background:var(--code);padding:.08em .35em;border-radius:5px;font-size:.86em;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-word}
pre{background:var(--code);padding:12px;border-radius:8px;overflow:auto;font-size:.88em;line-height:1.5}
pre code{background:none;padding:0;font-size:1em}
.topbar{position:sticky;top:0;z-index:30;background:var(--bg);
  border-bottom:1px solid var(--line);backdrop-filter:saturate(120%) blur(6px)}
.topbar .wrap{display:flex;align-items:center;gap:8px;padding-top:10px;padding-bottom:10px}
.brand{font-weight:700;font-size:16px;margin-right:auto;letter-spacing:.2px;min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.brand small{font-weight:500;color:var(--muted);display:block;font-size:11px;letter-spacing:0}
.btn{appearance:none;border:1px solid var(--line);background:var(--card);color:var(--fg);
  border-radius:9px;padding:8px 11px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn:active{transform:translateY(1px)}
.btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
@media(prefers-color-scheme:dark){.btn.primary{color:#06201d}}
.count{font-size:12px;color:var(--muted);white-space:nowrap;font-weight:600}
.savestate{font-size:12px;font-weight:700;white-space:nowrap;transition:color .2s}
.savestate.ok{color:var(--accent)}
.savestate.warn{color:#9a4a0b}
@media(prefers-color-scheme:dark){.savestate.warn{color:#fbbf24}}
.statusbar{border-bottom:1px solid var(--line);background:var(--card)}
.statusbar .wrap{padding:7px 16px;font-size:12px;color:var(--muted)}
#warnbar{display:none;background:#fff4e5;border-bottom:1px solid var(--note-line);color:#7c2d12}
#warnbar .wrap{padding:10px 16px;font-size:13px;line-height:1.45}
@media(prefers-color-scheme:dark){#warnbar{background:#3a2a0e;color:#fde68a;border-color:#6b531c}}
.hero{padding:26px 0 6px}
.hero h1{font-size:1.75rem;line-height:1.2;margin:.1em 0 .3em;letter-spacing:-.01em}
.hero p{color:var(--muted);margin:.3em 0;max-width:var(--measure)}
.help{border:1px solid var(--line);background:var(--card);border-radius:12px;padding:4px 14px;margin:14px 0}
.help summary{cursor:pointer;font-weight:600;padding:10px 0}
.help ul{margin:.2em 0 1em;padding-left:1.2em}
.help li{margin:.3em 0}
.toc{border:1px solid var(--line);background:var(--card);border-radius:12px;padding:4px 14px;margin:14px 0}
.toc>summary{cursor:pointer;font-weight:700;padding:11px 0;font-size:15px}
.toc-part{margin:2px 0;border-top:1px solid var(--line)}
.toc-part>summary{cursor:pointer;padding:9px 2px;font-weight:600;font-size:14px}
.toc-part ul{margin:.1em 0 .7em;padding-left:1.1em}
.toc-part li{margin:.22em 0;font-size:13.5px;line-height:1.4}
.toc-part a{text-decoration:none}
.toc-part a:hover{text-decoration:underline}
.part{margin:30px 0}
.part-title{font-size:1.45rem;line-height:1.25;border-bottom:2px solid var(--accent);
  padding-bottom:8px;margin:18px 0 10px}
.badge{display:inline-block;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;
  vertical-align:middle;padding:3px 7px;border-radius:6px;margin-right:8px}
.badge.spec,.badge.plan{background:var(--chip);color:var(--accent)}
.badge.workspace{background:var(--chip);color:var(--accent2)}
/* Two heading levels, told apart by size, weight and a rule: a ## section opens
   with a full-width rule; a ### section sits indented under it on a side rule. */
.sec{scroll-margin-top:72px}
.sec.l2{padding:14px 0 4px;border-top:1px solid var(--line);margin-top:10px}
.part>.sec.l2:first-of-type{border-top:none;margin-top:0}
.sec.l3{margin:6px 0 6px 2px;padding:2px 0 2px 14px;border-left:3px solid var(--line)}
.sec-h{margin:6px 0 8px;line-height:1.3}
h3.sec-h{font-size:1.3rem;font-weight:750}
h4.sec-h{font-size:1.08rem;font-weight:650;color:var(--fg)}
.sec.bare>.sec-h{margin-bottom:2px}
.chip{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:12px;font-weight:700;
  color:var(--accent);background:var(--chip);border-radius:6px;padding:2px 7px;margin-right:8px;vertical-align:middle}
h4 .chip{font-size:11px;color:var(--muted)}
.sec-title{vertical-align:middle}
.sec-body{margin:.2em 0 .4em}
.sec-body p,.sec-body li,.sec-body blockquote{max-width:var(--measure)}
.sec-body p{margin:.6em 0}
.sec-body ul,.sec-body ol{margin:.5em 0;padding-left:1.35em}
.sec-body li{margin:.3em 0}
.sec-body h4,.sec-body h5,.sec-body h6{font-size:1em;margin:1em 0 .3em}
.sec-body blockquote{margin:.7em 0;padding:.5em .9em;border-left:3px solid var(--accent);
  background:var(--code);border-radius:0 8px 8px 0;color:var(--fg)}
.sec-body blockquote p{margin:.3em 0}
.sec-body figure.drawspec{margin:.8em 0;overflow-x:auto}
.sec-body figure.drawspec svg{display:block;max-width:100%;height:auto;margin:0 auto}
.sec-body table{border-collapse:collapse;font-size:13.5px;line-height:1.45;margin:.6em 0;
  display:block;max-width:100%;overflow-x:auto}
.sec-body th,.sec-body td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
.sec-body th{background:var(--chip);font-weight:700}
.sec-body strong{font-weight:700}
/* The note field: the one thing on the page that is the reviewer's, so it must
   not read as more document. Dashed while empty, solid once written in. */
.note{max-width:var(--measure);background:var(--note);border:1.5px dashed var(--note-line);
  border-left:4px solid var(--note-line);border-radius:10px;padding:8px 12px;margin:12px 0 18px}
.note:focus-within{border-style:solid;box-shadow:0 0 0 3px color-mix(in srgb,var(--note-line) 30%,transparent)}
.note-head{display:flex;align-items:center;gap:8px;margin-bottom:2px;cursor:text}
.note-ico{color:var(--note-fg);font-weight:800}
.note-label{font-size:11.5px;font-weight:800;color:var(--note-fg);text-transform:uppercase;letter-spacing:.06em}
.note-ref{margin-left:auto;font-family:ui-monospace,Menlo,monospace;font-size:10.5px;color:var(--muted);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.note-ta{width:100%;border:none;background:transparent;color:var(--fg);font:inherit;font-size:16px;
  resize:none;outline:none;line-height:1.5;padding:2px 0;overflow:hidden;min-height:1.6em}
.note-ta::placeholder{color:var(--muted);opacity:1}
.note.filled{border-style:solid}
.fab{position:fixed;right:14px;bottom:16px;z-index:40;display:flex;flex-direction:column;gap:8px;align-items:flex-end}
.fab .btn{box-shadow:0 4px 14px #0003}
.modal{position:fixed;inset:0;z-index:60;background:#0008;display:none;align-items:center;justify-content:center;padding:16px}
.modal.open{display:flex}
.modal-card{background:var(--card);border:1px solid var(--line);border-radius:14px;max-width:680px;width:100%;
  max-height:86vh;display:flex;flex-direction:column;padding:16px}
.modal-card h3{margin:.1em 0 .5em}
.modal-card p{color:var(--muted);font-size:13.5px;margin:.2em 0 .6em}
.modal-card textarea{width:100%;flex:1;min-height:220px;border:1px solid var(--line);border-radius:8px;
  background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,Menlo,monospace;padding:10px;resize:vertical}
.modal-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.hidden-input{display:none}
.toast{position:fixed;left:50%;transform:translateX(-50%);bottom:84px;z-index:70;background:#111;color:#fff;
  padding:9px 14px;border-radius:9px;font-size:13px;opacity:0;transition:opacity .25s;pointer-events:none}
.toast.show{opacity:.96}
/* Phone width: smaller type, no floating column over the text, the hint dropped. */
@media(max-width:600px){
  body{font-size:16px;padding-bottom:76px}
  .hero h1{font-size:1.45rem}
  .part-title{font-size:1.25rem}
  h3.sec-h{font-size:1.15rem}
  h4.sec-h{font-size:1rem}
  .sec.l3{padding-left:10px}
  .statusbar .hint{display:none}
  .fab{left:0;right:0;bottom:0;flex-direction:row;justify-content:flex-end;gap:6px;padding:8px 10px;
    flex-wrap:wrap;background:var(--bg);border-top:1px solid var(--line)}
  .fab .btn{box-shadow:none;padding:7px 9px}
  .toast{bottom:64px}
}
@media print{.topbar,.statusbar,.fab,.help,.note:not(.filled){display:none}}
"""

JS = r"""
(function(){
  var NS = '__LS_NS__';
  var tas = Array.prototype.slice.call(document.querySelectorAll('.note-ta'));

  function autoGrow(t){t.style.height='auto';t.style.height=(t.scrollHeight)+'px';}
  function setFilled(t){var box=t.closest('.note');if(box){box.classList.toggle('filled',t.value.trim()!=='');}}
  function updateCount(){
    var n=0;tas.forEach(function(t){if(t.value.trim()!=='')n++;});
    var el=document.getElementById('count');if(el){el.textContent=n+' / '+tas.length+' notes';}
    return n;
  }
  var lsOK=true,dirty=false;
  try{localStorage.setItem('__vs__','1');localStorage.removeItem('__vs__');}catch(e){lsOK=false;}
  var WARN='⚠ Not saving — tap Save / Export';
  function nowStamp(){var d=new Date();return pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());}
  function setState(txt,cls){var el=document.getElementById('savestate');if(el){el.textContent=txt;el.className='savestate '+(cls||'');}}
  window.addEventListener('beforeunload',function(e){if(!lsOK&&dirty){e.preventDefault();e.returnValue='';}});

  var saveTimers={};
  function save(t){
    if(!lsOK){dirty=true;setState(WARN,'warn');return;}
    var k=NS+t.dataset.key;
    try{
      // An empty note is stored as "", not removed: a missing key means "never
      // touched" and the loader falls back to the seed, so removing it brought a
      // seeded note back after the reviewer had deliberately cleared it (#477).
      localStorage.setItem(k,t.value.trim()===''?'':t.value);
      dirty=true;setState('✓ Saved '+nowStamp(),'ok');
    }catch(e){dirty=true;lsOK=false;document.getElementById('warnbar').style.display='block';setState(WARN,'warn');}
  }
  var SEED=(typeof SPEC_SEED_NOTES!=='undefined')?SPEC_SEED_NOTES:{};
  tas.forEach(function(t){
    // Seed first, then let storage override it. With the read inside the same
    // try as the seed, a throwing localStorage skipped the seed too and the
    // reader came up empty exactly when it could save nothing (#477).
    if(Object.prototype.hasOwnProperty.call(SEED,t.dataset.key)){
      var sv=SEED[t.dataset.key];if(typeof sv==='string'){t.value=sv;}}
    try{var v=localStorage.getItem(NS+t.dataset.key);if(v!=null){t.value=v;}}catch(e){}
    autoGrow(t);setFilled(t);
    t.addEventListener('input',function(){
      autoGrow(t);setFilled(t);updateCount();
      clearTimeout(saveTimers[t.dataset.key]);
      saveTimers[t.dataset.key]=setTimeout(function(){save(t);},350);
    });
    t.addEventListener('blur',function(){save(t);});
  });
  updateCount();
  if(lsOK){setState('✓ Auto-saving','ok');}else{document.getElementById('warnbar').style.display='block';setState(WARN,'warn');}

  function toast(msg){var el=document.getElementById('toast');el.textContent=msg;el.classList.add('show');setTimeout(function(){el.classList.remove('show');},1900);}
  function pad(n){return n<10?'0'+n:''+n;}
  function today(){var d=new Date();return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());}
  // Named like the reader, <repo>-<doc>-v<N>-<date>-notes.md, the date being the export's.
  function exportName(){return '__EXPORT_STEM__-'+today()+'-notes.md';}

  function buildExport(){
    var byPart={},order=[],data={},n=0;
    tas.forEach(function(t){
      var v=t.value.trim();if(v==='')return;n++;
      data[t.dataset.key]=t.value;
      var part=t.dataset.part||'Other';
      if(!byPart[part]){byPart[part]=[];order.push(part);}
      byPart[part].push({label:t.dataset.label,ref:keyFromDom(t.dataset.key,t),note:t.value});
    });
    var lines=['# __DOC_LABEL__ notes','','_Exported '+today()+' · '+n+' note'+(n===1?'':'s')+'._','',
      '> Send this file back to continue the review. Notes are embedded as data at the bottom','> so importing this file restores them in the reader.',''];
    order.forEach(function(part){
      lines.push('## '+part,'');
      byPart[part].forEach(function(it){
        lines.push('**'+it.label+'**  `'+it.ref+'`');
        it.note.split('\n').forEach(function(ln){lines.push('> '+ln);});
        lines.push('');
      });
    });
    lines.push('<!-- SPEC-NOTES-DATA');
    lines.push(JSON.stringify(data));
    lines.push('-->');
    return{text:lines.join('\n'),n:n};
  }
  function keyFromDom(domkey,t){var box=t.closest('.note');var r=box?box.querySelector('.note-ref'):null;return r?r.textContent:domkey;}

  function download(name,text){
    try{var blob=new Blob([text],{type:'text/markdown'});var url=URL.createObjectURL(blob);
        var a=document.createElement('a');a.href=url;a.download=name;
        document.body.appendChild(a);a.click();
        setTimeout(function(){URL.revokeObjectURL(url);a.remove();},1500);return true;}catch(e){return false;}
  }

  var modal=document.getElementById('modal');
  var modalTa=document.getElementById('modal-ta');
  function openModal(text){modalTa.value=text;modal.classList.add('open');}
  function closeModal(){modal.classList.remove('open');}

  document.getElementById('btn-export').addEventListener('click',function(){
    var r=buildExport();
    if(r.n===0){toast('No notes yet — add some first.');return;}
    var saved=download(exportName(),r.text);openModal(r.text);
    // download() returning only means the download was REQUESTED; a browser can
    // still block or drop it. So `dirty` — what arms the unload warning — is
    // cleared only on a confirmed copy: the clipboard promise fulfilling.
    var cp=null;
    try{if(navigator.clipboard&&navigator.clipboard.writeText){cp=navigator.clipboard.writeText(r.text);}}catch(e){cp=null;}
    if(cp&&typeof cp.then==='function'){cp.then(function(){dirty=false;setState('✓ Copied to clipboard '+nowStamp(),'ok');},function(){});}
    if(!saved){setState('⚠ Download blocked — copy the notes from this box','warn');
               toast('Download blocked — copy the notes before leaving');return;}
    setState('Download requested '+nowStamp(),'ok');
    toast('Download requested — '+r.n+' note'+(r.n===1?'':'s'));
  });
  document.getElementById('btn-export2').addEventListener('click',function(){document.getElementById('btn-export').click();});

  document.getElementById('modal-copy').addEventListener('click',function(){
    // Success is reported only once a copy actually happened: the legacy
    // execCommand returned true, or the clipboard promise fulfilled. Claiming it
    // before writeText settled told the reviewer a rejected copy had worked (#477).
    function report(ok){if(ok){dirty=false;setState('✓ Copied to clipboard '+nowStamp(),'ok');}
                        toast(ok?'Copied to clipboard':'Select the text and copy');}
    modalTa.select();var legacy=false;try{legacy=document.execCommand('copy')===true;}catch(e){}
    if(legacy){report(true);return;}
    var p=null;
    try{if(navigator.clipboard&&navigator.clipboard.writeText){p=navigator.clipboard.writeText(modalTa.value);}}catch(e){p=null;}
    if(p&&typeof p.then==='function'){p.then(function(){report(true);},function(){report(false);});}
    else{report(false);}
  });
  document.getElementById('modal-dl').addEventListener('click',function(){download(exportName(),modalTa.value);toast('Download requested');});
  document.getElementById('modal-close').addEventListener('click',closeModal);
  modal.addEventListener('click',function(e){if(e.target===modal)closeModal();});

  // buildExport only ever writes strings, but an import takes whatever the file
  // holds: an object arrives as "[object Object]", an array joins on commas,
  // null becomes "null" — each then persisted over notes the reviewer wrote.
  // The realistic trigger is a hand-edited or truncated export, not an attack;
  // it just fails in the least useful way, silently and destructively. Skip
  // anything that is not a string and say how many were skipped.
  function applyData(obj){
    var applied=0,skipped=0;
    tas.forEach(function(t){
      if(!Object.prototype.hasOwnProperty.call(obj,t.dataset.key))return;
      var v=obj[t.dataset.key];
      if(typeof v!=='string'){skipped++;return;}
      t.value=v;save(t);autoGrow(t);setFilled(t);applied++;
    });
    updateCount();return{applied:applied,skipped:skipped};
  }
  document.getElementById('file-import').addEventListener('change',function(e){
    var f=e.target.files[0];if(!f)return;
    var rd=new FileReader();
    rd.onload=function(){
      var txt=String(rd.result||'');var obj=null;
      // Last LINE-ANCHORED marker. A plain lastIndexOf lands inside the JSON
      // string of a note that mentions the marker, and the import then reads as
      // a corrupt file — the same defect the Python seeder had (#239).
      var mk=/^[ \t]*<!--[ \t]*SPEC-NOTES-DATA/gm,mm=null,mx;
      while((mx=mk.exec(txt))!==null){mm=mx;}
      try{
        if(mm){var after=txt.slice(mm.index+mm[0].length);var jstart=after.indexOf('{');var jend=after.lastIndexOf('}');
                  if(jstart>=0&&jend>jstart){obj=JSON.parse(after.slice(jstart,jend+1));}}
        else{obj=JSON.parse(txt);}
      }catch(err){obj=null;}
      if(!obj){toast('Could not read notes from that file');return;}
      var r=applyData(obj);
      toast('Imported '+r.applied+' note'+(r.applied===1?'':'s')+
            (r.skipped?' ('+r.skipped+' skipped — not text)':''));
    };
    rd.readAsText(f);e.target.value='';
  });
  document.getElementById('btn-import').addEventListener('click',function(){document.getElementById('file-import').click();});

  document.getElementById('btn-clear').addEventListener('click',function(){
    if(!confirm('Clear ALL notes from this browser? Export first if you want to keep them.'))return;
    tas.forEach(function(t){t.value='';save(t);autoGrow(t);setFilled(t);});
    updateCount();toast('All notes cleared');
  });

  document.getElementById('btn-top').addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'});});
})();
"""

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="arsenal-source-sha256" content="__SOURCE_SHA__">
<title>__TITLE__ — __DOC_LABEL__ (annotatable)</title>
<style>__CSS__</style>
</head>
<body>
<div class="topbar">
  <div class="wrap">
    <div class="brand">__TITLE__ — __DOC_LABEL__<small>annotatable reader · __GEN_DATE__</small></div>
    <span class="savestate ok" id="savestate">✓ Auto-saving</span>
    <button class="btn primary" id="btn-export">Save / Export</button>
  </div>
</div>
<div class="statusbar"><div class="wrap"><span class="count" id="count">0 / __TOTAL__ notes</span><span class="hint"> · notes save automatically; <strong>Save / Export</strong> keeps a file you can send back</span></div></div>
<div id="warnbar"><div class="wrap"><strong>Heads up —</strong> this browser isn't storing your notes automatically (private/incognito mode or restricted file view). Tap <strong>Save / Export</strong> regularly.</div></div>

<div class="wrap">
  <div class="hero">
    <h1>__TITLE__ — __DOC_LABEL__</h1>
    <p>Read at your own pace and leave a note on any point you want to discuss.</p>
  </div>
  <details class="help">
    <summary>How to annotate (tap)</summary>
    <ul>
      <li>Tap the note field under any section and type. Each note is tagged so your feedback maps to an exact point.</li>
      <li><strong>Notes save automatically</strong> in this browser as you type.</li>
      <li><strong>Save / Export</strong> downloads a Markdown file of your notes — the copy to send back. Tap whenever you pause, especially on a phone.</li>
      <li><strong>Import</strong> reloads notes from a file you exported earlier (after clearing the browser or switching devices).</li>
      <li><strong>Clear</strong> erases every note in this browser. Export first.</li>
    </ul>
  </details>
  __TOC__
  __BODY__
</div>

<div class="fab">
  <button class="btn" id="btn-top" title="Back to top">↑ Top</button>
  <button class="btn" id="btn-import">Import</button>
  <button class="btn" id="btn-clear">Clear</button>
  <button class="btn primary" id="btn-export2">💾 Save / Export</button>
</div>
<input type="file" id="file-import" class="hidden-input" accept=".md,.markdown,.json,text/markdown,application/json">

<div class="modal" id="modal">
  <div class="modal-card">
    <h3>Your notes</h3>
    <p>Downloaded as a Markdown file. You can also copy the text below and paste it back.</p>
    <textarea id="modal-ta" readonly></textarea>
    <div class="modal-actions">
      <button class="btn primary" id="modal-copy">Copy</button>
      <button class="btn" id="modal-dl">Download again</button>
      <button class="btn" id="modal-close">Close</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>var SPEC_SEED_NOTES=__SEED_NOTES__;</script>
<script>__JS__</script>
</body>
</html>
"""


# ---------------------------------------------------------------- Entry point


def main() -> int:
    try:
        return _main()
    except DrawspecError as exc:
        # Before anything is written: a reader missing its diagram, or showing a
        # broken one, is not a reader to hand over.
        print(f"✗ {exc}", file=sys.stderr)
        return 1


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", metavar="FILE", help="single spec or plan Markdown file")
    parser.add_argument(
        "--input-dir", metavar="DIR", help="directory containing workspace subdirs with spec.md"
    )
    parser.add_argument(
        "--notes",
        metavar="FILE",
        help="returned export to seed notes from (default: <output-dir>/notes.json)",
    )
    parser.add_argument(
        "--output-dir", metavar="DIR", help="where to write the reader and annotated Markdown"
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="explicit HTML reader path, overriding the generated name; the annotated "
        "Markdown is written beside it as <stem>-annotated.md",
    )
    parser.add_argument(
        "--name", metavar="TEXT", help="project name for the reader title (default: git repo name)"
    )
    args = parser.parse_args()

    cwd = Path.cwd()
    single_label, doc_slug = DEFAULT_DOC_KIND[1], DEFAULT_DOC_KIND[2]
    out_base = ""

    if args.input:
        spec_path = Path(args.input)
        if not spec_path.is_file():
            print(f"✗ {spec_path} not found", file=sys.stderr)
            return 2
        parts = collect_parts_single(spec_path)
        _, single_label, doc_slug = doc_kind(spec_path.stem)
        # A document that is not the canonical spec or plan names its own output:
        # two design files in one `docs/**/specs/` folder would otherwise share
        # one `spec-reader.html`, and each regeneration would stale the other.
        if spec_path.stem.lower() not in CANONICAL_STEMS:
            out_base = spec_path.stem
        default_out = spec_path.parent
    elif args.input_dir:
        ws_dir = Path(args.input_dir)
        if not ws_dir.is_dir():
            print(f"✗ {ws_dir} is not a directory", file=sys.stderr)
            return 2
        parts = collect_parts_workspace(ws_dir)
        if not parts:
            print(f"✗ no workspace spec.md files found under {ws_dir}", file=sys.stderr)
            return 1
        default_out = cwd / "docs" / "spec-reader"
    else:
        mode, parts = auto_discover(cwd)
        default_out = (cwd / "docs" / "spec-reader") if mode == "workspace" else (cwd / "status")

    if args.output:
        html_file = Path(args.output)
        out_dir = html_file.parent
    else:
        out_dir = Path(args.output_dir) if args.output_dir else default_out
    out_dir.mkdir(parents=True, exist_ok=True)

    title = args.name or infer_title(cwd)
    gen_date = date.today().isoformat()

    # `<repo>-<doc>-v<N>-<date>-reader.html`: a reader that says what it is from a
    # Downloads folder or a chat attachment, and an export named the same way.
    # The doc is the kind for the canonical spec/plan and the stem otherwise, so
    # two design docs sharing a folder still get separate readers.
    repo = repo_name(cwd)
    doc = out_base or doc_slug
    version = doc_version(parts)
    stem = output_stem(repo, doc, version, gen_date)
    export_stem = stem[: -len(gen_date) - 1]
    if args.output:
        base = html_file.name.removesuffix(".html").removesuffix("-reader")
        md_file = out_dir / f"{base}-annotated.md"
    else:
        html_file = out_dir / f"{stem}-reader.html"
        md_file = out_dir / f"{stem}-annotated.md"

    notes_path = Path(args.notes) if args.notes else out_dir / "notes.json"
    seed_notes: dict = {}

    if notes_path.exists():
        try:
            loaded = read_notes(notes_path)
        except (ValueError, OSError) as exc:
            print(f"⚠  could not read {notes_path}: {exc}", file=sys.stderr)
            # Continuing unseeded is right for a stray notes.json — the reader
            # is still worth writing. It is not right when the unreadable file
            # is one this run is about to write: the notes are then replaced by
            # a fresh empty reader, and the warning scrolls past above the ✓
            # that says it worked.
            if notes_path.resolve() in {html_file.resolve(), md_file.resolve()}:
                print(
                    f"✗ {notes_path} is also this run's output — refusing to overwrite "
                    "notes that could not be read. Fix the file, or point --notes at "
                    "the export the page produced.",
                    file=sys.stderr,
                )
                return 2
        else:
            seed_notes = loaded
            print(f"ℹ  seeding from {notes_path} ({len(seed_notes)} notes)", file=sys.stderr)
    elif args.notes:
        print(f"✗ {notes_path} does not exist", file=sys.stderr)
        return 2

    html_out = build_html(
        parts,
        title,
        gen_date,
        seed_notes,
        single_label,
        doc_slug,
        ns_key=out_base or None,
        export_stem=export_stem,
    )
    md_out = build_markdown(parts, title, gen_date, seed_notes, single_label)

    html_file.write_text(html_out, encoding="utf-8")
    md_file.write_text(md_out, encoding="utf-8")
    if not args.output:
        keep = {html_file, md_file, notes_path}
        remove_superseded(superseded(out_dir, f"{slug(repo)}-{slug(doc)}", doc, keep))

    n_parts = len(parts)
    n_secs = sum(len(p["items"]) for _, p in parts)
    n_notes = sum(has_note_field(it, seed_notes) for _, p in parts for it in p["items"])
    print(f"✓ {n_parts} part(s), {n_secs} section(s), {n_notes} note field(s)", file=sys.stderr)
    print(f"  {html_file}", file=sys.stderr)
    print(f"  {md_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    # Windows consoles default to a legacy codepage (cp1252 and friends);
    # a non-ASCII line must degrade to "?", never take the process down.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
