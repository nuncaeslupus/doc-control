"""Deterministic synthetic .docx corpus (stdlib only).

``generate(kind, seed)`` returns the bytes of a complete WordprocessingML
package. The same ``(kind, seed)`` always yields the same bytes: members are
written in a fixed order with a fixed timestamp, and every random choice comes
from ``random.Random(seed)``.

Kinds cover the structures the engine must read or refuse: styled runs,
headings, tracked insertions/deletions (including a deleted paragraph mark and
a formatting change), comment threads with replies and resolved state across
all five comment parts, simple and complex fields, hyperlinks, tables (nested
too), footnotes, ``w14:paraId`` on every paragraph, an ``mc:Ignorable`` root,
and a Spanish 100k-word novel.
"""

from __future__ import annotations

import random
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "w16cid": "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "w16cex": "http://schemas.microsoft.com/office/word/2018/wordml/cex",
}

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml."
_OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"

# part name -> (content type, relationship type from document.xml)
PART_TYPES: dict[str, tuple[str, str]] = {
    "word/styles.xml": (_CT + "styles+xml", _OD + "styles"),
    "word/settings.xml": (_CT + "settings+xml", _OD + "settings"),
    "word/footnotes.xml": (_CT + "footnotes+xml", _OD + "footnotes"),
    "word/comments.xml": (_CT + "comments+xml", _OD + "comments"),
    "word/commentsExtended.xml": (
        _CT + "commentsExtended+xml",
        "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
    ),
    "word/commentsIds.xml": (
        _CT + "commentsIds+xml",
        "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
    ),
    "word/commentsExtensible.xml": (
        _CT + "commentsExtensible+xml",
        "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
    ),
    "word/people.xml": (
        _CT + "people+xml",
        "http://schemas.microsoft.com/office/2011/relationships/people",
    ),
}
HYPERLINK_REL = _OD + "hyperlink"

KINDS = (
    "plain",
    "headings",
    "revisions",
    "comments",
    "fields",
    "hyperlinks",
    "tables",
    "footnotes",
    "kitchen_sink",
    "novel",
)
FEATURES = ("revisions", "comments", "fields", "hyperlinks", "tables", "footnotes")
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def _words(text: str) -> tuple[str, ...]:
    return tuple(text.split())


AUTHORS = ("Ana Ruiz", "Claude", "Jordi Puig", "Marta López")

WORDS = _words(
    "casa camino noche luz agua puerta ciudad tiempo mano voz silencio carta río mar "
    "cielo tierra ventana calle sombra viento fuego piedra libro mesa madre padre "
    "hermano amigo pueblo plaza iglesia campo árbol flor sangre memoria sueño miedo "
    "esperanza verdad mentira palabra nombre secreto camisa sombrero tren estación "
    "puerto barco reloj espejo llave jardín"
)
VERBS = _words(
    "miraba caminaba esperaba decía pensaba recordaba sabía buscaba abría cerraba "
    "llevaba guardaba escuchaba tocaba callaba sentía volvía dejaba"
)
ADJS = _words(
    "viejo oscuro largo frío blanco antiguo pequeño lejano callado roto nuevo triste "
    "claro hondo quieto húmedo"
)
LINKS = ("y", "pero", "mientras", "aunque", "cuando", "porque", "sin que", "hasta que")
NAMES = ("María", "Tomás", "Lucía", "Andrés", "Elena", "Rafael", "Inés", "Julián")
SPEECH = ("dijo", "preguntó", "murmuró", "respondió", "susurró", "gritó", "añadió")
ENGLISH = _words(
    "The quick report covers the quarterly figures and the open questions raised by "
    "the review board during the last meeting of the year"
)


def _attr(name: str, value: str) -> str:
    return f" {name}={quoteattr(value)}"


def _t(text: str, tag: str = "w:t") -> str:
    return f'<{tag} xml:space="preserve">{escape(text)}</{tag}>'


def _run(text: str, rpr: str = "", tag: str = "w:t") -> str:
    props = f"<w:rPr>{rpr}</w:rPr>" if rpr else ""
    return f"<w:r>{props}{_t(text, tag)}</w:r>"


@dataclass
class _Comment:
    cid: int
    author: str
    date: str
    text: str
    para_id: str
    durable_id: str
    parent_para_id: str | None
    done: bool


