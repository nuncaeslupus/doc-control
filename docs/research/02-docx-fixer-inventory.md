# docx-fixer inventory (input for bundled recipes)

Source: `nuncaeslupus/docx-fixer` @ e2510db, surveyed 2026-10-12. Stdlib-only Python 3.9+ (`zipfile` + `xml.etree`).
Rules register via `@rule(name, title, scope, kind)` (`docx_fixer/findings.py`); findings carry severity `auto|review|info`, kind `apply|edit|style|note`, paragraph-text offsets and a `replacement`.

## Paragraph text rules (`docx_fixer/rules/text.py`)

| id | detects | fix |
|---|---|---|
| leading-space | leading spaces/tabs; >=8 (`centering_min_spaces`) = fake centring, else fake tab | report (blind removal would left-align) |
| multi-space | 2+ spaces inside a line | auto |
| trailing-space | spaces before break / paragraph end | auto |
| nbsp | U+00A0 | report |
| tab-inside | tab after text | report |
| space-before-punct | space before `, . ; : ! ? … » ” )` (skips `70 %`) | auto |
| space-after-opener | space after `« “ ( ¿ ¡ [` | auto |
| missing-space-after-punct | `hola,que` | auto for `,;:`, review after `.` |
| repeated-punct | `,,` `???` | report |
| ellipsis | `...`→`…`, `….`→`…` (profile) | auto |
| dialogue-dash | line opened with `-`, `--`, `–`, `— ` | auto → `—` glued |
| inciso-dash | hyphen/en dash with one-sided space inside dialogue (skips compounds) | auto → `—` |
| dash-spacing | em-dash spacing (parity of open/close, context overrides) | auto |
| dash-punct-order | `—dijo.—` vs `—dijo—.` | report |
| dash-in-word | em dash glued between words | report |
| narrator-dash | `mí. —El` (full stop depends on speech verb) | report |
| hyphen-as-dash | lone ` - ` | report |
| straight-quote | `"` (auto only if paired in paragraph), `'`→`’` | auto / review |
| quote-style | `«»` mixed with `“”`, majority wins | info |
| unbalanced | `¿?` `¡!` per paragraph | report |
| punct-inside-quote | `.»` / `.)` | report |
| author-note | case-sensitive regexes (`XXXX`, `REVISAR`, `??`…) | report |

## Document-scope rules (`docx_fixer/rules/structure.py`)

| id | detects | fix |
|---|---|---|
| style-inventory | per-style counts; styles with <=2 paragraphs flagged | note |
| font-inventory | fonts/sizes forced by hand | note |
| direct-format | paragraph-level indent/alignment/spacing/numbering over style | note |
| body-indent | body paragraphs vs convention (tab/style/off/auto) | auto (insert `w:tab` / remove manual tab) |
| unpaired | `() [] «» “”` across the whole book | report |
| style-definition | centred style with first-line indent; indenting chapter-opening style | note |
| chapter-opening | first paragraph not `NormalIniciocaptulo` | note/style |
| heading-hygiene | empty titles, titles ending in `.` | note |
| heading-run | title split across paragraphs | note |
| pause | exactly 2 blank paragraphs → `Normal tras pausa` style | note |
| empty-paragraphs | other blank runs | note |
| possible-chapter-break | 3+ blank paragraphs | note |
| note-style | annotation-like styles | note |
| quote-attribution | epigraph author typed as quote + blank line | note |
| separator-style | `* * *` lines without `Separador` | note/style |
| breaks | manual page breaks; section vs heading count | note |
| leftovers | `w:ins`/`w:del`, highlight, hidden text, comments, mixed `w:lang` | note |

Also: `reading` suggestions from external JSON (never auto-applied). `dash-unclosed` is specified but not implemented.

## Other features

- CLI: `inspect`, `check` (HTML/JSON), `fix` (`--mark comments|highlight|none`, `--only`, `--skip`), `template` (.dotx), `apply-template`, `normalize`, `overhaul`, `review` (local web page), `export` (`.dudas.json`, chunks), `styles`, `profile`. Dropping a `.docx` on the exe opens `review`.
- House template as data (`stylespec.py`), style mapping ignoring accents/case, merge into existing ids, chapter openings, collapse pauses, install missing styles, stop `autoRedefine`.
- `normalize` changes only paragraph styles, never runs.
- Word comments per paragraph (author `docx-fixer`), creating `comments.xml` + content type + relationship.
- Review page: loopback server on port 0, per-session token, single inlined HTML, four sections (apply/edit/style/note), >=80 chars context, overlap conflicts, mixed-formatting warning.
- Profile JSON overlay (quotes, ellipsis, style ids, thresholds, author-note patterns, disabled rules); regexes validated up front.
- Tests synthesise documents from the house template; round-trip test runs `fix_until_stable` and checks every part parses and a second pass finds nothing.

## Implementation lessons

- Parts read as raw bytes, original order kept; only parsed trees re-serialised.
- `xmlns.py` registers every prefix and prunes undeclared `mc:Ignorable` entries (the ElementTree trap).
- Text model: flattened paragraph text with per-character offsets back to elements; edits queued and applied back to front; edit lands in first `w:t`, others emptied (keeps run formatting); refuses edits needing new tabs/breaks.
- `fix_until_stable`: up to 4 passes (one fix uncovers the next).
- Safety: output always a new file; refuse save if any style id moved; review keeps document in memory; `applied + skipped == submitted`.
- Pre-handoff checklist: paragraph count, word-for-word text, counts of `w:i`/`w:b`/`w:sectPr`/`w:br`/`w:commentReference`, element/attribute totals, every part parses, no mojibake, zip sound.
- OOXML traps: invented styles need `w:customStyle="1"`; schema child order inside `w:style`; `w:qFormat` for gallery; content type + relationship for `styles.xml`; omitted element = inherit; `w:autoRedefine`; keep `bCs`/`iCs` and `w:link`; exact id beats alias; author-note regexes case-sensitive.
- Known problems: CI never ran (no runner), default branch not `main`, `.exe` cross-built under Wine by hand, a test passed against its own bug (mutation-check tests), no spelling/grammar.

## Gates used there

Spec: `changes_written_without_being_listed==0`, `rendered_operations_per_finding==1`, `findings_offering_apply_without_a_concrete_replacement==0`, `structural_findings_with_an_apply_button==0`, `context_chars>=80`, `findings_without_manual_edit==0`, `input_file_mtime_unchanged`, `outbound_network_requests==0`, single exe, `clicks_to_decide_a_group==1`.
Plan: `rules_without_a_kind==0`, `duplicate_finding_ids==0`, `applied+skipped==decisions_submitted`, `rejected_findings_changing_text==0`, `undetected_overlaps==0`, `requests_served_without_a_valid_token==0`, `bind_address==127.0.0.1`, `unflagged_mixed_format_spans==0`, `xml_parse_errors==0`, `dist_files==1`.
