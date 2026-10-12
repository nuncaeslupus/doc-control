"""Round-trip cases for the bundled recipes ported from docx-fixer's automatic rules.

Each case is ``(recipe, before, after)``: one paragraph of synthetic text and what the
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

CASES: list[tuple[str, str, str]] = [
    # dialogue opener
    ("es-dialogue-dash", "- Hola", f"{EM}Hola"),
    ("es-dialogue-dash", "-Hola", f"{EM}Hola"),
    ("es-dialogue-dash", "– Hola", f"{EM}Hola"),
    ("es-dialogue-dash", "--Hola", f"{EM}Hola"),
    ("es-dialogue-dash", f"{EM}{EM}Hola", f"{EM}Hola"),
    ("es-dialogue-dash", f"{EM} Hola", f"{EM}Hola"),
    ("es-dialogue-dash", f"-{NBSP}¿Qué tal?", f"{EM}¿Qué tal?"),
    ("es-dialogue-dash", "  - Hola", f"  {EM}Hola"),
    ("es-dialogue-dash", f"{EM}Hola", f"{EM}Hola"),
    ("es-dialogue-dash", "Un guion - suelto", "Un guion - suelto"),
    ("es-dialogue-dash", "-", "-"),
    # aside dashes inside dialogue
    ("es-aside-dash", f"{EM}Hola -dijo ella-. Adiós", f"{EM}Hola {EM}dijo ella{EM}. Adiós"),
    (
        "es-aside-dash",
        f"{EM}Hola -dijo ella- y siguió",
        f"{EM}Hola {EM}dijo ella{EM} y siguió",
    ),
    ("es-aside-dash", f"{EM}Un físico-químico", f"{EM}Un físico-químico"),
    ("es-aside-dash", "Sin raya -inicial- no cambia", "Sin raya -inicial- no cambia"),
    # dash glued before an aside
    (
        "es-dash-first-aside",
        f"{EM}Te admiramos{EM} le respondieron.",
        f"{EM}Te admiramos {EM}le respondieron.",
    ),
    (
        "es-dash-first-aside",
        f"{EM}Hola {EM}dijo ella{EM} y se fue",
        f"{EM}Hola {EM}dijo ella{EM} y se fue",
    ),
    # closing dash
    (
        "es-dash-close-space",
        f"{EM}Hola {EM}dijo ella {EM}. Adiós",
        f"{EM}Hola {EM}dijo ella{EM}. Adiós",
    ),
    ("es-dash-close-space", f"{EM}Hola {EM}dijo ella {EM}", f"{EM}Hola {EM}dijo ella{EM}"),
    (
        "es-dash-close-space",
        f"{EM}Hola {EM}dijo ella{EM} y siguió",
        f"{EM}Hola {EM}dijo ella{EM} y siguió",
    ),
    # opening dash of an aside
    (
        "es-dash-open-space",
        f"{EM}Hola {EM} dijo ella{EM}. Adiós",
        f"{EM}Hola {EM}dijo ella{EM}. Adiós",
    ),
    ("es-dash-open-space", f"{EM}Hola {EM}dijo ella{EM}.", f"{EM}Hola {EM}dijo ella{EM}."),
    (
        "es-dash-open-space",
        "Texto — aparte — en inglés",
        "Texto — aparte — en inglés",
    ),
    # whitespace
    ("collapse-spaces", "Dos  espacios y   tres", "Dos espacios y tres"),
    ("collapse-spaces", f"Duro{NBSP} y normal", "Duro y normal"),
    ("collapse-spaces", "  Sangría inicial", "  Sangría inicial"),
    ("collapse-spaces", "Uno solo", "Uno solo"),
    ("trim-trailing-spaces", "Final con espacios   ", "Final con espacios"),
    ("trim-trailing-spaces", "Final con tabulador\t", "Final con tabulador"),
    ("trim-trailing-spaces", "Limpio", "Limpio"),
    # punctuation spacing
    ("es-space-before-punctuation", "Hola , mundo .", "Hola, mundo."),
    ("es-space-before-punctuation", "¿Qué tal ?", "¿Qué tal?"),
    ("es-space-before-punctuation", "Dijo (mal ) que sí", "Dijo (mal) que sí"),
    ("es-space-before-punctuation", "Sin cambios, de verdad.", "Sin cambios, de verdad."),
    ("es-space-after-opener", "Dijo ( mal) que sí", "Dijo (mal) que sí"),
    ("es-space-after-opener", "¿ Qué?", "¿Qué?"),
    ("es-space-after-opener", "« hola»", "«hola»"),
    ("es-space-after-opener", "Dijo (mal) que s\u00ed", "Dijo (mal) que s\u00ed"),
    ("es-space-after-punctuation", "Uno,dos;tres:cuatro", "Uno, dos; tres: cuatro"),
    ("es-space-after-punctuation", "Hora 10:30 y 3,5", "Hora 10:30 y 3,5"),
    ("es-space-after-punctuation", "Ya, está bien", "Ya, está bien"),
    # ellipsis
    ("ellipsis-char", "Espera... y luego", "Espera… y luego"),
    ("ellipsis-char", "Espera…. Y luego", "Espera… Y luego"),
    ("ellipsis-char", "Cuatro.... se queda", "Cuatro.... se queda"),
    ("ellipsis-char", "Ya está…", "Ya está…"),
    # apostrophe and quotes
    ("typographic-apostrophe", "rock'n'roll y o'clock", "rock’n’roll y o’clock"),
    ("typographic-apostrophe", "'comillas simples'", "'comillas simples'"),
    ("es-quotes-open", 'Dijo "hola" y se fue', 'Dijo «hola" y se fue'),
    ("es-quotes-open", '"Inicio" de línea', '«Inicio" de línea'),
    ("es-quotes-open", '(¿"Qué?")', '(¿«Qué?")'),
    ("es-quotes-open", 'Pulgadas 5 " aparte', 'Pulgadas 5 " aparte'),
    ("es-quotes-close", 'Dijo "hola" y se fue', 'Dijo "hola» y se fue'),
    ("es-quotes-close", 'Dijo "hola".', 'Dijo "hola».'),
    ("es-quotes-close", 'Dijo "hola"', 'Dijo "hola»'),
    ("es-quotes-close", 'Pulgadas 5 " aparte', 'Pulgadas 5 " aparte'),
]

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
    ("Texto limpio sin fallos.", "Texto limpio sin fallos."),
]