@dataclass
class _Builder:
    rng: random.Random
    english: bool = True
    _para_ids: set[str] = field(default_factory=set)
    _durable_ids: set[str] = field(default_factory=set)
    _next_rev: int = 1
    _next_bookmark: int = 0
    comments: list[_Comment] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    hyperlinks: list[tuple[str, str]] = field(default_factory=list)

    # -- ids -----------------------------------------------------------
    def _hex_id(self, used: set[str]) -> str:
        while True:
            value = f"{self.rng.randrange(1, 0x80000000):08X}"
            if value not in used:
                used.add(value)
                return value

    def para_id(self) -> str:
        return self._hex_id(self._para_ids)

    def rev_attrs(self) -> str:
        rid = self._next_rev
        self._next_rev += 1
        author = self.rng.choice(AUTHORS)
        date = (
            f"2026-{self.rng.randint(1, 9):02d}-{self.rng.randint(1, 28):02d}"
            f"T{self.rng.randint(0, 23):02d}:{self.rng.randint(0, 59):02d}:00Z"
        )
        return _attr("w:id", str(rid)) + _attr("w:author", author) + _attr("w:date", date)

    # -- text ----------------------------------------------------------
    def sentence(self, n: int | None = None) -> str:
        n = n or self.rng.randint(6, 14)
        if self.english:
            words = [self.rng.choice(ENGLISH) for _ in range(n)]
        else:
            words = [self.rng.choice(WORDS) for _ in range(n)]
        words[0] = words[0].capitalize()
        return " ".join(words) + "."

    # -- paragraphs ----------------------------------------------------
    def p(self, content: str, ppr: str = "") -> str:
        props = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
        return f'<w:p w14:paraId="{self.para_id()}" w14:textId="77777777">{props}{content}</w:p>'

    def styled_paragraph(self) -> str:
        runs = []
        for _ in range(self.rng.randint(2, 5)):
            rpr = self.rng.choice(
                [
                    "",
                    "<w:b/>",
                    "<w:i/>",
                    "<w:b/><w:i/>",
                    '<w:color w:val="C00000"/>',
                    '<w:sz w:val="28"/><w:szCs w:val="28"/>',
                    '<w:u w:val="single"/>',
                    '<w:vertAlign w:val="superscript"/>',
                ]
            )
            runs.append(_run(self.sentence() + " ", rpr))
        style = self.rng.choice(["", '<w:pStyle w:val="Quote"/>', '<w:jc w:val="both"/>'])
        return self.p("".join(runs), style)

    def heading(self, level: int, text: str) -> str:
        return self.p(_run(text), f'<w:pStyle w:val="Heading{level}"/>')

    # -- features ------------------------------------------------------
    def revisions(self) -> list[str]:
        out = []
        # insertion and deletion inside one paragraph
        out.append(
            self.p(
                _run("Before the change, ")
                + f"<w:del{self.rev_attrs()}>"
                + _run("old wording ", tag="w:delText")
                + "</w:del>"
                + f"<w:ins{self.rev_attrs()}>"
                + _run("new wording ")
                + "</w:ins>"
                + _run(self.sentence())
            )
        )
        # deleted paragraph mark: this paragraph joins the next one
        out.append(
            self.p(
                f"<w:del{self.rev_attrs()}>"
                + _run("A whole deleted paragraph.", tag="w:delText")
                + "</w:del>",
                f"<w:rPr><w:del{self.rev_attrs()}/></w:rPr>",
            )
        )
        # inserted paragraph (inserted paragraph mark plus inserted run)
        out.append(
            self.p(
                f"<w:ins{self.rev_attrs()}>" + _run(self.sentence()) + "</w:ins>",
                f"<w:rPr><w:ins{self.rev_attrs()}/></w:rPr>",
            )
        )
        # formatting change
        out.append(
            self.p(
                _run(
                    self.sentence(),
                    f"<w:b/><w:rPrChange{self.rev_attrs()}><w:rPr/></w:rPrChange>",
                )
            )
        )
        return out

    def comment_thread(self, n_replies: int, done: bool) -> str:
        """Return a paragraph anchoring a root comment and its replies."""
        thread: list[_Comment] = []
        for i in range(n_replies + 1):
            parent = thread[0].para_id if thread else None
            thread.append(
                _Comment(
                    cid=len(self.comments),
                    author=self.rng.choice(AUTHORS),
                    date=f"2026-0{self.rng.randint(1, 9)}-1{self.rng.randint(0, 9)}T10:0{i}:00Z",
                    text=self.sentence(),
                    para_id=self.para_id(),
                    durable_id=self._hex_id(self._durable_ids),
                    parent_para_id=parent,
                    done=done and i == 0,
                )
            )
            self.comments.append(thread[-1])
        starts = "".join(f'<w:commentRangeStart w:id="{c.cid}"/>' for c in thread)
        ends = "".join(f'<w:commentRangeEnd w:id="{c.cid}"/>' for c in thread)
        refs = "".join(
            f'<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
            f'<w:commentReference w:id="{c.cid}"/></w:r>'
            for c in thread
        )
        return self.p(_run("Commented: ") + starts + _run(self.sentence()) + ends + refs)

    def comments_feature(self) -> list[str]:
        return [
            self.comment_thread(0, done=False),
            self.comment_thread(2, done=False),
            self.comment_thread(1, done=True),
        ]

    def fields(self) -> list[str]:
        simple = (
            _run("Page ")
            + '<w:fldSimple w:instr=" PAGE \\* MERGEFORMAT ">'
            + _run("1")
            + "</w:fldSimple>"
            + _run(" of the report.")
        )
        complex_ = (
            _run("Dated ")
            + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            + _run(' DATE \\@ "d MMMM yyyy" ', tag="w:instrText")
            + '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            + _run("12 October 2026")
            + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            + _run(" in the field test.")
        )
        return [self.p(simple), self.p(complex_)]

    def hyperlinks_feature(self) -> list[str]:
        rid = f"rIdLink{len(self.hyperlinks) + 1}"
        self.hyperlinks.append((rid, f"https://example.com/doc/{self.rng.randint(1, 9999)}"))
        bm = self._next_bookmark
        self._next_bookmark += 1
        link_rpr = '<w:rStyle w:val="Hyperlink"/>'
        external = self.p(
            _run("See ")
            + f'<w:hyperlink r:id="{rid}" w:history="1">'
            + _run("the reference page", link_rpr)
            + "</w:hyperlink>"
            + _run(" for details.")
        )
        target = self.p(
            f'<w:bookmarkStart w:id="{bm}" w:name="_Ref{bm}"/>'
            + _run("Bookmarked target paragraph.")
            + f'<w:bookmarkEnd w:id="{bm}"/>'
        )
        internal = self.p(
            _run("Jump to ")
            + f'<w:hyperlink w:anchor="_Ref{bm}" w:history="1">'
            + _run("the bookmark", link_rpr)
            + "</w:hyperlink>"
            + _run(".")
        )
        return [external, target, internal]

    def table(self, rows: int, cols: int, nested: bool = False) -> str:
        width = 9000 // cols
        grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(cols))
        trs = []
        for r in range(rows):
            tcs = []
            for c in range(cols):
                inner = ""
                if nested and r == rows - 1 and c == cols - 1:
                    inner = self.table(2, 2)
                cell = inner + self.p(_run(f"R{r + 1}C{c + 1} {self.sentence(3)}"))
                tcs.append(
                    f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>{cell}</w:tc>'
                )
            trs.append(f"<w:tr>{''.join(tcs)}</w:tr>")
        return (
            '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
            f"</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{''.join(trs)}</w:tbl>"
        )

    def tables_feature(self) -> list[str]:
        return [
            self.table(3, 3),
            self.p(_run("Between the tables.")),
            self.table(2, 2, nested=True),
            self.p(_run("After the nested table.")),
        ]

    def footnote_ref(self, text: str) -> str:
        fid = len(self.footnotes) + 1
        self.footnotes.append(text)
        return (
            '<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>'
            f'<w:footnoteReference w:id="{fid}"/></w:r>'
        )

    def footnotes_feature(self) -> list[str]:
        return [
            self.p(_run(self.sentence()) + self.footnote_ref(self.sentence()) + _run(" More."))
            for _ in range(2)
        ]

    def feature(self, name: str) -> list[str]:
        if name == "revisions":
            return self.revisions()
        if name == "comments":
            return self.comments_feature()
        if name == "fields":
            return self.fields()
        if name == "hyperlinks":
            return self.hyperlinks_feature()
        if name == "tables":
            return self.tables_feature()
        if name == "footnotes":
            return self.footnotes_feature()
        raise ValueError(f"unknown feature: {name}")

    # -- novel ---------------------------------------------------------
    def novel(self, target_words: int = 100_000) -> list[str]:
        rng = self.rng
        out: list[str] = [self.p(_run("La casa del río"), '<w:pStyle w:val="Title"/>')]
        words = 4
        chapter = 0
        while words < target_words:
            chapter += 1
            out.append(self.heading(1, f"Capítulo {chapter}"))
            words += 2
            for scene in range(rng.randint(3, 5)):
                if scene:
                    out.append(self.p(_run("* * *"), '<w:jc w:val="center"/>'))
                    words += 3
                for _ in range(rng.randint(8, 14)):
                    text = self._novel_dialogue() if rng.random() < 0.4 else self._novel_prose()
                    words += text.count(" ") + 1
                    out.append(self.p(_run(text)))
        return out

    def _clause(self) -> str:
        rng = self.rng
        return (
            f"{rng.choice(NAMES)} {rng.choice(VERBS)} "
            f"{rng.choice(('el', 'la', 'un', 'una'))} {rng.choice(WORDS)} {rng.choice(ADJS)}"
        )

    def _novel_prose(self) -> str:
        rng = self.rng
        sentences = []
        for _ in range(rng.randint(3, 6)):
            s = self._clause()
            for _ in range(rng.randint(1, 3)):
                s += f" {rng.choice(LINKS)} {self._clause()}"
            if rng.random() < 0.2:
                s += f", y pensó en «{rng.choice(WORDS)} {rng.choice(ADJS)}»"
            sentences.append(s + ".")
        return " ".join(sentences)

    def _novel_dialogue(self) -> str:
        rng = self.rng
        said = f"{rng.choice(SPEECH)} {rng.choice(NAMES)}"
        first = f"{rng.choice(WORDS).capitalize()} {rng.choice(ADJS)}, {self._clause().lower()}"
        if rng.random() < 0.5:
            return f"—¿{first}? —{said}."
        return f"—{first} —{said}—. {self._clause()}."


