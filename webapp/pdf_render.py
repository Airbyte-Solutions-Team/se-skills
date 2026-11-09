"""Render a skill-output markdown file to a designed, internal-facing PDF.

Pipeline: markdown -> styled HTML (print CSS) -> headless Chrome print-to-PDF.

Why server-side Chrome instead of the browser's window.print(): print-to-PDF
from the app gave no control over page breaks or the browser's own header/footer
chrome (the "about:blank" + timestamp + filename junk). This module controls
page breaks (table rows never split, headings stay with their content) and emits
a hero header band plus a running footer (title, brand, page x of y). The palette
is shared with the hosted renderer via `pdf_theme`.

The core Markdown -> HTML body conversion now lives in `md_render.py` and is
shared with the web reader and the internal.airbyte.ai HTML export.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import tempfile

import pdf_theme as theme
from md_render import markdown_to_body_html

# Chrome/Chromium candidates, in preference order (macOS, Linux, then Windows).
_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "chromium",
    "chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "chrome.exe",
    "msedge.exe",
]

# `__NAME__` placeholders are filled from `pdf_theme` by `_css_for`, so the CSS
# below stays readable and the palette stays in one place.
_CSS = """
@page {
  size: Letter;
  margin: 18mm 16mm 18mm 16mm;
  @bottom-left {
    content: "__FOOTER_LEFT__";
    font: 7.5pt "Segoe UI", -apple-system, "Helvetica Neue", Arial, sans-serif;
    color: __MUTED__; vertical-align: top; padding-top: 6mm;
  }
  @bottom-right {
    content: "Page " counter(page) " of " counter(pages);
    font: 7.5pt "Segoe UI", -apple-system, "Helvetica Neue", Arial, sans-serif;
    color: __MUTED__; vertical-align: top; padding-top: 6mm;
  }
}
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font-family: "Segoe UI", -apple-system, "Helvetica Neue", Arial, sans-serif;
  font-size: 10pt; line-height: 1.55; color: __TEXT__; margin: 0;
  counter-reset: sec;
}

