"""A 100k-word document that also carries many headings, comments and revisions.

The corpus "novel" has a few dozen headings and no comments or revisions, so every
list in it fits one response. This adds a few hundred of each to the same
generator, so every list operation has to page.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from tests.corpus import generator

EXTRA_SECTIONS = 80


class _BigBuilder(generator._Builder):
    def novel(self, target_words: int = 100_000) -> list[str]:
        blocks = super().novel(target_words)
        for n in range(EXTRA_SECTIONS):
            blocks.append(self.heading(2, f"Annotated section {n + 1} {self.sentence(5)}"))
            blocks += self.revisions()
            blocks += self.comments_feature()
        return blocks


def write_big_doc(path: Path) -> Path:
    """Write the big document (deterministic) to ``path``."""
    with mock.patch.object(generator, "_Builder", _BigBuilder):
        path.write_bytes(generator.generate("novel", 7))
    return path
