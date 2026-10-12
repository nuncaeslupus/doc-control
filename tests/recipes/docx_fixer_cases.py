"""Round-trip cases for the bundled recipes ported from docx-fixer's automatic rules.

The per-recipe cases (``CASES``) live in ``test_es_typography.py``; ``SMOKE`` is here.
Each case is ``(before, after)``: one paragraph of synthetic text and what the
recipe leaves of it once its tracked changes are in. ``after == before`` marks text the
recipe must leave alone. ``APPLY_ORDER`` is the order that reaches the fixed point.
"""

from __future__ import annotations

EM = "—"
NBSP = " "

APPLY_ORDER = (
    "es-dialogue-dash",
    "es-aside-dash",
    "es-dash-first-aside",
    "es-dash-close-space",
    "es-dash-close-pair-space",
    "es-dash-open-space",
    "collapse-spaces",
    "trim-trailing-spaces",
    "es-space-before-punctuation",
    "es-space-after-opener",
    "ellipsis-char",
    "typographic-apostrophe",
    "es-quotes-open",
    "es-quotes-close",
    "es-space-after-punctuation",
)

# One broken paragraph per kind of fault and the text the whole set makes of it.
SMOKE: list[tuple[str, str]] = [
    ("-Hola  -dijo ella-. ¿Qué tal? ", f"{EM}Hola {EM}dijo ella{EM}. ¿Qué tal?"),
    (
        "Un párrafo con  dos espacios y puntos suspensivos...",
        "Un párrafo con dos espacios y puntos suspensivos…",
    ),
    (f"{EM}Te admiramos{EM} le respondieron.", f"{EM}Te admiramos {EM}le respondieron."),
    ("Dijo ( mal ) que sí .", "Dijo (mal) que sí."),
    ('Dijo "hola",pero "no".', "Dijo «hola», pero «no»."),
    (f"{EM}Hola {EM} dijo él {EM} y se fue.", f"{EM}Hola {EM}dijo él{EM} y se fue."),
    (f"{EM}Mira {EM}  dijo él {EM} que viene.", f"{EM}Mira {EM}dijo él{EM} que viene."),
    ("Mira https://ex.com/a,b...c y fin...", "Mira https://ex.com/a,b...c y fin…"),
    ("Texto limpio sin fallos.", "Texto limpio sin fallos."),
]
