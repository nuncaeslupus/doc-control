# literary-critic indexer vs doc-control v1

Surveyed 2026-10-12 from `nuncaeslupus/literary-critic` (public), `src/litcritic/`.

## What literary-critic does
- **Ingest** (`ingest.py`): stdlib `zipfile` + ElementTree; walks `w:body` (tables, `sdt`), skips `w:del`/`moveFrom`/text boxes/footnote refs; heading level and centring via `styles.xml` (`basedOn`); pages from `lastRenderedPageBreak` and explicit breaks; NFC, soft hyphens/zero-width removed, whitespace collapsed; empty paragraphs dropped and recorded as `blank_before`. Also reads `.odt`, `.md`, `.txt`. Output: `manuscript/vNNN.<ext>`, `text/vNNN.txt`, `text/vNNN.meta.json`.
- **Structure** (`structure.py`, `kinds/novel.py`): deterministic parts/chapters/scenes heuristics (chapter words, numerals, heading/style names, page breaks, centred/capitalised lines, symbol lines, blank runs, ≥150 words per chapter); anchors `[pN.]cNN.sN.pN`; fingerprint = 12 hex SHA-1 of case- and accent-folded text; `index/vNNN/structure.json`. No LLM; the agent may write fingerprint-keyed `analysis/structure-overrides.json`.
- **Support**: `diff.py` (paragraph alignment between drafts: moved/split/merged/modified), `spans.py` (W3C-style spans, exact-only relocation, else `changed`/`lost`), `locate.py` (chapter/scene/page/shortest unique first-last words), `textstats.py` (words, Spanish-aware sentences, syllables, dialogue spans), `query.py`.
- **Codex** (`codex.py`, `sync.py`, `codex_types/*.toml`): LLM-written typed items on spans, JSONL + change log + SQLite view; re-anchoring and re-read flags on new drafts.

## Ownership recommendation
- **doc-control**: everything that reads or writes the `.docx` — paragraphs and layout facts, segmentation (sentences, dialogue, scene breaks, chapters; configurable, with overrides), outline, stats, find, addresses, quote anchors (with an exact mode), paragraph diff between versions, tracked edits, rename.
- **literary-critic**: readable `cNN.sN.pN` labels as a thin layer over doc-control addresses; Codex, spans tied to items, sync/re-read, prompts, metrics, checklists; non-`.docx` ingest until doc-control supports `.odt`.

## Gaps in doc-control v1 contracts (proposed additions, pending owner decision)
1. `para.get`/`outline`: `empty`, `blank_before`, heading level, style, centred, page break before.
2. Normalised-text view with a two-way offset map (visible ↔ collapsed), or a declared normalisation option.
3. `segment`: stable chapter/scene ids, the signal that triggered each detection, overrides as a parameter, per-profile settings (e.g. minimum chapter words).
4. `anchor.resolve`: `mode: "exact"` and the match kind in the response.
5. Paragraph-level `diff`/alignment between two document versions (replaces `diff.py`), giving an address map across drafts, not only after doc-control's own edits.
6. Unique-phrase locators (first/last words) next to `page.estimate`.

Migration order for literary-critic: replace `docx_edit.py`/`docx_out.py` with doc-control batches → `.docx` ingest via `para.get`/`outline` → segmentation and structure detection → `anchor.resolve` inside `spans.relocate` (keeping exact-only).
