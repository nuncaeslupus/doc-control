"""Compose the README's hero image — a redline sheet for doc-control.

Not shipped code. Writes docs/hero.svg, then rasterises it to docs/hero.png,
and docs/logo.svg to docs/logo.png, with a headless Chromium (the README shows
the PNGs: GitHub's `<img>` view of an SVG loads no fonts, so the PNG is what
keeps the faces). Needs the Inter,
DejaVu Sans Mono and Liberation Serif fonts installed for the PNG to match, and
Pillow to crop.

The sheet deliberately carries no version or counts: a PNG cannot follow the
repo, so anything that changes on a release stays off it.

Usage, from the repository root:

    python3 tools/hero.py           # write docs/hero.svg, docs/hero.png and docs/logo.png
    CHROMIUM=/path/to/chrome python3 tools/hero.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
OUT_SVG = ROOT / "docs" / "hero.svg"
OUT_PNG = ROOT / "docs" / "hero.png"
OUT_LOGO = ROOT / "docs" / "logo.png"

#: The GitHub social-preview size, which the README shows scaled to its column.
W, H = 1280, 640

GROUND = "#f4f3f8"
GRID = "#e6e3ef"
LINE = "#c9c3da"
INK = "#1f1b2e"
DIM = "#6e6880"
ACCENT = "#4a3a7a"  # the logo's purple
BAR = "#f2b632"  # the change bar Word draws in the margin
DEL = "#b8423b"
INS = "#2f7d4f"
PAPER = "#ffffff"
SANS = "Inter, 'DejaVu Sans', sans-serif"
DISPLAY = "'Inter Display', Inter, sans-serif"
MONO = "'DejaVu Sans Mono', monospace"
SERIF = "'Liberation Serif', 'DejaVu Serif', serif"
MONO_EM = 0.602  # DejaVu Sans Mono advance, in em


def serif_width(s: str, size: float) -> float:
    """Advance of `s` in the page's serif, from the installed face (fc-match)."""
    from PIL import ImageFont

    path = subprocess.run(
        ["fc-match", "-f", "%{file}", "Liberation Serif"], capture_output=True, text=True
    ).stdout
    return ImageFont.truetype(path, 100).getlength(s) * size / 100


def text(
    x: float,
    y: float,
    s: str,
    *,
    size: float,
    fill: str = INK,
    family: str = SANS,
    weight: int = 400,
    anchor: str = "start",
    spacing: float = 0,
) -> str:
    return (
        f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" '
        f'letter-spacing="{spacing}" xml:space="preserve">{escape(s)}</text>'
    )


def nested(path: Path, x: float, y: float, size: float) -> str:
    src = path.read_text().strip()
    inner = src[src.index(">") + 1 : src.rindex("</svg>")]
    return f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 64 64">{inner}</svg>'


def sheet() -> list[str]:
    """Grid, border with zone marks, registration marks — the claude-arsenal family."""
    out = [f'<rect width="{W}" height="{H}" fill="{GROUND}"/>']
    for x in range(0, W, 20):
        out.append(
            f'<line x1="{x}" y1="0" x2="{x}" y2="{H}" stroke="{GRID}" '
            f'stroke-width="{1 if x % 100 == 0 else 0.4}"/>'
        )
    for y in range(0, H, 20):
        out.append(
            f'<line x1="0" y1="{y}" x2="{W}" y2="{y}" stroke="{GRID}" '
            f'stroke-width="{1 if y % 100 == 0 else 0.4}"/>'
        )
    out.append(
        f'<rect x="16" y="16" width="{W - 32}" height="{H - 32}" fill="none" stroke="{LINE}"/>'
    )
    out.append(
        f'<rect x="30" y="30" width="{W - 60}" height="{H - 60}" fill="none" '
        f'stroke="{DIM}" stroke-width="1.5"/>'
    )
    zw = (W - 60) / 8
    for i in range(8):
        x = 30 + zw * i
        if i:
            out += [
                f'<line x1="{x}" y1="16" x2="{x}" y2="30" stroke="{LINE}"/>',
                f'<line x1="{x}" y1="{H - 30}" x2="{x}" y2="{H - 16}" stroke="{LINE}"/>',
            ]
        for y in (27, H - 19):
            out.append(
                text(x + zw / 2, y, str(8 - i), size=9, fill=DIM, family=MONO, anchor="middle")
            )
    zh = (H - 60) / 4
    for i in range(4):
        y = 30 + zh * i
        if i:
            out += [
                f'<line x1="16" y1="{y}" x2="30" y2="{y}" stroke="{LINE}"/>',
                f'<line x1="{W - 30}" y1="{y}" x2="{W - 16}" y2="{y}" stroke="{LINE}"/>',
            ]
        for x in (23, W - 23):
            out.append(
                text(x, y + zh / 2 + 3, "ABCD"[i], size=9, fill=DIM, family=MONO, anchor="middle")
            )
    for cx, cy in ((48, 48), (48, H - 48)):
        out += [
            f'<circle cx="{cx}" cy="{cy}" r="7" fill="none" stroke="{ACCENT}"/>',
            f'<line x1="{cx - 11}" y1="{cy}" x2="{cx + 11}" y2="{cy}" stroke="{ACCENT}"/>',
            f'<line x1="{cx}" y1="{cy - 11}" x2="{cx}" y2="{cy + 11}" stroke="{ACCENT}"/>',
        ]
    return out


