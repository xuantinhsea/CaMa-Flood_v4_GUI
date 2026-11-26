"""Convert USER_MANUAL.md into USER_MANUAL.docx.

The Markdown file is the single source of the manual; the Word file is generated from it by
``build_installer.bat`` so the two never drift apart. This handles the Markdown the manual
actually uses -- ATX headings, paragraphs (with ``\\`` hard breaks), bullet and numbered lists
with continuation lines and one level of nesting, pipe tables (also indented inside a list
item), fenced code blocks, horizontal rules, and inline ``**bold**``, ``*italic*``,
`` `code` ``, ``[text](url)`` and ``<url>`` -- not the whole CommonMark spec.

Layout: a title page built from everything above the first ``---``, a table of contents (a
real Word TOC field over Heading 1-2), then the body; A4, page numbers in the footer.

    python packaging/md_to_docx.py [USER_MANUAL.md] [-o USER_MANUAL.docx]
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_TAB_ALIGNMENT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]

BODY_FONT = "Calibri"
EAST_ASIA_FONT = "Yu Gothic"          # for the postal mark and any Japanese text
CODE_FONT = "Consolas"
ACCENT = RGBColor(0x1F, 0x4E, 0x79)   # dark blue: headings, table header text
LINK = RGBColor(0x05, 0x63, 0xC1)
HEADER_FILL = "DCE6F1"
CODE_FILL = "F2F2F2"
FOOTER_TEXT = "CaMa_Floodcast 1.0 — User Manual"


# --------------------------------------------------------------------------- low-level helpers
#
# WordprocessingML is order-sensitive: Word rejects a file whose property elements are out of
# schema sequence. These tuples list the elements that must come *after* the one being added.

_PPR_AFTER_SHD = ("w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
                  "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
                  "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
                  "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
                  "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl",
                  "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange")
_PPR_AFTER_PBDR = ("w:shd",) + _PPR_AFTER_SHD
_TCPR_AFTER_SHD = ("w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText", "w:vAlign",
                   "w:hideMark", "w:headers", "w:cellIns", "w:cellDel", "w:cellMerge",
                   "w:tcPrChange")
_TBLPR_AFTER_IND = ("w:tblBorders", "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
                    "w:tblCaption", "w:tblDescription", "w:tblPrChange")
_SETTINGS_AFTER_UPDATEFIELDS = (
    "w:hdrShapeDefaults", "w:footnotePr", "w:endnotePr", "w:compat", "w:docVars", "w:rsids",
    "m:mathPr", "w:attachedSchema", "w:themeFontLang", "w:clrSchemeMapping",
    "w:doNotIncludeSubdocsInStats", "w:doNotAutoCompressPictures", "w:forceUpgrade",
    "w:captions", "w:readModeInkLockDown", "w:smartTagType", "sl:schemaLibrary",
    "w:shapeDefaults", "w:doNotEmbedSmartTags", "w:decimalSymbol", "w:listSeparator")

def set_fonts(rpr_owner, ascii_font: str, east_asia: str = EAST_ASIA_FONT) -> None:
    """Set ascii/hAnsi/eastAsia fonts on a style or run (python-docx only sets ascii/hAnsi)."""
    rpr = rpr_owner.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attr, val in (("w:ascii", ascii_font), ("w:hAnsi", ascii_font),
                      ("w:cs", ascii_font), ("w:eastAsia", east_asia)):
        rfonts.set(qn(attr), val)


def shade(element, fill: str) -> None:
    """Background fill on a paragraph (pPr) or table cell (tcPr)."""
    is_par = hasattr(element, "get_or_add_pPr")
    pr = element.get_or_add_pPr() if is_par else element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pr.insert_element_before(shd, *(_PPR_AFTER_SHD if is_par else _TCPR_AFTER_SHD))


def bottom_border(paragraph, color: str = "1F4E79", size: int = 8) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", str(size)), ("w:space", "4"), ("w:color", color)):
        b.set(qn(k), v)
    bdr.append(b)
    ppr.insert_element_before(bdr, *_PPR_AFTER_PBDR)


def add_field(paragraph, instruction: str, placeholder: str = "") -> None:
    """A complex field (PAGE, TOC ...), shown as ``placeholder`` until Word updates it."""
    def fld(kind):
        r = OxmlElement("w:r")
        c = OxmlElement("w:fldChar")
        c.set(qn("w:fldCharType"), kind)
        if kind == "begin":
            c.set(qn("w:dirty"), "true")
        r.append(c)
        return r

    p = paragraph._p
    p.append(fld("begin"))
    r = OxmlElement("w:r")
    t = OxmlElement("w:instrText")
    t.set(qn("xml:space"), "preserve")
    t.text = f" {instruction} "
    r.append(t)
    p.append(r)
    p.append(fld("separate"))
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = placeholder
    r.append(t)
    p.append(r)
    p.append(fld("end"))


def add_hyperlink(paragraph, url: str, text: str, *, bold=False, italic=False) -> None:
    rid = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    h = OxmlElement("w:hyperlink")
    h.set(qn("r:id"), rid)
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), str(LINK))
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rpr.append(color)
    rpr.append(u)
    if bold:
        rpr.append(OxmlElement("w:b"))
    if italic:
        rpr.append(OxmlElement("w:i"))
    r.append(rpr)
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    h.append(r)
    paragraph._p.append(h)


# --------------------------------------------------------------------------- inline Markdown

_INLINE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<bold>\*\*(?:[^*]|\*(?!\*))+?\*\*)"
    r"|(?P<italic>(?<![\w*])\*(?!\s)[^*]+?(?<!\s)\*(?!\*))"
    r"|(?P<link>\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)\))"
    r"|(?P<auto><(?P<aurl>https?://[^>\s]+)>)"
)


def add_inline(paragraph, text: str, *, bold=False, italic=False, size: Pt | None = None) -> None:
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            _run(paragraph, text[pos:m.start()], bold=bold, italic=italic, size=size)
        if m.group("code"):
            r = _run(paragraph, m.group("code")[1:-1], bold=bold, italic=italic, size=size)
            set_fonts(r, CODE_FONT, CODE_FONT)
            r.font.color.rgb = RGBColor(0x40, 0x40, 0x40)
        elif m.group("bold"):
            add_inline(paragraph, m.group("bold")[2:-2], bold=True, italic=italic, size=size)
        elif m.group("italic"):
            add_inline(paragraph, m.group("italic")[1:-1], bold=bold, italic=True, size=size)
        elif m.group("link"):
            url, label = m.group("lurl"), m.group("ltext")
            if url.startswith("#"):
                # In-document reference (§n): plain text; the section number carries it.
                add_inline(paragraph, label, bold=bold, italic=italic, size=size)
            else:
                add_hyperlink(paragraph, url, re.sub(r"[`*]", "", label), bold=bold, italic=italic)
        elif m.group("auto"):
            add_hyperlink(paragraph, m.group("aurl"), m.group("aurl"), bold=bold, italic=italic)
        pos = m.end()
    if pos < len(text):
        _run(paragraph, text[pos:], bold=bold, italic=italic, size=size)


def _run(paragraph, text, *, bold, italic, size):
    r = paragraph.add_run(text)
    r.bold = bold or None
    r.italic = italic or None
    if size:
        r.font.size = size
    return r


def add_text_with_breaks(paragraph, lines: list[str], **fmt) -> None:
    """Join soft-wrapped lines; a line ending in a backslash is a hard line break."""
    buf: list[str] = []
    for i, line in enumerate(lines):
        hard = line.rstrip().endswith("\\")
        buf.append(line.rstrip().rstrip("\\").strip())
        if hard and i < len(lines) - 1:
            add_inline(paragraph, " ".join(buf), **fmt)
            paragraph.add_run().add_break()
            buf = []
    if buf:
        add_inline(paragraph, " ".join(buf), **fmt)


# --------------------------------------------------------------------------- document setup

def new_document() -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.2)
    sec.top_margin, sec.bottom_margin = Cm(2.2), Cm(2.0)

    st = doc.styles
    normal = st["Normal"]
    normal.font.name, normal.font.size = BODY_FONT, Pt(11)
    set_fonts(normal, BODY_FONT)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.1

    for name, size, before, after in (("Title", 30, 0, 12), ("Heading 1", 17, 18, 8),
                                      ("Heading 2", 13.5, 12, 6), ("Heading 3", 12, 10, 4)):
        s = st[name]
        s.font.name, s.font.size, s.font.bold = BODY_FONT, Pt(size), True
        s.font.color.rgb = ACCENT
        set_fonts(s, BODY_FONT)
        pf = s.paragraph_format
        pf.space_before, pf.space_after, pf.keep_with_next = Pt(before), Pt(after), True

    for name in ("List Bullet", "List Bullet 2"):
        st[name].font.name, st[name].font.size = BODY_FONT, Pt(11)
        st[name].paragraph_format.space_after = Pt(3)

    # Word refreshes the TOC and page fields on open (packaging/update_docx_fields.ps1 does it
    # ahead of time when Word is available, so readers never see the prompt).
    upd = OxmlElement("w:updateFields")
    upd.set(qn("w:val"), "true")
    doc.settings.element.insert_element_before(upd, *_SETTINGS_AFTER_UPDATEFIELDS)
    return doc


def add_footer(section) -> None:
    section.different_first_page_header_footer = True     # clean title page
    p = section.footer.paragraphs[0]
    width = section.page_width - section.left_margin - section.right_margin
    tabs = p.paragraph_format.tab_stops
    # The built-in Footer style has centre/right stops for US Letter; clear them so the page
    # number lands on our own right-aligned stop at the A4 text edge.
    for style_tab in (Cm(8.255), Cm(16.51)):
        tabs.add_tab_stop(style_tab, WD_TAB_ALIGNMENT.CLEAR)
    tabs.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
    r = p.add_run(FOOTER_TEXT + "\t")
    r.font.size, r.font.color.rgb = Pt(9), RGBColor(0x59, 0x59, 0x59)
    add_field(p, "PAGE", "1")
    for run in p.runs:
        run.font.size = Pt(9)


# --------------------------------------------------------------------------- block rendering

def render_table(doc, rows: list[list[str]], indent_cm: float = 0.0) -> None:
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    table = doc.add_table(rows=len(rows), cols=ncols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False

    sec = doc.sections[-1]
    avail = (sec.page_width - sec.left_margin - sec.right_margin) - Cm(indent_cm)
    # Column widths: every column at least as wide as its longest word (so no word is ever
    # broken mid-way), the remaining width shared out by how much text each column holds.
    plain = [[re.sub(r"[`*]", "", r[c]) for c in range(ncols)] for r in rows]
    cm_per_char = 0.19                                      # ~9.5 pt Calibri, generous
    floors = [Cm(0.35 + cm_per_char * max(len(w) for r in plain for w in (r[c].split() or [""])))
              for c in range(ncols)]
    lens = [max(sum(len(r[c]) for r in plain) / len(rows), 1) for c in range(ncols)]
    spare = max(avail - sum(floors), 0)
    widths = [int(f + spare * L / sum(lens)) for f, L in zip(floors, lens)]
    if sum(widths) > avail:                                  # floors alone overflow: scale down
        widths = [int(w * avail / sum(widths)) for w in widths]

    for i, row in enumerate(rows):
        for c, text in enumerate(row):
            cell = table.cell(i, c)
            cell.width = widths[c]
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.space_before = Pt(2)
            add_inline(p, text.strip(), bold=(i == 0), size=Pt(9.5))
            if i == 0:
                shade(cell._tc, HEADER_FILL)
                for r in p.runs:
                    r.font.color.rgb = ACCENT
    # Repeat the header row on every page the table spans.
    trpr = table.rows[0]._tr.get_or_add_trPr()
    hdr = OxmlElement("w:tblHeader")
    hdr.set(qn("w:val"), "true")
    trpr.append(hdr)
    if indent_cm:
        tblpr = table._tbl.tblPr
        ind = OxmlElement("w:tblInd")
        ind.set(qn("w:w"), str(int(Cm(indent_cm).twips)))
        ind.set(qn("w:type"), "dxa")
        tblpr.insert_element_before(ind, *_TBLPR_AFTER_IND)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def render_code(doc, lines: list[str]) -> None:
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.left_indent, pf.space_before, pf.space_after = Cm(0.4), Pt(4), Pt(8)
    pf.line_spacing = 1.0
    shade(p._p, CODE_FILL)
    for i, line in enumerate(lines):
        r = p.add_run(line)
        set_fonts(r, CODE_FONT, CODE_FONT)
        r.font.size = Pt(9)
        if i < len(lines) - 1:
            r.add_break()


def parse_table(lines: list[str]) -> list[list[str]]:
    rows = []
    for ln in lines:
        s = ln.strip()
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", s):
            continue
        cells = [c for c in re.split(r"(?<!\\)\|", s.strip("|"))]
        rows.append([c.replace("\\|", "|") for c in cells])
    return rows


_LIST = re.compile(r"^(?P<ind>\s*)(?P<mark>[-*]|\d+\.)\s+(?P<text>.*)$")


def convert(md_path: Path, out_path: Path) -> None:
    lines = md_path.read_text(encoding="utf-8").splitlines()
    doc = new_document()
    add_footer(doc.sections[0])

    i, n = 0, len(lines)
    in_title = True                 # everything before the first '---' is the title page
    para: list[str] = []

    def flush_para():
        nonlocal para
        if para:
            if in_title:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_after = Pt(14)
                add_text_with_breaks(p, para, size=Pt(13))
            else:
                p = doc.add_paragraph()
                if len(para[0]) - len(para[0].lstrip()) >= 2:     # belongs to a list item
                    p.paragraph_format.left_indent = Cm(0.63)
                add_text_with_breaks(p, para)
            para = []

    while i < n:
        line = lines[i]
        s = line.strip()

        if not s:
            flush_para()
            i += 1
            continue

        if s.startswith("```"):
            flush_para()
            code = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            render_code(doc, code)
            i += 1
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush_para()
            level = len(m.group(1))
            if level == 1:
                for _ in range(6):
                    doc.add_paragraph()
                t = doc.add_paragraph(m.group(2), style="Title")
                t.alignment = WD_ALIGN_PARAGRAPH.CENTER
                bottom_border(t, size=12)
            else:
                h = doc.add_heading(level=level - 1)
                add_inline(h, m.group(2))
            i += 1
            continue

        if re.fullmatch(r"-{3,}", s):
            flush_para()
            if in_title:
                in_title = False
                # Contents page, then the body in a new section (footer numbering continues).
                doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
                c = doc.add_paragraph()
                r = c.add_run("Contents")
                r.bold, r.font.size, r.font.color.rgb = True, Pt(17), ACCENT
                bottom_border(c)
                add_field(doc.add_paragraph(), r'TOC \o "1-2" \h \z \u',
                          "Right-click and choose Update Field to build the table of contents.")
                doc.add_section(WD_SECTION.NEW_PAGE)
                doc.sections[-1].different_first_page_header_footer = False
            i += 1
            continue

        if s.startswith("|"):
            flush_para()
            indent = len(line) - len(line.lstrip())
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            render_table(doc, parse_table(block), indent_cm=0.63 if indent else 0.0)
            continue

        lm = _LIST.match(line)
        if lm:
            flush_para()
            ind = len(lm.group("ind"))
            mark = lm.group("mark")
            item = [lm.group("text")]
            i += 1
            # Continuation lines: indented deeper than the marker, not a new item or table.
            while i < n and lines[i].strip() and not lines[i].strip().startswith("|") \
                    and not _LIST.match(lines[i]) and (len(lines[i]) - len(lines[i].lstrip())) > ind:
                item.append(lines[i])
                i += 1
            nested = ind >= 2
            if mark[0].isdigit():
                p = doc.add_paragraph()
                pf = p.paragraph_format
                pf.left_indent = Cm(0.63 + (0.63 if nested else 0))
                pf.first_line_indent = Cm(-0.63)
                pf.space_after = Pt(3)
                pf.tab_stops.add_tab_stop(pf.left_indent)
                p.add_run(f"{mark}\t")
            else:
                p = doc.add_paragraph(style="List Bullet 2" if nested else "List Bullet")
            add_text_with_breaks(p, item)
            continue

        para.append(line)
        i += 1

    flush_para()
    doc.core_properties.title = "CaMa_Floodcast User Manual"
    doc.core_properties.subject = "CaMa_Floodcast 1.0"
    doc.core_properties.author = "Nguyen Xuan TINH (Ph.D.)"
    doc.core_properties.comments = "© 2026 Nippon Koei Co.,Ltd. Generated from USER_MANUAL.md."
    doc.save(out_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert USER_MANUAL.md to .docx")
    ap.add_argument("source", nargs="?", type=Path, default=ROOT / "USER_MANUAL.md")
    ap.add_argument("-o", "--output", type=Path)
    a = ap.parse_args()
    out = a.output or a.source.with_suffix(".docx")
    convert(a.source, out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
