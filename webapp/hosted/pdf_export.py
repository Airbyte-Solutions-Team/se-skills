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
and the common symbol ranges. Without those files the renderer falls back to
Helvetica and replaces characters the built-in encoding cannot represent with
`?`, so degraded text is visibly degraded instead of quietly wrong. Scripts
outside DejaVu's coverage (CJK, emoji) still degrade; Markdown export always
carries the exact approved bytes.
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

# Character the built-in Helvetica fallback uses for text it cannot encode.
_UNSUPPORTED_CHAR = "?"

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


def _register_unicode_fonts() -> bool:
    """Register the DejaVu family when the host provides it."""
    for directory in _FONT_DIRS:
        base = Path(directory)
        if not all((base / name).is_file() for name in _FONT_FILES.values()):
            continue
        try:
            for font_name, file_name in _FONT_FILES.items():
                pdfmetrics.registerFont(TTFont(font_name, str(base / file_name)))
        except Exception:  # noqa: BLE001 - unreadable font files are not fatal
            return False
        pdfmetrics.registerFontFamily(
            "SEBody",
            normal="SEBody",
            bold="SEBody-Bold",
            italic="SEBody-Italic",
            boldItalic="SEBody-BoldItalic",
        )
        return True
    return False


UNICODE_FONTS = _register_unicode_fonts()
_BODY_FONT = "SEBody" if UNICODE_FONTS else "Helvetica"
_BOLD_FONT = "SEBody-Bold" if UNICODE_FONTS else "Helvetica-Bold"
_MONO_FONT = "SEMono" if UNICODE_FONTS else "Courier"


def _coerce(text: str) -> str:
    """Keep text honest under the built-in font's narrow encoding."""
    if UNICODE_FONTS:
        return text
    coerced: list[str] = []
    for char in text:
        try:
            char.encode("cp1252")
        except UnicodeEncodeError:
            coerced.append(_UNSUPPORTED_CHAR)
        else:
            coerced.append(char)
    return "".join(coerced)


class PdfRenderError(Exception):
    """The approved Markdown could not be rendered within hosted bounds."""


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


def _escape(text: str) -> str:
    return html.escape(_coerce(text), quote=True)


def _inline(node: _Node) -> str:
    """Render inline content as escaped text plus allowlisted ReportLab markup."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(_escape(child))
            continue
        tag = child.tag
        if tag == "br":
            parts.append("<br/>")
            continue
        if tag in _VOID_TAGS:
            continue
        inner = _inline(child)
        if tag == "code":
            parts.append(f'<font face="{_MONO_FONT}">{inner}</font>')
        elif tag == "a":
            href = child.attrs.get("href", "").strip()
            if href.lower().startswith(_ALLOWED_LINK_SCHEMES):
                parts.append(f'<link href="{_escape(href)}" color="#1558b0">{inner}</link>')
            else:
                parts.append(inner)
        elif tag in _INLINE_MARKUP:
            markup = _INLINE_MARKUP[tag]
            parts.append(f"<{markup}>{inner}</{markup}>")
        else:
            parts.append(inner)
    return "".join(parts)


def _plain_text(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            parts.append(_plain_text(child))
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


def _table_flowable(node: _Node, styles: dict[str, ParagraphStyle]) -> list:
    rows = _rows(node)
    if not rows:
        return []
    data: list[list[Paragraph]] = []
    header = False
    for index, row in enumerate(rows):
        cells = [c for c in row.children if isinstance(c, _Node) and c.tag in ("td", "th")]
        if not cells:
            continue
        if index == 0 and all(cell.tag == "th" for cell in cells):
            header = True
        style = styles["th"] if (header and index == 0) else styles["td"]
        data.append([Paragraph(_inline(cell) or "&nbsp;", style) for cell in cells[:_MAX_TABLE_COLUMNS]])
    if not data:
        return []
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
    return [flowable, Spacer(1, 8)]


def _list_flowables(
    node: _Node, styles: dict[str, ParagraphStyle], depth: int, ordered: bool
) -> list:
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
    flow: list = []
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                flow.append(Paragraph(_escape(child.strip()), styles["body"]))
            continue
        tag = child.tag
        if tag in _HEADINGS:
            text = _inline(child).strip()
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
        elif tag == "pre":
            text = _coerce(_plain_text(child).rstrip("\n"))
            if text:
                flow.append(Preformatted(text, styles["code"]))
                flow.append(Spacer(1, 6))
        elif tag == "blockquote":
            for quoted in _blocks(child, styles, depth):
                flow.append(quoted)
        elif tag in _BLOCK_CONTAINERS:
            flow.extend(_blocks(child, styles, depth))
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
