"""Shared look for the two PDF renderers.

`pdf_render.py` (local, headless Chrome) and `hosted/pdf_export.py` (hosted,
in-process ReportLab) render the same sanitized Markdown, and a PDF should look
the same whichever one produced it. This module is the single place the palette
and the title/kicker rules live. It is deliberately dependency-free (no ReportLab,
no Chrome) so either renderer can import it.

The palette is the internal.airbyte.ai "rs-group" design system used by the
coverage-handoff template, so exported PDFs and the internal pages read as one
family.
"""
from __future__ import annotations

import re

# rs-group design-system palette (see skills/coverage-handoff/template.html).
PRIMARY = "#1a1a2e"        # deep navy: hero band, table headers, headings
PRIMARY_MID = "#312e81"    # indigo: hero gradient end
ACCENT = "#6366f1"         # indigo accent: rules, numbers, links
ACCENT_LIGHT = "#818cf8"
ACCENT_TINT = "#eef2ff"    # pale indigo: summary card, zebra, chips
TEXT = "#1e293b"
MUTED = "#64748b"
BORDER = "#e2e8f0"
ZEBRA = "#f8fafc"
LINK = "#4338ca"

# Callout tones: (border/label colour, background).
CALLOUTS = {
    "info": ("#3b82f6", "#eff6ff"),
    "warning": ("#d97706", "#fffbeb"),
    "risk": ("#ea580c", "#fff4ed"),
    "blocker": ("#dc2626", "#fef2f2"),
}

BRAND_LABEL = "Airbyte Internal"
CONFIDENTIAL_LABEL = "Confidential"
FOOTER_TITLE_MAX = 70


_DATEISH = re.compile(
    r"^(\d{4}(?!\d)|\d{1,2}[/.-]\d|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d)",
    re.IGNORECASE,
)


def split_title(title: str) -> tuple[str, str]:
    """Split an H1 into a small kicker (the document type) and the headline.

    Skills title their output ``<Kind> — <Subject>`` (``Deal Assessment —
    Northwind Analytics``) or ``<Kind>: <Subject> — <Date>`` (``Call Summary:
    Acme — June 11, 2026``). The colon form wins when present. A dash split is
    refused when the right-hand side is just a date, so a date never becomes the
    headline. A title with no recognisable shape (a punchy verdict headline) has
    no kicker and the caller falls back to the brand label. Only the separator
    is dropped; both halves are kept verbatim.
    """
    title = title.strip()
    head, colon, tail = title.partition(":")
    if colon and head.strip() and tail.strip() and len(head) <= 40 and " " in head.strip() + " ":
        return head.strip(), tail.strip()
    parts = re.split(r"\s+[—–]\s+|\s+-\s+", title, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1] and len(parts[0]) <= 40 and not _DATEISH.match(parts[1]):
        return parts[0].strip(), parts[1].strip()
    return "", title


def short_title(title: str) -> str:
    """The title trimmed for a one-line running footer."""
    title = " ".join(title.split())
    if len(title) <= FOOTER_TITLE_MAX:
        return title
    return title[: FOOTER_TITLE_MAX - 1].rstrip() + "…"