/* ---- Hero band ---- */
.hero {
  margin: 0 0 16pt 0; padding: 18pt 24pt 20pt 24pt; color: #fff; border-radius: 12px;
  background: linear-gradient(135deg, __PRIMARY__ 0%, __PRIMARY_MID__ 100%);
}
.hero-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16pt; }
.brand {
  font-size: 8.5pt; font-weight: 700; letter-spacing: 1.6px; text-transform: uppercase; color: #fff;
}
.brand::before {
  content: ""; display: inline-block; width: 8pt; height: 8pt; border-radius: 2.5pt;
  background: __ACCENT_LIGHT__; margin-right: 6pt; vertical-align: -0.5pt;
}
.badge {
  font-size: 7pt; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase;
  border: 1px solid rgba(255,255,255,0.4); border-radius: 20pt; padding: 2pt 8pt;
  color: rgba(255,255,255,0.85);
}
.kicker {
  font-size: 9pt; font-weight: 700; letter-spacing: 1.4px; text-transform: uppercase;
  color: #c7d2fe; margin-bottom: 4pt;
}
.hero h1 {
  font-size: 25pt; line-height: 1.15; margin: 0; font-weight: 700; color: #fff;
  padding: 0; border: none; letter-spacing: -0.3px;
}
.chips { display: flex; flex-wrap: wrap; gap: 5pt; margin-top: 13pt; }
.chip {
  font-size: 8.5pt; line-height: 1.3; color: #e0e7ff; background: rgba(255,255,255,0.12);
  border: 1px solid rgba(255,255,255,0.18); border-radius: 20pt; padding: 3pt 10pt;
}
.chip strong { color: #fff; font-weight: 700; }
.chip a { color: #fff; }

/* ---- Headings ---- */
h1 { font-size: 21pt; line-height: 1.2; margin: 0 0 8pt 0; color: __PRIMARY__; }
h2 {
  font-size: 14.5pt; line-height: 1.25; color: __PRIMARY__; margin: 22pt 0 9pt 0;
  padding-bottom: 6pt; border-bottom: 1.5pt solid __BORDER__;
  break-after: avoid; counter-increment: sec; display: flex; align-items: center;
}
h2::before {
  content: counter(sec, decimal-leading-zero);
  display: inline-block; flex: none; margin-right: 10pt; min-width: 24pt; text-align: center;
  background: __ACCENT__; color: #fff; font-size: 9pt; font-weight: 700;
  padding: 3pt 6pt; border-radius: 5pt; letter-spacing: 0.4px;
}
h3 {
  font-size: 11.5pt; color: __PRIMARY__; margin: 15pt 0 6pt 0; break-after: avoid;
  padding-left: 8pt; border-left: 3pt solid __ACCENT_LIGHT__; line-height: 1.3;
}
h4 {
  font-size: 9.5pt; color: __MUTED__; margin: 11pt 0 4pt 0; break-after: avoid;
  text-transform: uppercase; letter-spacing: 0.8px;
}
h2 + p, h2 + ul, h3 + p, h3 + ul, h2 + table, h3 + table { break-before: avoid; }
h2 + p { break-after: avoid; }

p { margin: 0 0 7pt 0; orphans: 3; widows: 3; }
ul, ol { margin: 0 0 9pt 0; padding-left: 18pt; }
li { margin: 0 0 4pt 0; }
li::marker { color: __ACCENT__; }
a { color: __LINK__; text-decoration: none; border-bottom: 0.5pt solid #c7d2fe; }
code {
  font-family: "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace; font-size: 8.6pt;
  background: __ACCENT_TINT__; color: __PRIMARY_MID__; border: 1px solid #dbe0fb;
  border-radius: 3px; padding: 0.5pt 4pt; overflow-wrap: anywhere;
}
pre {
  background: __PRIMARY__; color: #e2e8f0; border-radius: 6px; padding: 9pt 12pt;
  font-size: 8.5pt; line-height: 1.45; white-space: pre-wrap; break-inside: avoid;
}
pre code { background: none; border: none; color: inherit; padding: 0; }
mark { background: #fef08a; padding: 0 2pt; border-radius: 2px; }
strong { color: __PRIMARY__; }
.dot { font-size: 10.5pt; vertical-align: -1px; }

/* ---- Summary card: whatever leads the doc before the first numbered section ---- */
.summary {
  background: __ACCENT_TINT__; border: 1px solid #c7d2fe; border-left: 4pt solid __ACCENT__;
  border-radius: 8px; padding: 11pt 16pt 6pt 16pt; margin: 0 0 6pt 0; break-inside: avoid;
}
.summary > h3:first-child, .summary > h2:first-child {
  margin-top: 0; padding: 0; border: none; font-size: 8.5pt; text-transform: uppercase;
  letter-spacing: 1.2px; color: __ACCENT__; display: block;
}
.summary ul { list-style: none; padding-left: 0; margin-bottom: 6pt; }
.summary li { padding: 3pt 0; border-bottom: 1px solid #dbe0fb; margin: 0; }
.summary li:last-child { border-bottom: none; }
.summary li strong { color: __PRIMARY_MID__; }
.summary p:last-child { margin-bottom: 4pt; }

/* ---- Tables: never split a row; repeat the header on each page ---- */
table {
  width: 100%; border-collapse: separate; border-spacing: 0; margin: 6pt 0 13pt 0;
  font-size: 9pt; break-inside: auto; border: 1px solid __BORDER__; border-radius: 7px;
  overflow: hidden;
}
thead { display: table-header-group; }
tr { break-inside: avoid; break-after: auto; }
th {
  background: __PRIMARY__; color: #fff; text-align: left; padding: 6.5pt 9pt;
  font-size: 7.8pt; letter-spacing: 0.7px; text-transform: uppercase; vertical-align: top;
}
td { padding: 6.5pt 9pt; border-bottom: 1px solid __BORDER__; vertical-align: top; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:nth-child(even) td { background: __ZEBRA__; }
td:first-child { font-weight: 600; color: __PRIMARY__; }

/* ---- Callouts ---- */
.admon {
  break-inside: avoid; margin: 11pt 0 13pt 0; padding: 9pt 13pt 9pt 15pt;
  border-radius: 7px; border: 1px solid __BORDER__; border-left: 4pt solid #888; background: #f8fafc;
}
.admon-label {
  font-weight: 700; font-size: 8pt; text-transform: uppercase;
  letter-spacing: 0.9px; margin-bottom: 4pt;
}
.admon-body p:last-child { margin-bottom: 0; }
__CALLOUT_CSS__

hr { border: none; border-top: 1px solid __BORDER__; margin: 15pt 0; }
blockquote {
  margin: 9pt 0; padding: 9pt 14pt; background: #f8fafc; color: #334155;
  border-left: 3pt solid __ACCENT_LIGHT__; border-radius: 0 6px 6px 0; break-inside: avoid;
}
blockquote p:last-child { margin-bottom: 0; }
em { color: #334155; }
"""


def _css_for(footer_left: str) -> str:
    """Fill the palette placeholders and the running-footer text."""
    callouts = "\n".join(
        f".admon-{kind} {{ border-left-color: {fg}; background: {bg}; }}\n"
        f".admon-{kind} .admon-label {{ color: {fg}; }}"
        for kind, (fg, bg) in theme.CALLOUTS.items()
    )
    css = _CSS.replace("__CALLOUT_CSS__", callouts)
    # CSS string literal: escape backslash and quote; the text is a single line.
    css = css.replace("__FOOTER_LEFT__", footer_left.replace("\\", "\\\\").replace('"', '\\"'))
    palette = {
        "PRIMARY_MID": theme.PRIMARY_MID, "PRIMARY": theme.PRIMARY,
        "ACCENT_LIGHT": theme.ACCENT_LIGHT, "ACCENT_TINT": theme.ACCENT_TINT,
        "ACCENT": theme.ACCENT, "TEXT": theme.TEXT, "MUTED": theme.MUTED,
        "BORDER": theme.BORDER, "ZEBRA": theme.ZEBRA, "LINK": theme.LINK,
    }
    for name, value in palette.items():
        css = css.replace(f"__{name}__", value)
    return css


def find_chrome() -> str | None:
    """Return a usable Chrome/Chromium executable path, or None."""
    if env := os.environ.get("SE_CHROME_PATH"):
        if os.path.isfile(env) or shutil.which(env):
            return env
    for cand in _CHROME_CANDIDATES:
        if os.path.isfile(cand) or shutil.which(cand):
            return cand
    return None


def _plain(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _hero(body: str) -> tuple[str, str, str]:
    """Pull the leading H1 (and a following "**Key:** value · ..." line) out of
    the body into a hero band. Returns (hero_html, remaining_body, title_text).

    Nothing is dropped: the H1 splits into kicker + headline, and the metadata
    line becomes one chip per ` · ` segment with its text unchanged. A document
    with no leading H1 gets no hero and is rendered as-is.
    """
    m = re.match(r"\s*<h1[^>]*>(.*?)</h1>", body, re.DOTALL)
    if not m:
        return "", body, ""
    title = _plain(m.group(1))
    rest = body[m.end():]
    chips = ""
    pm = re.match(r"\s*<p>(.*?)</p>", rest, re.DOTALL)
    if pm and "<strong>" in pm.group(1) and "\u00b7" in pm.group(1):
        segments = [seg.strip() for seg in pm.group(1).split("\u00b7") if seg.strip()]
        chips = '<div class="chips">' + "".join(f'<span class="chip">{seg}</span>' for seg in segments) + "</div>"
        rest = rest[pm.end():]
    kicker, headline = theme.split_title(title)
    kicker_html = f'<div class="kicker">{html.escape(kicker)}</div>' if kicker else ""
    hero = (
        '<header class="hero"><div class="hero-top">'
        f'<span class="brand">{html.escape(theme.BRAND_LABEL)}</span>'
        f'<span class="badge">{html.escape(theme.CONFIDENTIAL_LABEL)}</span></div>'
        f"{kicker_html}<h1>{html.escape(headline)}</h1>{chips}</header>"
    )
    return hero, rest, title


def _wrap_summary(body: str) -> str:
    """Wrap whatever leads the document before its first H2 in a summary card
    (the Decision Summary / At-a-Glance block). A doc with no H2 is left alone."""
    m = re.search(r"<h2[\s>]", body)
    if not m or not body[: m.start()].strip():
        return body
    return f'<section class="summary">{body[: m.start()]}</section>{body[m.start():]}'


def markdown_to_html(md_text: str) -> str:
    """Convert skill-output markdown to a full print-styled HTML document string
    (used by the PDF pipeline)."""
    hero, body, title = _hero(markdown_to_body_html(md_text))
    body = _wrap_summary(body)
    footer = f"{theme.short_title(title)}  \u00b7  {theme.BRAND_LABEL}" if title else theme.BRAND_LABEL
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title or theme.BRAND_LABEL)}</title>"
        f"<style>{_css_for(footer)}</style></head><body>{hero}{body}</body></html>"
    )


# Print rules for already-styled HTML (coverage-handoff, internal HTML exports) that
# was designed for a screen. Added only when the document defines no @page of its
# own, so documents with a print design (the Opportunity export) are untouched.
_PRINT_FALLBACK_CSS = (
    "<style>@page{size:Letter;margin:12mm 10mm}"
    "html{-webkit-print-color-adjust:exact;print-color-adjust:exact}"
    ".card,.kv,tr,.coverage-banner .inner,blockquote{break-inside:avoid}"
    ".section-title,h2,h3{break-after:avoid}"
    ".airbyte-auth-marker{position:absolute!important}"
    ".airbyte-auth-marker .tooltip{display:none}</style>"
)


def _with_print_css(html_doc: str) -> str:
    if "@page" in html_doc:
        return html_doc
    if "</head>" in html_doc:
        return html_doc.replace("</head>", _PRINT_FALLBACK_CSS + "</head>", 1)
    return _PRINT_FALLBACK_CSS + html_doc


def render_html_pdf(html_doc: str) -> bytes:
    """Render a full HTML document to PDF bytes via headless Chrome. Use for
    already-styled HTML (e.g. coverage-handoff pages). Raises RuntimeError if
    Chrome isn't found."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError(
            "No Chrome/Chromium found for PDF rendering. "
            "Install Google Chrome or set SE_CHROME_PATH."
        )
    with tempfile.TemporaryDirectory() as td:
        html_path = os.path.join(td, "doc.html")
        pdf_path = os.path.join(td, "doc.pdf")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(_with_print_css(html_doc))
        subprocess.run(
            [
                chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}", f"file://{html_path}",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        with open(pdf_path, "rb") as f:
            return f.read()


def render_pdf(md_text: str) -> bytes:
    """Render markdown to PDF bytes. Raises RuntimeError if Chrome isn't found."""
    return render_html_pdf(markdown_to_html(md_text))
