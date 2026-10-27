"""Export the current canonical Opportunity Overview as a shareable HTML or PDF file.

Both formats come from one renderer, `opportunity_print.render_print_html`, which
mirrors the workspace layout. HTML is delivered as-is (self-contained, no external
assets); PDF is that same document printed by headless Chrome. Only the validated
canonical state is exported -- conclusions, gaps, and evidence *names*; transcript
bodies, hashes, and filesystem paths are never part of the state contract.
"""
from __future__ import annotations

import re
from typing import Any, Literal

import pdf_render
from opportunity_print import render_print_html
from opportunity_state import OpportunityStateVersion

ExportFormat = Literal["html", "pdf"]


def export_filename(opp_slug: str, revision: int, fmt: ExportFormat) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", opp_slug).strip("-") or "opportunity"
    return f"{safe}-opportunity-brief-r{revision}.{fmt}"


def render_export(
    version: OpportunityStateVersion,
    fmt: ExportFormat,
    *,
    account: dict[str, Any],
    opportunity: dict[str, Any],
) -> str | bytes:
    """HTML (str) or PDF (bytes). PDF raises RuntimeError when Chrome is unavailable."""
    document = render_print_html(version, account=account, opportunity=opportunity)
    return document if fmt == "html" else pdf_render.render_html_pdf(document)
