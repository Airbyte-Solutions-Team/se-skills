"""Render a skill-output markdown file to a clean, internal-facing PDF.

Pipeline: markdown -> styled HTML (print CSS) -> headless Chrome print-to-PDF.

Why server-side Chrome instead of the browser's window.print(): print-to-PDF
from the app gave no control over page breaks or the browser's own header/footer
chrome (the "about:blank" + timestamp + filename junk). This module controls
page breaks (each H2 section starts a new page; table rows never split) and emits
a clean page-number footer only.

Handles the non-standard markdown the skills emit:
  - ==highlight==                       -> <mark>
  - GitHub admonitions > [!info|risk|blocker|warning]
  - GFM task-list checkboxes - [ ] / - [x]
  - status emoji dots (🟢 🟡 🔴 …) wrapped for consistent sizing
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import markdown

# Chrome/Chromium candidates, in preference order (macOS first, then Linux).
_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "chromium",
    "chromium-browser",
]

_ADMON_LABELS = {
    "info": ("Note", "admon-info"),
    "risk": ("Risk", "admon-risk"),
    "blocker": ("Blocker", "admon-blocker"),
    "warning": ("Warning", "admon-warning"),
    "verdict": ("Verdict", "admon-info"),
}

_CSS = """
@page {
  size: Letter;
  margin: 18mm 16mm 16mm 16mm;
  @bottom-right { content: counter(page) " / " counter(pages); }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.5; color: #1a1a1a; margin: 0;
}
h1 {
  font-size: 21pt; line-height: 1.2; margin: 0 0 4pt 0;
  padding-bottom: 8pt; border-bottom: 2px solid #222; color: #111;
}
/* Each major section starts on its own page. */
h2 {
  font-size: 15pt; color: #111; margin: 0 0 8pt 0;
  padding-bottom: 4pt; border-bottom: 1px solid #ccc;
  break-before: page; break-after: avoid;
}
h3 { font-size: 12.5pt; color: #222; margin: 14pt 0 6pt 0; break-after: avoid; }
h2 + p, h2 + ul, h3 + p, h3 + ul { break-before: avoid; }
p { margin: 0 0 7pt 0; orphans: 3; widows: 3; }
ul, ol { margin: 0 0 8pt 0; padding-left: 20pt; }
li { margin: 0 0 4pt 0; break-inside: avoid; }
a { color: #1558b0; text-decoration: none; }
code {
  font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 9pt;
  background: #f0f1f3; border: 1px solid #e0e2e6; border-radius: 3px;
  padding: 0.5pt 4pt; white-space: nowrap;
}
mark { background: #fff3b0; padding: 0 2pt; border-radius: 2px; }
strong { color: #000; }
.dot { font-size: 11pt; vertical-align: -1px; }

/* Tables — never split a row across a page; repeat the header. */
table {
  width: 100%; border-collapse: collapse; margin: 4pt 0 12pt 0;
  font-size: 9.5pt; break-inside: auto;
}
thead { display: table-header-group; }
tr { break-inside: avoid; break-after: auto; }
th {
  background: #2a2f36; color: #fff; text-align: left; padding: 6pt 8pt;
  font-size: 8.5pt; letter-spacing: 0.3px; text-transform: uppercase;
  vertical-align: top;
}
td { padding: 6pt 8pt; border-bottom: 1px solid #e2e4e8; vertical-align: top; }
tbody tr:nth-child(even) { background: #f7f8fa; }

/* Callouts */
.admon {
  break-inside: avoid; margin: 10pt 0 12pt 0; padding: 9pt 12pt 9pt 14pt;
  border-radius: 4px; border-left: 4px solid #888; background: #f6f7f9;
}
.admon-label {
  font-weight: 700; font-size: 8.5pt; text-transform: uppercase;
  letter-spacing: 0.6px; margin-bottom: 4pt;
}
.admon-body p:last-child { margin-bottom: 0; }
.admon-info    { border-left-color: #2f6fb0; background: #eef4fb; }
.admon-info .admon-label    { color: #2f6fb0; }
.admon-warning { border-left-color: #c08a1e; background: #fdf6e8; }
.admon-warning .admon-label { color: #9a6a10; }
.admon-risk    { border-left-color: #c0501e; background: #fcf0ea; }
.admon-risk .admon-label    { color: #b0461a; }
.admon-blocker { border-left-color: #c01e1e; background: #fbeaea; }
.admon-blocker .admon-label { color: #b01818; }

hr { border: none; border-top: 1px solid #ddd; margin: 14pt 0; }
blockquote {
  margin: 8pt 0; padding: 8pt 12pt; background: #f6f7f9;
  border-left: 4px solid #bbb; break-inside: avoid;
}
blockquote p:last-child { margin-bottom: 0; }
em { color: #333; }
"""


def find_chrome() -> str | None:
    """Return a usable Chrome/Chromium executable path, or None."""
    if env := os.environ.get("SE_CHROME_PATH"):
        if os.path.isfile(env) or shutil.which(env):
            return env
    for cand in _CHROME_CANDIDATES:
        if os.path.isfile(cand) or shutil.which(cand):
            return cand
    return None


def markdown_to_body_html(md_text: str) -> str:
    """Convert skill-output markdown (with the non-standard extras) to an HTML
    body FRAGMENT (no <html>/<style> wrapper).

    Shared by both the PDF path and the internal.airbyte.ai HTML export so the
    non-standard markdown the skills emit — admonitions, ==highlight==, GFM
    checkboxes, status dots, the tables/lists blank-line fixup — is handled in
    exactly one place."""
    raw = md_text

    # GitHub-style admonitions: a blockquote whose first line is "> [!type] title".
    lines = raw.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^>\s*\[!(\w+)\]\s*(.*)$", line)
        if m:
            kind = m.group(1).lower()
            title = m.group(2).strip()
            label, cls = _ADMON_LABELS.get(kind, ("Note", "admon-info"))
            body: list[str] = [title] if title else []
            i += 1
            while i < len(lines) and lines[i].startswith(">"):
                body.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            body_html = markdown.markdown("\n".join(body).strip(), extensions=["extra"])
            out.append(
                f'<div class="admon {cls}"><div class="admon-label">{label}</div>'
                f'<div class="admon-body">{body_html}</div></div>'
            )
            continue
        out.append(line)
        i += 1
    raw = "\n".join(out)

    # ==highlight== -> <mark> (before the parser so it survives table cells)
    raw = re.sub(r"==(.+?)==", r"<mark>\1</mark>", raw)

    # python-markdown's tables/sane_lists need a blank line between a preceding
    # paragraph and the block; the skills omit it. Insert the missing blank line.
    fixed: list[str] = []
    prev = ""
    for ln in raw.split("\n"):
        stripped = ln.lstrip()
        starts_table = stripped.startswith("|")
        starts_list = bool(re.match(r"^(\s*[-*+]\s|\s*\d+\.\s)", ln))
        prev_is_block = (
            prev.strip() != ""
            and not prev.lstrip().startswith("|")
            and not re.match(r"^(\s*[-*+]\s|\s*\d+\.\s)", prev)
            and not prev.lstrip().startswith("#")
            and not prev.lstrip().startswith(">")
            and not prev.lstrip().startswith("<")
        )
        if (starts_table or starts_list) and prev_is_block:
            fixed.append("")
        fixed.append(ln)
        prev = ln
    raw = "\n".join(fixed)

    # GFM task-list checkboxes -> a box glyph (extra doesn't render them).
    raw = re.sub(r"^(\s*)[-*+]\s+\[ \]\s+", r"\1- ☐ ", raw, flags=re.MULTILINE)
    raw = re.sub(r"^(\s*)[-*+]\s+\[[xX]\]\s+", r"\1- ☑ ", raw, flags=re.MULTILINE)

    body = markdown.markdown(raw, extensions=["extra", "sane_lists", "toc"])

    for dot in ["🟢", "🟡", "🔴", "⚠️", "❌", "✅", "⭐", "★"]:
        body = body.replace(dot, f'<span class="dot">{dot}</span>')

    return body


def markdown_to_html(md_text: str) -> str:
    """Convert skill-output markdown to a full print-styled HTML document string
    (used by the PDF pipeline)."""
    body = markdown_to_body_html(md_text)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<style>{_CSS}</style></head><body>{body}</body></html>"
    )


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
        with open(html_path, "w") as f:
            f.write(html_doc)
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
