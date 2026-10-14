"""On/off flags of ``word/settings.xml`` (``w:evenAndOddHeaders``, ``w:mirrorMargins``),
changed on the bytes so every other byte of the part stays.

Word cannot track a settings change, so callers record it as an untracked layout change
(LAYOUT D-L2). :func:`top_level` finds the byte span of each child of ``w:settings``;
:func:`set_flag` inserts, rewrites or removes one flag element in schema position;
:func:`without` strips the named children, which is how the layout footprint checks that
nothing else in the part moved.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from doc_control.errors import DocControlError, ErrorCode

SETTINGS_PART = "word/settings.xml"
FLAGS = ("evenAndOddHeaders", "mirrorMargins", "updateFields")

# CT_Settings child order (ECMA-376 17.15.1.78), enough of it to place the flags.
_NAMES = """
writeProtection view zoom removePersonalInformation removeDateAndTime doNotDisplayPageBoundaries
displayBackgroundShape printPostScriptOverText printFractionalCharacterWidth printFormsData
embedTrueTypeFonts embedSystemFonts saveSubsetFonts saveFormsData mirrorMargins
alignBordersAndEdges bordersDoNotSurroundHeader bordersDoNotSurroundFooter gutterAtTop
hideSpellingErrors hideGrammaticalErrors activeWritingStyle proofState formsDesign
attachedTemplate linkStyles stylePaneFormatFilter stylePaneSortMethod documentType mailMerge
revisionView trackRevisions doNotTrackMoves doNotTrackFormatting documentProtection
autoFormatOverride styleLockTheme styleLockQFSet defaultTabStop autoHyphenation
consecutiveHyphenLimit hyphenationZone doNotHyphenateCaps showEnvelope summaryLength
clickAndTypeStyle defaultTableStyle evenAndOddHeaders bookFoldRevPrinting bookFoldPrinting
bookFoldPrintingSheets drawingGridHorizontalSpacing drawingGridVerticalSpacing
displayHorizontalDrawingGridEvery displayVerticalDrawingGridEvery
doNotUseMarginsForDrawingGridOrigin drawingGridHorizontalOrigin drawingGridVerticalOrigin
doNotShadeFormData noPunctuationKerning characterSpacingControl printTwoOnOne
strictFirstAndLastChars noLineBreaksAfter noLineBreaksBefore savePreviewPicture
doNotValidateAgainstSchema saveInvalidXml ignoreMixedContent alwaysShowPlaceholderText
doNotDemarcateInvalidXml saveXmlDataOnly useXSLTWhenSaving saveThroughXslt showXMLTags
alwaysMergeEmptyNamespace updateFields hdrShapeDefaults footnotePr endnotePr compat docVars
rsids mathPr attachedSchema themeFontLang clrSchemeMapping doNotIncludeSubdocsInStats
doNotAutoCompressPictures forceUpgrade captions readModeInkLockDown smartTagType
schemaLibrary shapeDefaults doNotEmbedSmartTags decimalSymbol listSeparator
"""
_RANK = {name: i for i, name in enumerate(_NAMES.split())}

_TOKEN = re.compile(
    rb"<!--.*?-->|<\?.*?\?>|<!\[CDATA\[.*?\]\]>|<!DOCTYPE(?:[^>\[]|\[.*?\])*>"
    rb"|</([^\s>]+)\s*>"
    rb"|<([^\s/>]+)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>",
    re.S,
)
_VAL = re.compile(rb"[\s:]val\s*=\s*[\"']([^\"']*)[\"']")
_OFF = frozenset({"0", "false", "off"})


def _malformed(why: str) -> DocControlError:
    return DocControlError(ErrorCode.NOT_A_DOCX, f"{SETTINGS_PART}: {why}")


def top_level(data: bytes) -> list[tuple[str, int, int]]:
    """``(local name, start, end)`` of each child of the root element, in order."""
    out: list[tuple[str, int, int]] = []
    depth = 0
    start = 0
    name = ""
    for m in _TOKEN.finditer(data):
        if m.group(1) is not None:
            depth -= 1
            if depth == 1:
                out.append((name, start, m.end()))
            elif depth < 0:
                raise _malformed("unbalanced end tag")
        elif m.group(2) is not None:
            if depth == 1:
                start = m.start()
                name = m.group(2).decode().rpartition(":")[2]
                if m.group(4):
                    out.append((name, start, m.end()))
            if not m.group(4):
                depth += 1
    if depth != 0:
        raise _malformed("unbalanced start tag")
    return out


def _root_prefix(data: bytes) -> str:
    """The prefix WordprocessingML elements carry in this part ('' for the default)."""
    for m in _TOKEN.finditer(data):
        if m.group(2) is not None:
            return m.group(2).decode().rpartition(":")[0]
    raise _malformed("no root element")


def is_on(element: bytes) -> bool:
    """Whether a flag element's bytes mean "on" (``w:val`` absent, or not 0/false/off)."""
    m = _VAL.search(element)
    return m is None or m.group(1).decode().lower() not in _OFF


def read_flag(data: bytes, name: str) -> bool:
    for local, start, end in top_level(data):
        if local == name:
            return is_on(data[start:end])
    return False


def _element(data: bytes, name: str) -> bytes:
    prefix = _root_prefix(data)
    return f"<{prefix + ':' if prefix else ''}{name}/>".encode()


def set_flag(data: bytes, name: str, on: bool) -> bytes:
    """``data`` with the flag ``name`` on or off. On is the bare element; off removes it.

    Returns ``data`` itself when nothing changes."""
    if name not in _RANK:
        raise ValueError(name)
    try:
        ET.fromstring(data)
    except ET.ParseError as exc:
        raise _malformed(str(exc)) from exc
    children = top_level(data)
    for local, start, end in children:
        if local != name:
            continue
        if is_on(data[start:end]) == on:
            return data
        return data[:start] + (_element(data, name) if on else b"") + data[end:]
    if not on:
        return data
    root = next((m for m in _TOKEN.finditer(data) if m.group(2) is not None), None)
    if root is not None and root.group(4):  # `<w:settings/>`: open it up
        tag = root.group(2) + root.group(3)
        return (
            data[: root.start()]
            + b"<"
            + tag
            + b">"
            + _element(data, name)
            + (b"</" + root.group(2) + b">")
            + data[root.end() :]
        )
    at = data.rfind(b"</")
    for local, start, _ in children:
        if _RANK.get(local, -1) > _RANK[name]:
            at = start
            break
    return data[:at] + _element(data, name) + data[at:]


def without(data: bytes, names: frozenset[str]) -> bytes:
    """``data`` with the children called ``names`` cut out."""
    out = bytearray()
    pos = 0
    for local, start, end in top_level(data):
        if local in names:
            out += data[pos:start]
            pos = end
    out += data[pos:]
    return bytes(out)
