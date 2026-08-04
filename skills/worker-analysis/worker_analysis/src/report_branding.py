"""Shared Airbyte branding elements for all PDF reports.

Provides reusable logo header, divider, and branded footer (icon + page number)
that can be added to any report generated with ReportLab.
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import Image, Spacer, Table, TableStyle

# Logo assets path (relative to repo root)
_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_FULL_PATH = _ASSETS_DIR / "airbyte_logo_full.png"
LOGO_ICON_PATH = _ASSETS_DIR / "airbyte_icon.png"

# Brand colors used by branding elements
AIRBYTE_BLUE_700 = colors.HexColor('#5F5CFF')
AIRBYTE_COOL_200 = colors.HexColor('#D4D4E3')
AIRBYTE_COOL_500 = colors.HexColor('#8487A4')


def get_logo_header_elements(logo_width: float = 2.0, logo_height: float = 0.8):
    """Return flowable elements for the Airbyte logo header on page 1.

    Includes the full wordmark logo and a colored divider line beneath it.
    Returns a list of flowables to prepend to the document story.
    """
    elements = []

    if LOGO_FULL_PATH.exists():
        logo = Image(
            str(LOGO_FULL_PATH),
            width=logo_width * inch,
            height=logo_height * inch,
        )
        logo.hAlign = 'LEFT'
        elements.append(logo)
        elements.append(Spacer(1, 8))

    return elements


def get_divider_line(width: float = 6.8):
    """Return a colored horizontal divider line as a flowable.

    Use after the title/subtitle area to separate from content.
    """
    divider = Table([['']], colWidths=[width * inch], rowHeights=[3])
    divider.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_BLUE_700),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    return divider


def make_branded_footer(customer_name: str, date_str: str = ""):
    """Return a footer callback function for use with `doc.build()`.

    The footer includes:
    - Airbyte icon on the left
    - Centered confidential text with customer name and date
    - Page number on the right
    - A thin separator line above all elements

    Usage:
        footer_fn = make_branded_footer("Acme Corp", "2026-07-02")
        doc.build(story, onFirstPage=footer_fn, onLaterPages=footer_fn)
    """
    footer_text = f"PREPARED FOR {customer_name.upper()}"
    if date_str:
        footer_text += f"  \u00b7  CONFIDENTIAL  {date_str}"
    else:
        footer_text += "  \u00b7  CONFIDENTIAL"

    icon_path = str(LOGO_ICON_PATH) if LOGO_ICON_PATH.exists() else None

    def _footer_callback(canvas, doc):
        canvas.saveState()
        page_width = letter[0]

        # Thin colored line above footer
        canvas.setStrokeColor(AIRBYTE_COOL_200)
        canvas.setLineWidth(0.5)
        canvas.line(0.6 * inch, 0.55 * inch, page_width - 0.6 * inch, 0.55 * inch)

        # Airbyte icon on the left
        if icon_path:
            canvas.drawImage(
                icon_path, 0.6 * inch, 0.18 * inch,
                width=14, height=14, preserveAspectRatio=True, mask='auto',
            )

        # Footer text centered
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(AIRBYTE_COOL_500)
        canvas.drawCentredString(page_width / 2, 0.25 * inch, footer_text)

        # Page number on the right
        canvas.drawRightString(
            page_width - 0.6 * inch, 0.25 * inch,
            f"Page {doc.page}",
        )
        canvas.restoreState()

    return _footer_callback
