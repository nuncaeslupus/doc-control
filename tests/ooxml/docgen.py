"""Random ``word/document.xml`` generator for splice fuzzing.

Each document is built from a seed, so a failure names the seed that reproduces
it. The generator deliberately mixes the awkward cases the tokenizer must
survive: arbitrary and default-namespace prefixes, local re-declarations,
``mc:Ignorable`` namespaces, attributes holding ``>`` and quotes, comments,
CDATA and processing instructions that contain ``<w:p>`` text, self-closing
paragraphs, whitespace inside tags, tables, nested tables, ``w:sdt`` content,
text boxes with paragraphs inside a paragraph, and a ``p`` element from a
foreign namespace that must not count as a paragraph.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
EXTRA_NS = {
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
}
FOREIGN_NS = "urn:example:not-word"
TEXTS = [
    "Hola",
    "a &amp; b",
    "1 &lt; 2 &gt; 0",
    "caf&#233; &#x00F1;",
    "  espacios  ",
    "¿qué?",
    "comillas \" y '",
    "",
    "línea",
    "emoji \U0001f600",
]
ATTR_VALUES = ['"a>b"', "'x\"y'", '"1"', "'00A1B2C3'", '"&amp;&gt;&lt;"', '" spaced value "']


@dataclass
class _Gen:
    rng: random.Random
    w: str  # element prefix for W, "" for the default namespace
    paragraphs: int = 0
    nested: int = 0
    parts: list[str] = field(default_factory=list)

    def q(self, local: str, prefix: str | None = None) -> str:
        p = self.w if prefix is None else prefix
        return f"{p}:{local}" if p else local

    def ws(self) -> str:
        return self.rng.choice(["", "", "", " ", "  ", "\n", "\t"])

    def attrs(self) -> str:
        out: list[str] = []
        for _ in range(self.rng.randint(0, 2)):
            name = self.rng.choice(["w14:paraId", "w14:textId", "r:id", "o:x", self.q("val")])
            if f" {name}" in "".join(out):
                continue
            eq = self.rng.choice(["=", " = ", "=\n"])
            out.append(f" {name}{eq}{self.rng.choice(ATTR_VALUES)}")
        return "".join(out)

    def misc(self) -> str:
        r = self.rng.random()
        if r < 0.15:
            return "<!-- <w:p> not a paragraph </w:p> -->"
        if r < 0.22:
            return "<?dc-pi <w:p> > ?>"
        if r < 0.28:
            return "\n  "
        return ""

    def run(self, depth: int) -> str:
        r, t = self.q("r"), self.q("t")
        text = self.rng.choice(TEXTS)
        body = self.rng.random()
        if body < 0.15:
            content = f"<{t}><![CDATA[<{self.q('p')}> & ]]> fin]]&gt;</{t}>"
        elif body < 0.25 and depth < 2:
            content = self.textbox(depth)
        elif body < 0.32:
            content = f"<{self.q('br')}/>"
        else:
            space = self.rng.choice(["", ' xml:space="preserve"'])
            content = f"<{t}{space}>{text}</{t}>"
        rpr = ""
        if self.rng.random() < 0.3:
            tag = self.q("rPr")
            rpr = f"<{tag}><{self.q('b')}/><{self.q('sz')} {self.q('val')}='24'/></{tag}>"
        return f"<{r}{self.attrs()}>{rpr}{self.misc()}{content}</{r}{self.ws()}>"

    def textbox(self, depth: int) -> str:
        inner = "".join(self.paragraph(depth + 1) for _ in range(self.rng.randint(1, 2)))
        return (
            f"<{self.q('pict')}><v:shape><v:textbox>"
            f"<{self.q('txbxContent')}>{inner}</{self.q('txbxContent')}>"
            f"</v:textbox></v:shape></{self.q('pict')}>"
        )

    def paragraph(self, depth: int = 0) -> str:
        self.paragraphs += 1
        if depth:
            self.nested += 1
        rng = self.rng
        prefix = self.w
        decl = ""
        if rng.random() < 0.08:  # re-declare W locally under a fresh prefix
            prefix = rng.choice(["q", "wx", "w2"])
            decl = f' xmlns:{prefix}="{W_NS}"'
        name = self.q("p", prefix)
        if rng.random() < 0.12:
            return f"<{name}{decl}{self.attrs()}{self.ws()}/>"
        ppr = ""
        if rng.random() < 0.4:
            ppr_tag, style = self.q("pPr", prefix), self.q("pStyle", prefix)
            ppr = f'<{ppr_tag}><{style} {self.q("val", prefix)}="Heading1"/></{ppr_tag}>'
        old_w, self.w = self.w, prefix
        runs = "".join(self.run(depth) + self.misc() for _ in range(rng.randint(0, 3)))
        if rng.random() < 0.1:
            runs += f'<x:p xmlns:x="{FOREIGN_NS}">foreign</x:p>'
        self.w = old_w
        return f"<{name}{decl}{self.attrs()}{self.ws()}>{ppr}{runs}</{name}{self.ws()}>"

    def table(self, depth: int) -> str:
        rows = []
        for _ in range(self.rng.randint(1, 2)):
            cells = []
            for _ in range(self.rng.randint(1, 3)):
                inner = "".join(self.block(depth + 1) for _ in range(self.rng.randint(1, 2)))
                if not inner.count(self.q("p")):  # a cell needs a paragraph
                    inner += self.paragraph()
                cells.append(f"<{self.q('tc')}>{inner}</{self.q('tc')}>")
            rows.append(f"<{self.q('tr')}>{''.join(cells)}</{self.q('tr')}>")
        return f"<{self.q('tbl')}><{self.q('tblPr')}/>{''.join(rows)}</{self.q('tbl')}>"

    def sdt(self, depth: int) -> str:
        inner = "".join(self.block(depth + 1) for _ in range(self.rng.randint(1, 2)))
        return (
            f'<{self.q("sdt")}><{self.q("sdtPr")}><{self.q("alias")} {self.q("val")}="c"/>'
            f"</{self.q('sdtPr')}><{self.q('sdtContent')}>{inner}</{self.q('sdtContent')}></{self.q('sdt')}>"
        )

    def block(self, depth: int) -> str:
        r = self.rng.random()
        if depth < 2 and r < 0.12:
            return self.table(depth)
        if depth < 2 and r < 0.2:
            return self.sdt(depth)
        return self.paragraph() + self.misc()


def generate(seed: int) -> bytes:
    """Return a random but well-formed ``word/document.xml`` for ``seed``."""
    rng = random.Random(seed)
    w = rng.choice(["w", "w", "w", "x", "wo", ""])  # "" puts W in the default namespace
    gen = _Gen(rng=rng, w=w)
    decl = rng.choice(
        [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n',
            "<?xml version='1.0' encoding='utf-8'?>",
            "",
        ]
    )
    prolog = rng.choice(["", "<!-- generated -->", "<?mso-application progid='Word.Document'?>\n"])
    ns_names = list(EXTRA_NS)
    rng.shuffle(ns_names)
    decls = [f'xmlns:{p}="{EXTRA_NS[p]}"' for p in ns_names]
    decls.insert(rng.randint(0, len(decls)), f'xmlns{":" + w if w else ""}="{W_NS}"')
    ignorable = rng.choice(['mc:Ignorable="w14 wp14"', "mc:Ignorable='w14'", ""])
    sep = rng.choice([" ", "\n  ", " "])
    root_attrs = sep.join([*decls, ignorable]).rstrip()
    blocks = "".join(gen.block(0) for _ in range(rng.randint(0, 12)))
    sect_tag = gen.q("sectPr")
    sect = rng.choice(["", f"<{sect_tag}><{gen.q('pgSz')} {gen.q('w')}='11906'/></{sect_tag}>"])
    body = gen.q("body")
    return (
        f"{decl}{prolog}<{gen.q('document')} {root_attrs}>"
        f"<{body}>{blocks}{sect}</{body}></{gen.q('document')}>"
    ).encode()
