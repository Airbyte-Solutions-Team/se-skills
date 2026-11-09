"""The shared PDF design: title splitting, the Chrome document builder, and the
print fallback. Deterministic and Chrome-free; actual rendering is covered by the
export tests that monkeypatch `find_chrome`."""
from __future__ import annotations

import re

import pytest

import pdf_render
import pdf_theme


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Deal Assessment — Northwind Analytics", ("Deal Assessment", "Northwind Analytics")),
        ("Account Refresher - Halcyon Health", ("Account Refresher", "Halcyon Health")),
        ("Call Summary: Acme — June 11, 2026", ("Call Summary", "Acme — June 11, 2026")),
        # A date is never promoted to the headline.
        ("Weekly sync — August 19, 2026", ("", "Weekly sync — August 19, 2026")),
        ("Weekly sync — 2026-08-19", ("", "Weekly sync — 2026-08-19")),
        # No recognisable shape: no kicker, headline is the whole title.
        ("Viable, gated on one security review", ("", "Viable, gated on one security review")),
    ],
)
def test_split_title(title: str, expected: tuple[str, str]) -> None:
    assert pdf_theme.split_title(title) == expected


def test_short_title_truncates_for_the_footer() -> None:
    long = "x" * 200
    assert len(pdf_theme.short_title(long)) == pdf_theme.FOOTER_TITLE_MAX
    assert pdf_theme.short_title("Short") == "Short"


_DOC = (
    "# Deal Assessment — Northwind\n\n"
    "**Date:** August 19, 2026 · **Account:** Northwind\n\n"
    "### Decision Summary\n- **Verdict:** viable\n\n"
    "## First section\n\nBody.\n\n"
    "| A | B |\n| - | - |\n| 1 | 2 |\n"
)


def test_document_has_hero_chips_summary_and_footer() -> None:
    doc = pdf_render.markdown_to_html(_DOC)
    assert '<header class="hero">' in doc
    assert '<div class="kicker">Deal Assessment</div>' in doc
    assert "<h1>Northwind</h1>" in doc
    # The metadata line becomes chips with its text intact, not dropped.
    assert doc.count('class="chip"') == 2
    assert "August 19, 2026" in doc
    # Pre-H2 content sits in the summary card; the numbered section follows it.
    assert doc.index('class="summary"') < doc.index("<h2")
    # Running footer carries the full title and the brand.
    assert "Deal Assessment — Northwind  ·  Airbyte Internal" in doc


def test_every_palette_placeholder_is_filled() -> None:
    doc = pdf_render.markdown_to_html(_DOC)
    assert not re.search(r"__[A-Z_]+__", doc)


def test_document_without_a_title_or_sections_still_renders_as_is() -> None:
    doc = pdf_render.markdown_to_html("Just a paragraph.")
    assert '<header class="hero">' not in doc
    assert 'class="summary"' not in doc
    assert "Just a paragraph." in doc


def test_footer_text_cannot_break_out_of_the_css_string() -> None:
    doc = pdf_render.markdown_to_html('# Evil "title" \\ end\n\nBody.\n')
    css = doc.split("<style>", 1)[1].split("</style>", 1)[0]
    assert 'content: "Evil \\"title\\" \\\\ end' in css


def test_print_fallback_only_applies_when_the_document_has_no_page_rule() -> None:
    plain = "<html><head></head><body>x</body></html>"
    assert "@page" in pdf_render._with_print_css(plain)
    designed = "<html><head><style>@page{margin:1mm}</style></head><body>x</body></html>"
    assert pdf_render._with_print_css(designed) == designed