#: One rule in every hero: the logo tile is 0.95 x the wordmark's font size, centred on
#: its capital band (baseline to cap height, 0.727 em), with a quarter-em gap before it.
WORD_SIZE, WORD_BASE, LOGO_X = 84, 150, 64
LOGO_SIZE = round(0.95 * WORD_SIZE)
LOGO_Y = round(WORD_BASE - 0.727 * WORD_SIZE / 2 - LOGO_SIZE / 2)
TEXT_X = LOGO_X + LOGO_SIZE + round(0.25 * WORD_SIZE)


def wordmark() -> list[str]:
    return [
        nested(ROOT / "docs" / "logo.svg", LOGO_X, LOGO_Y, LOGO_SIZE),
        f'<text x="{TEXT_X}" y="{WORD_BASE}" font-family="{DISPLAY}" '
        f'font-size="{WORD_SIZE}" font-weight="800" letter-spacing="-3">'
        f'<tspan fill="{INK}">doc-</tspan><tspan fill="{ACCENT}">control</tspan></text>',
        text(LOGO_X, 204, "Let AI agents edit Word documents safely.", size=27, fill="#3d3752"),
        text(
            LOGO_X,
            238,
            "Deterministic .docx edits · every change tracked · the writer decides",
            size=17,
            fill=DIM,
        ),
    ]


def terminal() -> list[str]:
    """The agent's side: one JSON batch in, one JSON verdict out."""
    x, y, w, h = 64, 290, 540, 214
    out = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="#1f1b2e"/>',
        f'<path d="M {x} {y + 28} H {x + w}" stroke="#3a3450"/>',
        text(
            x + w / 2,
            y + 18,
            "agent → doc-control",
            size=10,
            fill="#9a93b3",
            family=MONO,
            anchor="middle",
        ),
    ]
    for i, c in enumerate(("#e06c6c", "#e0b84c", "#7fbf7f")):
        out.append(f'<circle cx="{x + 16 + i * 14}" cy="{y + 14}" r="4.5" fill="{c}"/>')
    key, val, punct = "#c9bff0", "#f2d29b", "#e9e6f2"
    rows: list[list[tuple[str, str]]] = [
        [("$ ", BAR), ("doc-control doc.batch --json '{", punct)],
        [
            ('  "doc"', key),
            (": ", punct),
            ('"novel.docx"', val),
            (", ", punct),
            ('"ops"', key),
            (": [", punct),
        ],
        [
            ("    {", punct),
            ('"op"', key),
            (": ", punct),
            ('"text.replace"', val),
            (", ", punct),
            ('"at"', key),
            (": ", punct),
            ('"p12#a7b2:4-8"', val),
            (",", punct),
        ],
        [('     "text"', key), (": ", punct), ('"snow"', val), ("},", punct)],
        [
            ("    {", punct),
            ('"op"', key),
            (": ", punct),
            ('"comment.add"', val),
            (", ", punct),
            ('"at"', key),
            (": ", punct),
            ('"p12#a7b2:44-55"', val),
            (",", punct),
        ],
        [('     "text"', key), (": ", punct), ('"Too soon?"', val), ("}]}'", punct)],
        [],
        [('{"ok": true, "applied": 2, "tracked": 2}', "#8fd19e")],
    ]
    size, lead = 12.5, 20
    for r, row in enumerate(rows):
        tx = x + 18
        ty = y + 56 + r * lead
        for chunk, fill in row:
            out.append(text(tx, ty, chunk, size=size, fill=fill, family=MONO))
            tx += len(chunk) * size * MONO_EM
    return out