# -- package parts -----------------------------------------------------


def _root(tag: str, prefixes: tuple[str, ...], ignorable: bool = True) -> str:
    decls = "".join(f" xmlns:{p}={quoteattr(NS[p])}" for p in prefixes)
    ign = ' mc:Ignorable="w14 w15 wp14"' if ignorable else ""
    return f"<{tag}{decls}{ign}>"


_MAIN = ("w", "r", "mc", "w14", "w15", "wp14")


def _document(b: _Builder, blocks: list[str]) -> bytes:
    sect = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
    )
    xml = (
        XML_DECL
        + _root("w:document", _MAIN)
        + "<w:body>"
        + "".join(blocks)
        + sect
        + "</w:body></w:document>"
    )
    return xml.encode("utf-8")


def _styles() -> bytes:
    def para(sid: str, name: str, ppr: str = "", rpr: str = "", based: str = "Normal") -> str:
        base = f'<w:basedOn w:val="{based}"/>' if based else ""
        return (
            f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>{base}'
            f"<w:qFormat/><w:pPr>{ppr}</w:pPr><w:rPr>{rpr}</w:rPr></w:style>"
        )

    def char(sid: str, name: str, rpr: str) -> str:
        return (
            f'<w:style w:type="character" w:styleId="{sid}"><w:name w:val="{name}"/>'
            f"<w:rPr>{rpr}</w:rPr></w:style>"
        )

    styles = [
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
        '<w:qFormat/><w:rPr><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style>',
        '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
        '<w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/></w:style>',
        para("Title", "Title", "", '<w:sz w:val="56"/><w:szCs w:val="56"/>'),
        *(
            para(
                f"Heading{n}",
                f"heading {n}",
                f'<w:keepNext/><w:outlineLvl w:val="{n - 1}"/>',
                f'<w:b/><w:sz w:val="{36 - 4 * n}"/><w:szCs w:val="{36 - 4 * n}"/>',
            )
            for n in (1, 2, 3)
        ),
        para("Quote", "Quote", '<w:ind w:left="864" w:right="864"/>', "<w:i/>"),
        para("FootnoteText", "footnote text", "", '<w:sz w:val="20"/><w:szCs w:val="20"/>'),
        para("CommentText", "annotation text", "", '<w:sz w:val="20"/><w:szCs w:val="20"/>'),
        char("Hyperlink", "Hyperlink", '<w:color w:val="0563C1"/><w:u w:val="single"/>'),
        char("FootnoteReference", "footnote reference", '<w:vertAlign w:val="superscript"/>'),
        char("CommentReference", "annotation reference", '<w:sz w:val="16"/><w:szCs w:val="16"/>'),
        '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>'
        "<w:tblPr><w:tblBorders>"
        + "".join(
            f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
            for side in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + "</w:tblBorders></w:tblPr></w:style>",
    ]
    xml = XML_DECL + _root("w:styles", _MAIN) + "".join(styles) + "</w:styles>"
    return xml.encode("utf-8")


def _settings() -> bytes:
    xml = (
        XML_DECL
        + _root("w:settings", _MAIN)
        + '<w:defaultTabStop w:val="708"/><w:characterSpacingControl w:val="doNotCompress"/>'
        + '<w:compat><w:compatSetting w:name="compatibilityMode" '
        + 'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
        + "</w:settings>"
    )
    return xml.encode("utf-8")


def _footnotes(b: _Builder) -> bytes:
    parts = [
        '<w:footnote w:type="separator" w:id="-1">'
        + b.p("<w:r><w:separator/></w:r>")
        + "</w:footnote>",
        '<w:footnote w:type="continuationSeparator" w:id="0">'
        + b.p("<w:r><w:continuationSeparator/></w:r>")
        + "</w:footnote>",
    ]
    for i, text in enumerate(b.footnotes, start=1):
        body = b.p(
            '<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteRef/></w:r>'
            + _run(" " + text),
            '<w:pStyle w:val="FootnoteText"/>',
        )
        parts.append(f'<w:footnote w:id="{i}">{body}</w:footnote>')
    xml = XML_DECL + _root("w:footnotes", _MAIN) + "".join(parts) + "</w:footnotes>"
    return xml.encode("utf-8")


def _initials(author: str) -> str:
    return "".join(word[0] for word in author.split())


def _comment_parts(b: _Builder) -> dict[str, bytes]:
    comments = []
    ext = []
    ids = []
    cex = []
    for c in b.comments:
        para = (
            f'<w:p w14:paraId="{c.para_id}" w14:textId="77777777">'
            '<w:pPr><w:pStyle w:val="CommentText"/></w:pPr>'
            '<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:annotationRef/></w:r>'
            f"{_run(c.text)}</w:p>"
        )
        comments.append(
            f'<w:comment w:id="{c.cid}"'
            + _attr("w:author", c.author)
            + _attr("w:date", c.date)
            + _attr("w:initials", _initials(c.author))
            + f">{para}</w:comment>"
        )
        parent = f' w15:paraIdParent="{c.parent_para_id}"' if c.parent_para_id else ""
        ext.append(f'<w15:commentEx w15:paraId="{c.para_id}"{parent} w15:done="{int(c.done)}"/>')
        ids.append(
            f'<w16cid:commentId w16cid:paraId="{c.para_id}" w16cid:durableId="{c.durable_id}"/>'
        )
        cex.append(
            f'<w16cex:commentExtensible w16cex:durableId="{c.durable_id}" '
            f'w16cex:dateUtc="{c.date}"/>'
        )
    people = "".join(
        "<w15:person"
        + _attr("w15:author", a)
        + '><w15:presenceInfo w15:providerId="None"'
        + _attr("w15:userId", a)
        + "/></w15:person>"
        for a in sorted({c.author for c in b.comments})
    )
    return {
        "word/comments.xml": (
            XML_DECL + _root("w:comments", _MAIN) + "".join(comments) + "</w:comments>"
        ).encode("utf-8"),
        "word/commentsExtended.xml": (
            XML_DECL + _root("w15:commentsEx", _MAIN) + "".join(ext) + "</w15:commentsEx>"
        ).encode("utf-8"),
        "word/commentsIds.xml": (
            XML_DECL
            + _root("w16cid:commentsIds", ("mc", "w14", "w15", "wp14", "w16cid"))
            + "".join(ids)
            + "</w16cid:commentsIds>"
        ).encode("utf-8"),
        "word/commentsExtensible.xml": (
            XML_DECL
            + _root("w16cex:commentsExtensible", ("mc", "w14", "w15", "wp14", "w16cex"))
            + "".join(cex)
            + "</w16cex:commentsExtensible>"
        ).encode("utf-8"),
        "word/people.xml": (
            XML_DECL + _root("w15:people", _MAIN) + people + "</w15:people>"
        ).encode("utf-8"),
    }


def _content_types(parts: list[str]) -> bytes:
    overrides = [
        ("/word/document.xml", _CT + "document.main+xml"),
        ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
        (
            "/docProps/app.xml",
            "application/vnd.openxmlformats-officedocument.extended-properties+xml",
        ),
    ]
    overrides += [("/" + name, PART_TYPES[name][0]) for name in parts]
    xml = (
        XML_DECL
        + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        + '<Default Extension="rels" '
        + 'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        + '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(
            f'<Override PartName="{name}" ContentType="{ctype}"/>' for name, ctype in overrides
        )
        + "</Types>"
    )
    return xml.encode("utf-8")


def _rels(entries: list[tuple[str, str, str, bool]]) -> bytes:
    rows = "".join(
        f'<Relationship Id="{rid}" Type="{rtype}" Target={quoteattr(target)}'
        + (' TargetMode="External"' if external else "")
        + "/>"
        for rid, rtype, target, external in entries
    )
    xml = (
        XML_DECL
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + rows
        + "</Relationships>"
    )
    return xml.encode("utf-8")


def _package_rels() -> bytes:
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
    return _rels(
        [
            ("rId1", _OD + "officeDocument", "word/document.xml", False),
            ("rId2", pkg, "docProps/core.xml", False),
            ("rId3", _OD + "extended-properties", "docProps/app.xml", False),
        ]
    )


def _core(kind: str, seed: int) -> bytes:
    xml = (
        XML_DECL
        + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        + 'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        + 'xmlns:dcterms="http://purl.org/dc/terms/" '
        + 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + f"<dc:title>synthetic {kind} {seed}</dc:title><dc:creator>doc-control corpus</dc:creator>"
        + '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
        + "</cp:coreProperties>"
    )
    return xml.encode("utf-8")


def _app() -> bytes:
    xml = (
        XML_DECL
        + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        + 'extended-properties"><Application>doc-control corpus</Application></Properties>'
    )
    return xml.encode("utf-8")


def generate_parts(kind: str, seed: int) -> dict[str, bytes]:
    """Return the package members of a synthetic document, in write order."""
    if kind not in KINDS:
        raise ValueError(f"unknown corpus kind {kind!r}; expected one of {', '.join(KINDS)}")
    rng = random.Random(f"{kind}:{seed}")
    b = _Builder(rng, english=kind != "novel")
    blocks: list[str] = []
    if kind == "novel":
        blocks = b.novel()
    else:
        blocks.append(b.heading(1, f"Synthetic {kind} document {seed}"))
        blocks += [b.styled_paragraph() for _ in range(rng.randint(2, 4))]
        if kind == "headings":
            for n in (2, 3, 2):
                blocks.append(b.heading(n, b.sentence(4)))
                blocks += [b.styled_paragraph() for _ in range(2)]
        features = list(FEATURES) if kind == "kitchen_sink" else [kind]
        for name in features:
            if name in FEATURES:
                blocks.append(b.heading(2, f"Section {name}"))
                blocks += b.feature(name)
                blocks.append(b.styled_paragraph())

    extra: dict[str, bytes] = {
        "word/styles.xml": _styles(),
        "word/settings.xml": _settings(),
    }
    if b.footnotes:
        extra["word/footnotes.xml"] = _footnotes(b)
    if b.comments:
        extra.update(_comment_parts(b))
    document = _document(b, blocks)

    doc_rels = [
        (f"rId{i}", PART_TYPES[name][1], name.removeprefix("word/"), False)
        for i, name in enumerate(extra, start=1)
    ]
    doc_rels += [(rid, HYPERLINK_REL, url, True) for rid, url in b.hyperlinks]

    parts: dict[str, bytes] = {
        "[Content_Types].xml": _content_types(list(extra)),
        "_rels/.rels": _package_rels(),
        "docProps/core.xml": _core(kind, seed),
        "docProps/app.xml": _app(),
        "word/document.xml": document,
        "word/_rels/document.xml.rels": _rels(doc_rels),
    }
    parts.update(extra)
    return parts


def generate(kind: str, seed: int) -> bytes:
    """Return the bytes of a deterministic synthetic .docx of ``kind``."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in generate_parts(kind, seed).items():
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()
