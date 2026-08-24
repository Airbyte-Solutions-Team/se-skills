"""In-process PDF rendering for hosted exports.

The local desktop exporter drives headless Chrome through a subprocess and a
temporary directory (`webapp/pdf_render.py`). Neither is acceptable in hosted
mode: the hosted API must not spawn processes, must not depend on a browser
being installed on the box, and must not write export artifacts to the local
filesystem. This module renders in-process with ReportLab instead, into memory.

Fidelity comes from sharing the source semantics rather than the renderer: the
approved Markdown goes through the same `md_render.markdown_to_body_html`
sanitizer the reader and the local exporters use, and only that sanitized
fragment is translated into ReportLab flowables. The translation is an
allowlist, so anything the sanitizer did not remove is still inert here:

  * text is escaped before it reaches ReportLab's own inline markup parser, so
    raw HTML that survived as text stays text;
  * only a fixed set of inline tags becomes markup (bold, italic, code, links,
    line breaks, sub/superscript, strikethrough);
  * link targets are re-checked against an http/https/mailto/tel allowlist, so a
    `javascript:` URL renders as plain text with no clickable target;
  * unknown tags contribute their text content and nothing else.

Fonts
-----
ReportLab's built-in Type 1 fonts only cover CP1252, which would silently turn a
checkmark or CJK text into the wrong glyphs. So a DejaVu TrueType family is
registered when the host provides one (`fonts-dejavu-core` on Debian/Ubuntu
images, the same paths on Homebrew/macOS boxes), covering Latin, Greek, Cyrillic
and the common symbol ranges.

Whichever family ends up active, the renderer never rewrites reviewed text to fit
it: every run of text is checked against the coverage of the face that will
actually draw it, and a character that face cannot represent raises
`PdfFontCoverageError`. Per-face matters because the DejaVu cmaps differ between
regular, bold, oblique, bold-oblique, and mono, so a character present in the
regular face can be missing from the oblique one; checking one face would let
italic or heading text reach the page as a missing glyph. A PDF export therefore
either carries the approved characters or fails; it never substitutes them.
Markdown export always carries the exact approved bytes, so a document outside
the font's coverage (CJK, emoji) is still exportable through Markdown.

The DejaVu family is a hosted runtime requirement for any content beyond CP1252
(`fonts-dejavu-core` on Debian/Ubuntu). `unicode_font_status()` reports what the
running process resolved so a preflight can assert it; wiring that preflight into
live provisioning belongs to Slice 5B2B2.

Bounds
------
Rendering work is bounded by the Markdown byte ceiling, an element-count ceiling,
a nesting-depth ceiling raised before CPython's own recursion limit, and the
table-column ceiling that switches a wide table to a stacked layout.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import md_render

# Bounded work: the same ceiling the correction editor enforces, plus a node
# ceiling so a pathological document cannot turn into unbounded rendering work.
MAX_MARKDOWN_BYTES = 400_000
_MAX_NODES = 40_000
_MAX_TABLE_COLUMNS = 12

# Nesting ceiling for the recursive translation. Sanitized Markdown nests far
# below this; hitting it has to be a bounded error rather than a `RecursionError`.
_MAX_DEPTH = 24

# The PDF carries no document-level metadata derived from customer content: the
# reviewed body is the artifact, and the title/author fields stay neutral.
_DOC_TITLE = "Reviewed output"
_PRODUCER = "se-skills hosted export"

_PAGE_WIDTH, _PAGE_HEIGHT = LETTER
_MARGIN = 16 * mm
_TOP_MARGIN = 18 * mm
_FRAME_WIDTH = _PAGE_WIDTH - 2 * _MARGIN

_VOID_TAGS = frozenset({"br", "hr", "img", "input", "col", "meta", "link"})
_BLOCK_CONTAINERS = frozenset({"div", "section", "article", "details", "summary", "root"})
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_INLINE_MARKUP = {
    "strong": "b",
    "b": "b",
    "em": "i",
    "i": "i",
    "u": "u",
    "s": "strike",
    "del": "strike",
    "strike": "strike",
    "sup": "super",
    "sub": "sub",
}
_ALLOWED_LINK_SCHEMES = ("http://", "https://", "mailto:", "tel:")
_BULLETS = ("\u2022", "\u25e6", "\u2023")

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/local/share/fonts/dejavu",
    "/opt/homebrew/share/fonts",
    "/Library/Fonts",
)
_FONT_FILES = {
    "SEBody": "DejaVuSans.ttf",
    "SEBody-Bold": "DejaVuSans-Bold.ttf",
    "SEBody-Italic": "DejaVuSans-Oblique.ttf",
    "SEBody-BoldItalic": "DejaVuSans-BoldOblique.ttf",
    "SEMono": "DejaVuSansMono.ttf",
}


class PdfRenderError(Exception):
    """The approved Markdown could not be rendered within hosted bounds."""


class PdfFontCoverageError(PdfRenderError):
    """The active font cannot represent characters the approved text contains."""


@dataclass(frozen=True)
class FontStatus:
    """What font family this process resolved, for deployment preflight."""

    unicode_fonts: bool
    body_font: str
    mono_font: str
    searched_dirs: tuple[str, ...]


def _register_unicode_fonts() -> dict[str, frozenset[int]]:
    """Register the DejaVu family when the host provides it, with its coverage.

    The returned mapping is empty when no usable family was found, which leaves
    the renderer on ReportLab's built-in CP1252 fonts.
    """
    for directory in _FONT_DIRS:
        base = Path(directory)
        if not all((base / name).is_file() for name in _FONT_FILES.values()):
            continue
        coverage: dict[str, frozenset[int]] = {}
        try:
            for font_name, file_name in _FONT_FILES.items():
                font = TTFont(font_name, str(base / file_name))
                pdfmetrics.registerFont(font)
                coverage[font_name] = frozenset(font.face.charToGlyph)
        except Exception:  # noqa: BLE001 - unreadable font files are not fatal
            return {}
        pdfmetrics.registerFontFamily(
            "SEBody",
            normal="SEBody",
            bold="SEBody-Bold",
            italic="SEBody-Italic",
            boldItalic="SEBody-BoldItalic",
        )
        return coverage
    return {}


_FONT_COVERAGE = _register_unicode_fonts()
UNICODE_FONTS = bool(_FONT_COVERAGE)
_BODY_FONT = "SEBody" if UNICODE_FONTS else "Helvetica"
_BOLD_FONT = "SEBody-Bold" if UNICODE_FONTS else "Helvetica-Bold"
_ITALIC_FONT = "SEBody-Italic" if UNICODE_FONTS else "Helvetica-Oblique"
_BOLD_ITALIC_FONT = "SEBody-BoldItalic" if UNICODE_FONTS else "Helvetica-BoldOblique"
_MONO_FONT = "SEMono" if UNICODE_FONTS else "Courier"


@dataclass(frozen=True)
class _Face:
    """Which registered face will actually draw a run of text.

    Coverage differs between the faces of one family, so a run has to be checked
    against the face ReportLab will draw it with rather than against the regular
    face.
    """

    bold: bool = False
    italic: bool = False
    mono: bool = False

    def styled(self, markup: str) -> "_Face":
        if markup == "b":
            return _Face(True, self.italic, self.mono)
        if markup == "i":
            return _Face(self.bold, True, self.mono)
        return self


_BODY_FACE = _Face()
_BOLD_FACE = _Face(bold=True)
_MONO_FACE = _Face(mono=True)


def _font_for(face: _Face) -> str:
    if face.mono:
        return _MONO_FONT
    if face.bold and face.italic:
        return _BOLD_ITALIC_FONT
    if face.bold:
        return _BOLD_FONT
    if face.italic:
        return _ITALIC_FONT
    return _BODY_FONT


def unicode_font_status() -> FontStatus:
    """Report the font family this process resolved, for deployment preflight."""
    return FontStatus(
        unicode_fonts=UNICODE_FONTS,
        body_font=_BODY_FONT,
        mono_font=_MONO_FONT,
        searched_dirs=_FONT_DIRS,
    )


def _unrepresentable(text: str, coverage: frozenset[int] | None) -> int:
    """Count characters the face cannot draw.

    Whitespace is exempt: layout consumes it rather than drawing a glyph. A
    `None` coverage means a built-in Type 1 face, whose encoding is CP1252.
    """
    missing = 0
    for char in text:
        if char.isspace():
            continue
        if coverage is None:
            try:
                char.encode("cp1252")
            except UnicodeEncodeError:
                missing += 1
            continue
        if ord(char) not in coverage:
            missing += 1
    return missing


def _checked(text: str, face: _Face) -> str:
    """Return reviewed text unchanged, or fail closed rather than mutate it."""
    missing = _unrepresentable(text, _FONT_COVERAGE.get(_font_for(face)))
    if missing:
        raise PdfFontCoverageError(
            f"document contains {missing} character(s) the export font cannot "
            "represent; export as Markdown to keep the exact text"
        )
    return text


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["_Node | str"] = field(default_factory=list)


class _FragmentParser(HTMLParser):
    """Build a small tree from the sanitized body fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self._stack: list[_Node] = [self.root]
        self._nodes = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._nodes += 1
        if self._nodes > _MAX_NODES:
            raise PdfRenderError("document has too many elements to render")
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._nodes += 1
        if self._nodes > _MAX_NODES:
            raise PdfRenderError("document has too many elements to render")
        self._stack[-1].children.append(_Node(tag, {name: value or "" for name, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def _escape(text: str, face: _Face) -> str:
    return html.escape(_checked(text, face), quote=True)


def _image_text(node: _Node) -> str:
    """Describe an image by its reviewed text only.

    The renderer never fetches an image: `src` may point anywhere, and an export
    must not make a network request. The alt and title text is reviewed content
    though, so it is carried into the PDF inertly instead of being dropped, and
    an image with neither still leaves a marker so the reader sees the omission.
    """
    alt = " ".join(node.attrs.get("alt", "").split())
    title = " ".join(node.attrs.get("title", "").split())
    if alt and title and title != alt:
        return f"[image: {alt} — {title}]"
    return f"[image: {alt or title}]" if alt or title else "[image]"


def _inline(node: _Node, face: _Face = _BODY_FACE, depth: int = 0) -> str:
    """Render inline content as escaped text plus allowlisted ReportLab markup.

    `face` is the face the enclosing style draws with, so every run is coverage
    checked against the font that will actually render it.
    """
    if depth > _MAX_DEPTH:
        raise PdfRenderError("document nests too deeply to render")
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(_escape(child, face))
            continue
        tag = child.tag
        if tag == "br":
            parts.append("<br/>")
            continue
        if tag == "img":
            parts.append(_escape(_image_text(child), face))
            continue
        if tag in _VOID_TAGS:
            continue
        if tag == "code":
            mono = _Face(face.bold, face.italic, True)
            inner = _inline(child, mono, depth + 1)
            parts.append(f'<font face="{_font_for(mono)}">{inner}</font>')
            continue
        if tag == "a":
            inner = _inline(child, face, depth + 1)
            href = child.attrs.get("href", "").strip()
            if href.lower().startswith(_ALLOWED_LINK_SCHEMES):
                parts.append(f'<link href="{_escape(href, face)}" color="#1558b0">{inner}</link>')
            else:
                parts.append(inner)
            continue
        if tag in _INLINE_MARKUP:
            markup = _INLINE_MARKUP[tag]
            inner = _inline(child, face.styled(markup), depth + 1)
            parts.append(f"<{markup}>{inner}</{markup}>")
            continue
        parts.append(_inline(child, face, depth + 1))
    return "".join(parts)


def _plain_text(node: _Node, depth: int = 0) -> str:
    if depth > _MAX_DEPTH:
        raise PdfRenderError("document nests too deeply to render")
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "img":
            parts.append(_image_text(child))
        else:
            parts.append(_plain_text(child, depth + 1))
    return "".join(parts)


def _styles() -> dict[str, ParagraphStyle]:
    body = ParagraphStyle(
        "body",
        fontName=_BODY_FONT,
        fontSize=9.5,
        leading=13.5,
        spaceAfter=6,
        textColor=colors.HexColor("#1a1a1a"),
    )
    styles = {
        "body": body,
        "h1": ParagraphStyle(
            "h1", parent=body, fontName=_BOLD_FONT, fontSize=18, leading=22,
            spaceBefore=0, spaceAfter=10, textColor=colors.HexColor("#111111"),
        ),
        "h2": ParagraphStyle(
            "h2", parent=body, fontName=_BOLD_FONT, fontSize=13.5, leading=17,
            spaceBefore=14, spaceAfter=6, textColor=colors.HexColor("#111111"),
        ),
        "h3": ParagraphStyle(
            "h3", parent=body, fontName=_BOLD_FONT, fontSize=11.5, leading=15,
            spaceBefore=10, spaceAfter=4,
        ),
        "h4": ParagraphStyle(
            "h4", parent=body, fontName=_BOLD_FONT, fontSize=10, leading=13.5,
            spaceBefore=8, spaceAfter=3,
        ),
        "quote": ParagraphStyle(
            "quote", parent=body, leftIndent=10, textColor=colors.HexColor("#333333"),
        ),
        "code": ParagraphStyle(
            "code", parent=body, fontName=_MONO_FONT, fontSize=8.5, leading=11,
        ),
        "th": ParagraphStyle(
            "th", parent=body, fontName=_BOLD_FONT, fontSize=8.5, leading=11,
            spaceAfter=0, textColor=colors.white,
        ),
        "td": ParagraphStyle("td", parent=body, fontSize=8.5, leading=11, spaceAfter=0),
    }
    styles["h5"] = styles["h4"]
    styles["h6"] = styles["h4"]
    return styles


def _caption_flowables(table: _Node, styles: dict[str, ParagraphStyle]) -> list:
    """Carry `<caption>` text into the PDF instead of dropping it."""
    style = ParagraphStyle(
        "caption", parent=styles["td"], spaceBefore=4, spaceAfter=3,
        textColor=colors.HexColor("#333333"),
    )
    flow: list = []
    for child in table.children:
        if not isinstance(child, _Node) or child.tag != "caption":
            continue
        text = _inline(child, _BOLD_FACE).strip()
        if text:
            flow.append(Paragraph(f"<b>{text}</b>", style))
    return flow


def _rows(table: _Node) -> list[_Node]:
    rows: list[_Node] = []
    for child in table.children:
        if isinstance(child, str):
            continue
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in ("thead", "tbody", "tfoot"):
            rows.extend(row for row in child.children if isinstance(row, _Node) and row.tag == "tr")
    return rows


def _stacked_table_flowables(
    cell_rows: list[list[_Node]], header: bool, styles: dict[str, ParagraphStyle]
) -> list:
    """Render a table too wide for the page without dropping any cell.

    A grid wider than `_MAX_TABLE_COLUMNS` cannot stay legible across a letter
    page, and silently trimming columns would remove reviewed evidence from the
    export. So each row becomes a labelled block instead: every cell keeps its
    text, in source order, under its header label when the table has one.
    """
    label_style = ParagraphStyle("cell", parent=styles["td"], leftIndent=10, spaceAfter=2)
    row_style = ParagraphStyle("cellrow", parent=styles["td"], spaceBefore=4, spaceAfter=2)
    headers = [_inline(cell, _BOLD_FACE).strip() for cell in cell_rows[0]] if header else []
    body_rows = cell_rows[1:] if header else cell_rows
    flow: list = []
    if headers:
        flow.append(Paragraph("<b>Columns</b>", row_style))
        for position, label in enumerate(headers, start=1):
            flow.append(Paragraph(f"{position}. {label or '&nbsp;'}", label_style))
    for index, cells in enumerate(body_rows, start=1):
        flow.append(Paragraph(f"<b>Row {index}</b>", row_style))
        for position, cell in enumerate(cells):
            label = headers[position] if position < len(headers) else ""
            label = label or f"Column {position + 1}"
            value = _inline(cell).strip() or "&nbsp;"
            flow.append(Paragraph(f"<b>{label}:</b> {value}", label_style))
    flow.append(Spacer(1, 8))
    return flow


def _table_flowable(node: _Node, styles: dict[str, ParagraphStyle]) -> list:
    caption = _caption_flowables(node, styles)
    rows = _rows(node)
    if not rows:
        return caption
    cell_rows: list[list[_Node]] = []
    header = False
    for row in rows:
        cells = [c for c in row.children if isinstance(c, _Node) and c.tag in ("td", "th")]
        if not cells:
            continue
        if not cell_rows and all(cell.tag == "th" for cell in cells):
            header = True
        cell_rows.append(cells)
    if not cell_rows:
        return caption
    if max(len(row) for row in cell_rows) > _MAX_TABLE_COLUMNS:
        return caption + _stacked_table_flowables(cell_rows, header, styles)
    data: list[list[Paragraph]] = []
    for index, cells in enumerate(cell_rows):
        head = header and index == 0
        style = styles["th"] if head else styles["td"]
        face = _BOLD_FACE if head else _BODY_FACE
        data.append([Paragraph(_inline(cell, face) or "&nbsp;", style) for cell in cells])
    if not data:
        return caption
    columns = max(len(row) for row in data)
    for row in data:
        while len(row) < columns:
            row.append(Paragraph("&nbsp;", styles["td"]))
    width = _FRAME_WIDTH / columns
    style_commands = [
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d8dbe0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if header:
        style_commands.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a2f36")))
    flowable = Table(data, colWidths=[width] * columns, repeatRows=1 if header else 0)
    flowable.setStyle(TableStyle(style_commands))
    return caption + [flowable, Spacer(1, 8)]


def _list_flowables(
    node: _Node, styles: dict[str, ParagraphStyle], depth: int, ordered: bool
) -> list:
    if depth > _MAX_DEPTH:
        raise PdfRenderError("document nests too deeply to render")
    flow: list = []
    counter = 0
    for item in node.children:
        if not isinstance(item, _Node) or item.tag != "li":
            continue
        counter += 1
        marker = f"{counter}." if ordered else _BULLETS[min(depth, len(_BULLETS) - 1)]
        inline_children = _Node("li", {}, [
            child for child in item.children
            if isinstance(child, str) or child.tag not in ("ul", "ol", "table")
        ])
        text = _inline(inline_children).strip()
        style = ParagraphStyle(
            f"li{depth}",
            parent=styles["body"],
            leftIndent=14 * (depth + 1),
            bulletIndent=14 * depth,
            spaceAfter=3,
        )
        flow.append(Paragraph(text or "&nbsp;", style, bulletText=marker))
        for child in item.children:
            if isinstance(child, _Node) and child.tag in ("ul", "ol"):
                flow.extend(_list_flowables(child, styles, depth + 1, child.tag == "ol"))
            elif isinstance(child, _Node) and child.tag == "table":
                flow.extend(_table_flowable(child, styles))
    if flow:
        flow.append(Spacer(1, 4))
    return flow


def _blocks(node: _Node, styles: dict[str, ParagraphStyle], depth: int = 0) -> list:
    if depth > _MAX_DEPTH:
        raise PdfRenderError("document nests too deeply to render")
    flow: list = []
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                flow.append(Paragraph(_escape(child.strip(), _BODY_FACE), styles["body"]))
            continue
        tag = child.tag
        if tag in _HEADINGS:
            # Headings draw with the bold face, so their runs are checked there.
            text = _inline(child, _BOLD_FACE).strip()
            if text:
                flow.append(Paragraph(text, styles[tag]))
        elif tag == "p":
            text = _inline(child).strip()
            if text:
                flow.append(Paragraph(text, styles["body"]))
        elif tag in ("ul", "ol"):
            flow.extend(_list_flowables(child, styles, depth, tag == "ol"))
        elif tag == "table":
            flow.extend(_table_flowable(child, styles))
        elif tag == "hr":
            flow.append(HRFlowable(width="100%", color=colors.HexColor("#dddddd"), spaceAfter=8))
        elif tag == "img":
            flow.append(Paragraph(_escape(_image_text(child), _BODY_FACE), styles["body"]))
        elif tag == "pre":
            text = _checked(_plain_text(child).rstrip("\n"), _MONO_FACE)
            if text:
                flow.append(Preformatted(text, styles["code"]))
                flow.append(Spacer(1, 6))
        elif tag == "blockquote":
            for quoted in _blocks(child, styles, depth + 1):
                flow.append(quoted)
        elif tag in _BLOCK_CONTAINERS:
            flow.extend(_blocks(child, styles, depth + 1))
        elif tag in _VOID_TAGS:
            continue
        else:
            text = _inline(child).strip()
            if text:
                flow.append(Paragraph(text, styles["body"]))
    return flow


def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont(_BODY_FONT, 8)
    canvas.setFillColor(colors.HexColor("#666666"))
    canvas.drawRightString(_PAGE_WIDTH - _MARGIN, _MARGIN * 0.6, str(canvas.getPageNumber()))
    canvas.restoreState()


def render_markdown_pdf(md_text: str) -> bytes:
    """Render approved Markdown to PDF bytes entirely in memory.

    Raises `PdfRenderError` for anything that cannot be rendered within the
    hosted bounds; callers turn that into a redacted response.
    """
    if len(md_text.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise PdfRenderError("document is too large to render")
    body_html = md_render.markdown_to_body_html(md_text)
    parser = _FragmentParser()
    try:
        parser.feed(body_html)
        parser.close()
    except PdfRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - bounded translation failure
        raise PdfRenderError("document could not be parsed for rendering") from exc

    styles = _styles()
    flow = _blocks(parser.root, styles)
    if not flow:
        flow = [Paragraph("&nbsp;", styles["body"])]

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_TOP_MARGIN,
        bottomMargin=_MARGIN,
        title=_DOC_TITLE,
        author=_PRODUCER,
        subject=_DOC_TITLE,
        creator=_PRODUCER,
    )
    try:
        doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)
    except PdfRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - ReportLab layout failure
        raise PdfRenderError("document could not be laid out for rendering") from exc
    return buffer.getvalue()