def page() -> list[str]:
    """The writer's side: the same edits, as tracked changes in the open document."""
    px, py, pw, ph = 676, 58, 360, 444
    out = [
        f'<rect x="{px + 4}" y="{py + 5}" width="{pw}" height="{ph}" fill="{INK}" '
        'fill-opacity="0.08"/>',
        f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" fill="{PAPER}" stroke="{LINE}"/>',
        text(
            px, py - 8, "novel.docx · Track Changes: On", size=9, fill=DIM, family=MONO, spacing=0.5
        ),
    ]
    lx, size, lead = px + 40, 15, 23

    def filler(y: float, widths: tuple[int, ...]) -> None:
        for i, w in enumerate(widths):
            out.append(
                f'<rect x="{lx}" y="{y + i * lead - 8}" width="{w}" height="6" rx="3" '
                f'fill="{GRID}"/>'
            )

    filler(py + 48, (280, 268, 276, 150))
    # The edited paragraph, set in the document's own serif.
    y0 = py + 170
    out.append(
        f'<rect x="{lx - 26}" y="{y0 - 16}" width="2.5" height="{lead * 2 + 2}" fill="{BAR}"/>'
    )
    out.append(text(lx, y0 - 40, "¶ 12", size=9, fill=DIM, family=MONO))
    out.append(
        f'<text x="{lx}" y="{y0}" font-family="{SERIF}" font-size="{size}" fill="{INK}" '
        'xml:space="preserve">The '
        f'<tspan fill="{DEL}" text-decoration="line-through">rain</tspan>'
        f'<tspan fill="{INS}" text-decoration="underline">snow</tspan>'
        " fell on the harbour all night,</text>"
    )
    hl_x = lx + serif_width("and ", size)
    hl_w = serif_width("before dawn", size)
    out.append(
        f'<rect x="{hl_x - 1:.1f}" y="{y0 + lead - 13}" width="{hl_w + 2:.1f}" height="17" '
        'fill="#e9e3fb"/>'
    )
    out.append(
        f'<text x="{lx}" y="{y0 + lead}" font-family="{SERIF}" font-size="{size}" '
        f'fill="{INK}" xml:space="preserve">and before dawn the boats were '
        "gone.</text>"
    )
    filler(y0 + lead * 2 + 4, (284, 262, 272, 120))
    filler(y0 + lead * 7, (280, 276, 200))

    # Balloons in the markup pane, each tied to its anchor.
    bx, bw = px + pw + 22, 160
    out.append(
        f'<path d="M {lx + serif_width("The rain", size):.1f} {y0 - 15} V {y0 - 22} '
        f'L {px + pw - 8} {y0 - 22} H {bx}" fill="none" '
        f'stroke="{DEL}" stroke-dasharray="3 2"/>'
    )
    out.append(
        f'<path d="M {hl_x + hl_w / 2:.1f} {y0 + lead + 4} V {y0 + 92} '
        f'L {px + pw - 8} {y0 + 92} H {bx}" fill="none" '
        f'stroke="{ACCENT}" stroke-dasharray="3 2"/>'
    )

    def balloon(y: float, head: str, colour: str, lines: list[str], buttons: bool) -> None:
        h = 30 + 16 * len(lines) + (30 if buttons else 0)
        out.append(
            f'<rect x="{bx}" y="{y}" width="{bw}" height="{h}" rx="5" fill="{PAPER}" '
            f'stroke="{colour}" stroke-width="1.2"/>'
        )
        out.append(text(bx + 10, y + 18, head, size=9.5, fill=colour, family=MONO, weight=700))
        for i, line in enumerate(lines):
            out.append(text(bx + 10, y + 37 + i * 16, line, size=12, fill=INK))
        if buttons:
            by = y + h - 28
            for i, (label, c) in enumerate((("✓ Accept", INS), ("✕ Reject", DEL))):
                cx = bx + 10 + i * 72
                out.append(
                    f'<rect x="{cx}" y="{by}" width="66" height="20" rx="10" fill="none" '
                    f'stroke="{c}"/>'
                )
                out.append(
                    text(cx + 33, by + 14, label, size=10.5, fill=c, anchor="middle", weight=600)
                )

    balloon(y0 - 44, "agent · replaced", DEL, ["“rain” → “snow”"], True)
    balloon(y0 + 72, "agent · comment", ACCENT, ["Too soon?"], False)
    return out


def leader() -> list[str]:
    y, x1, x2 = 397, 612, 668
    return [
        f'<path d="M {x1} {y} H {x2 - 8}" stroke="{ACCENT}" stroke-width="1.2" '
        'stroke-dasharray="6 4"/>',
        f'<circle cx="{x1}" cy="{y}" r="2.6" fill="{ACCENT}"/>',
        f'<path d="M {x2} {y} l -9 -3.6 v 7.2 Z" fill="{ACCENT}"/>',
        text((x1 + x2) / 2, y - 9, "tracked", size=9.5, fill=ACCENT, family=MONO, anchor="middle"),
    ]


