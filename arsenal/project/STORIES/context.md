# STORIES: Context

<!-- ≤200-word worker brief — written by /specify in workspace mode -->

Extend the file engine from the main story to footnotes, endnotes, headers, footers and
table structure (`spec.md`, ideas I-1 to I-3).

- Body addresses (`p<n>#<fp>`) must not change: `body_address_drift == 0`.
- Other stories use a prefix on the same grammar, e.g. `fn3/p1#fp` or `hdr1.default/p1#fp`,
  through the existing ops and `batch`.
- Reads stay body-only unless `stories` asks for more.
- Writes keep the safety guarantees: the footprint names the touched part, and
  `unintended_changes == 0`.
- The live engine stays body-only in this workspace.
- Start from `model/address.py`, `model/index.py`, `ooxml/splice.py`, `ops/tracked.py`,
  `safety/verify.py` and `safety/history.py`, where the body part is hard-coded today.
