# LAYOUT: Context

<!-- ≤200-word worker brief — written by /specify in workspace mode -->

Page layout and long-document structure (`spec.md`, ideas I-34 to I-39): section breaks,
page numbering (start, format, shown or hidden), odd/even and first-page headers, mirror
margins, recto starts, TOC, index, captions, cross-references and citation fields.

- Recommended: a declarative page plan (`layout.get` / `layout.apply`) that hides Word's
  header-inheritance rules, plus field primitives.
- Layout writes are tracked where Word can track them.
- LibreOffice is used only to refresh field results and to verify page labels; the core
  stays stdlib-only.
- Builds on STORIES (`sections.list`, header and footer stories).