def title_block() -> list[str]:
    x, y, w, h = 676, 528, 522, 64
    cells = [
        (0, "PROJECT", "doc-control"),
        (200, "SHEET", "Tracked edits for Word"),
        (400, "LICENSE", "Apache-2.0"),
    ]
    out = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{GROUND}" stroke="{DIM}" '
        'stroke-width="1.2"/>'
    ]
    for cx, label, value in cells:
        if cx:
            out.append(f'<line x1="{x + cx}" y1="{y}" x2="{x + cx}" y2="{y + h}" stroke="{DIM}"/>')
        out.append(text(x + cx + 10, y + 18, label, size=9, fill=DIM, family=MONO, spacing=1.5))
        big = label == "PROJECT"
        out.append(
            text(
                x + cx + 10,
                y + 46,
                value,
                size=19 if big else 12,
                weight=700 if big else 400,
                family=MONO,
            )
        )
    return out


def width(s: str, size: float, face: str) -> float:
    """Advance of `s` in an installed face (an fc-match pattern)."""
    from PIL import ImageFont

    path = subprocess.run(
        ["fc-match", "-f", "%{file}", face], capture_output=True, text=True
    ).stdout
    return ImageFont.truetype(path, 100).getlength(s) * size / 100


ARSENAL = "#c4623f"  # claude-arsenal's own orange


def powered_by() -> list[str]:
    """The claude-arsenal stamp, set on the frame's bottom edge: what this repo is built with."""
    lead, name, url = "built with", "claude-arsenal", "github.com/nuncaeslupus/claude-arsenal"
    tile, pad, gap = 22, 8, 8
    lead_w = width(lead, 12, "Inter")
    name_w = width(name, 15, "Inter:weight=700")
    url_w = len(url) * 10 * MONO_EM
    total = pad + tile + gap + lead_w + 6 + name_w + 12 + url_w + pad + 6
    x, cy = 72, H - 30
    tx = x + pad + tile + gap
    return [
        f'<rect x="{x}" y="{cy - 16}" width="{total:.1f}" height="32" rx="16" '
        f'fill="{GROUND}" stroke="{ARSENAL}" stroke-width="1.5"/>',
        nested(ROOT / "docs" / "claude-arsenal-logo.svg", x + pad, cy - tile / 2, tile),
        text(tx, cy + 4.3, lead, size=12, fill=DIM),
        text(round(tx + lead_w + 6, 1), cy + 5.3, name, size=15, fill=ARSENAL, weight=700),
        text(
            round(tx + lead_w + 6 + name_w + 12, 1), cy + 3.6, url, size=10, fill=DIM, family=MONO
        ),
    ]


def svg() -> str:
    parts = sheet() + wordmark() + terminal() + leader() + page() + title_block() + powered_by()
    body = "\n  ".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" aria-label="doc-control: an agent edits a Word '
        f'document, every change a tracked change">\n  {body}\n</svg>\n'
    )


def chromium() -> str:
    for c in (
        os.environ.get("CHROMIUM"),
        "/opt/pw-browsers/chromium",
        shutil.which("chromium"),
        shutil.which("google-chrome"),
    ):
        if c and Path(c).exists():
            return c
    sys.exit("hero: no Chromium found; set CHROMIUM=/path/to/chrome")


def shoot(svg_src: str, out: Path, w: int, h: int) -> None:
    """Rasterise `svg_src` at 2x through headless Chromium into `out`."""
    with tempfile.TemporaryDirectory() as tmp:
        html = Path(tmp) / "shot.html"
        html.write_text(
            '<html><body style="margin:0;background:transparent">' + svg_src + "</body></html>"
        )
        subprocess.run(
            [
                chromium(),
                "--headless=new",
                "--no-sandbox",
                "--force-device-scale-factor=2",
                "--hide-scrollbars",
                "--default-background-color=00000000",
                f"--window-size={w},{h + 200}",
                f"--screenshot={out}",
                html.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
    # Headless Chromium's viewport is shorter than its window; shoot tall, crop.
    from PIL import Image

    with Image.open(out) as shot:
        shot.crop((0, 0, w * 2, h * 2)).save(out, optimize=True)


def main() -> None:
    OUT_SVG.write_text(svg())
    shoot(svg(), OUT_PNG, W, H)
    # The README heading's 40px logo, from the same source as the hero's tile.
    logo = (
        (ROOT / "docs" / "logo.svg")
        .read_text()
        .replace("<svg ", '<svg width="128" height="128" ', 1)
    )
    shoot(logo, OUT_LOGO, 128, 128)
    for out in (OUT_SVG, OUT_PNG, OUT_LOGO):
        print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
