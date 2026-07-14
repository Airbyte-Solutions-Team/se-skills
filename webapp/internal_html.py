"""Render a skill-output markdown file to a self-contained internal.airbyte.ai
HTML page (the "rs-group" design system).

Unlike coverage-handoff — which fills a FIXED 11-section template — this exporter
is GENERIC: it renders whatever H2 sections the source markdown actually has, in
the document's own order. A deal-assessment keeps its MEDDPICC scorecard; an
account-refresher keeps its Who's Who / Story So Far. The source .md is never
modified and no section is dropped, reordered, or invented — the exporter only
changes the *skin*, wrapping each section's content in an rs-group card.

Pipeline: markdown -> body HTML fragment (shared with pdf_render, so the
non-standard markdown extras are handled identically) -> split on <h2> into
.section/.card blocks -> wrap in the rs-group chrome (header stats + auth marker
+ footer) with the coverage-handoff <style> block embedded verbatim.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from md_render import markdown_to_body_html

# The rs-group <style> block lives in the coverage-handoff template so there's a
# single source of truth for the design system. Read it at render time (verbatim,
# including the <style> tags) rather than duplicating the CSS here.
_TEMPLATE = Path(__file__).resolve().parent.parent / "skills" / "coverage-handoff" / "template.html"

_AUTH_MARKER = (
    '<div class="airbyte-auth-marker" tabindex="0" role="note" '
    'aria-label="Airbyte internal — SSO required">'
    '<span class="lock">&#128274;</span><span class="label">Airbyte Internal</span>'
    '<div class="tooltip">This page is secured by Google authentication and is only '
    'available to users with a valid <strong>@airbyte.io</strong> login account.</div>'
    "</div>"
)


def _style_block() -> str:
    """The rs-group <style>…</style> block, verbatim from the shared template.
    Falls back to a minimal style if the template is missing."""
    try:
        text = _TEMPLATE.read_text()
    except OSError:
        return "<style>body{font-family:-apple-system,sans-serif;max-width:900px;margin:0 auto;padding:24px;}</style>"
    # The template's HTML comment mentions "<style> block" in prose, so a naive
    # `<style>.*?</style>` matches the comment first and grabs junk (which breaks
    # the :root variables). Match the REAL style tag: a `<style>` at the start of
    # a line, i.e. not inside the comment.
    m = re.search(r"^<style>.*?</style>", text, re.DOTALL | re.MULTILINE)
    return m.group(0) if m else ""


def _primary_heading_level(body_html: str) -> int:
    """The shallowest heading level (2–4) used for top-level sections, AFTER the
    H1 title. Skills differ: deal-assessment leads sections with <h2>, but
    account-refresher uses <h3> throughout. We split on whichever the doc uses so
    every skill's own sections are preserved — not just H2 docs."""
    for lvl in (2, 3, 4):
        if re.search(rf"<h{lvl}[^>]*>", body_html):
            return lvl
    return 2


def _split_sections(body_html: str, level: int) -> tuple[str, list[tuple[str, str]]]:
    """Split a rendered body fragment on <hLEVEL> boundaries.

    Returns (preamble, sections) where preamble is any content before the first
    heading of that level (e.g. an At-a-Glance block) and sections is a list of
    (title, inner_html) in document order — nothing dropped or reordered."""
    parts = re.split(rf'(<h{level}[^>]*>.*?</h{level}>)', body_html, flags=re.DOTALL)
    preamble = parts[0]
    sections: list[tuple[str, str]] = []
    for i in range(1, len(parts), 2):
        heading = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ""
        title = re.sub(r"<[^>]+>", "", heading).strip()  # strip heading tags for the section title
        sections.append((title, content))
    return preamble, sections


def _extract_h1(body_html: str) -> tuple[str, str]:
    """Pull the leading <h1> (the doc title) out of the body, returning
    (title_text, body_without_h1). Title text is used in the header."""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", body_html, re.DOTALL)
    if not m:
        return "", body_html
    title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return title, body_html[: m.start()] + body_html[m.end():]


def _as_of(md_text: str) -> str:
    """The doc's own 'As of' / 'Date' line, shown as a single header stat. This
    is a fact the doc states verbatim — unlike prose At-a-Glance values, it's
    safe to surface without risk of a misleading truncation."""
    m = re.search(r"^\s*\*\*(?:As of|Date):?\*\*\s*(.+?)\s*$", md_text, re.MULTILINE)
    if not m:
        return ""
    # take just the date, before any trailing "· Source Coverage: …" clause
    return re.split(r"\s*[·|]\s*", re.sub(r"[*=`]", "", m.group(1)).strip())[0].strip()


def render_internal_html(md_text: str, *, customer: str = "", subtitle: str = "") -> str:
    """Render skill-output markdown to a self-contained rs-group HTML page.

    customer/subtitle populate the header; if omitted, the doc's own H1 is used.
    Every H2 section of the source renders as one rs-group card, in order."""
    body = markdown_to_body_html(md_text)
    doc_title, body = _extract_h1(body)
    # The doc's H1 is the headline (often a punchy verdict); the customer folder
    # name is the kicker. Prefer the H1 as the title so the header carries the
    # message, falling back to the customer name.
    title = doc_title or customer or "Airbyte Internal"

    preamble, sections = _split_sections(body, _primary_heading_level(body))

    stat_html = ""
    as_of = _as_of(md_text)
    if as_of:
        stat_html = (
            f'<div class="stats"><div class="stat">'
            f'<div class="num">{html.escape(as_of)}</div>'
            f'<div class="label">As of</div></div></div>'
        )

    section_html = ""
    if preamble.strip():
        # content before the first H2 (the At-a-Glance) leads as a full card
        section_html += f'<div class="section"><div class="card full">{preamble}</div></div>'
    for sec_title, content in sections:
        section_html += (
            f'<div class="section">'
            f'<div class="section-title">{html.escape(sec_title)}</div>'
            f'<div class="card full">{content}</div>'
            f"</div>"
        )

    # Kicker line above the title: the customer, when the H1 became the headline.
    kicker = ""
    if customer and doc_title:
        kicker = f'<div class="subtitle">{html.escape(customer)}</div>'
    sub = f'<div class="subtitle">{html.escape(subtitle)}</div>' if subtitle else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Airbyte Internal</title>
{_style_block()}
</head>
<body>
{_AUTH_MARKER}
<div class="header">
  <div class="header-inner">
    <div>
      {kicker}
      <h1>{html.escape(title)}</h1>
      {sub}
    </div>
    {stat_html}
  </div>
</div>
<div class="main">
{section_html}
</div>
<div class="footer">{html.escape(title)} &middot; Airbyte Internal &middot; Generated from SE Skills</div>
</body>
</html>"""
