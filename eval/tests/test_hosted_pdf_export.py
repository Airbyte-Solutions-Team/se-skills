"""Slice 6B1: fidelity and inertness of the in-process hosted PDF renderer.

These tests are deterministic and need no container: they exercise the
translation from approved Markdown to ReportLab flowables, plus the bounded
in-memory build. The hosted export route is covered in `test_hosted_exports.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.platypus import Flowable, Paragraph, Preformatted, Table

from hosted import pdf_export

_FIXTURE = Path("eval/fixtures/outputs/post-call-full.md")


def _flowable_text(md_text: str) -> str:
    """Return the text the renderer would place in the PDF, in document order.

    ReportLab compresses page streams, so the rendered bytes are not greppable.
    Asserting on the flowables instead keeps the fidelity checks readable and
    still covers the only translation step the hosted export performs.
    """
    parser = pdf_export._FragmentParser()
    parser.feed(pdf_export.md_render.markdown_to_body_html(md_text))
    parser.close()
    parts: list[str] = []

    def _walk(flowables: list[Flowable]) -> None:
        for flowable in flowables:
            if isinstance(flowable, Paragraph):
                parts.append(flowable.text)
            elif isinstance(flowable, Preformatted):
                parts.extend(flowable.lines)
            elif isinstance(flowable, Table):
                for row in flowable._cellvalues:
                    _walk(list(row))

    _walk(pdf_export._blocks(parser.root, pdf_export._styles()))
    return "\n".join(parts)


def test_reviewed_material_survives_rendering() -> None:
    """Sections, tables, lists, and ordering of a real output are preserved."""
    text = _flowable_text(_FIXTURE.read_text(encoding="utf-8"))
    for heading in ("Key Takeaways", "Actions &amp; Next Step", "Source Coverage"):
        assert heading in text
    assert text.index("Key Takeaways") < text.index("Source Coverage")


@pytest.mark.parametrize(
    "markdown,present,absent",
    [
        pytest.param(
            "<script>alert(1)</script>\n\nAfter the script.",
            ["After the script."],
            ["alert(1)"],
            id="raw_script_is_dropped_by_the_shared_sanitizer",
        ),
        pytest.param(
            '<img src=x onerror="alert(1)">\n\nAfter the image.',
            ["After the image."],
            ["onerror", "alert(1)"],
            id="event_handler_never_reaches_the_pdf",
        ),
        pytest.param(
            "[click me](javascript:alert(1))",
            ["click me"],
            ["<link", "javascript:"],
            id="javascript_url_renders_as_inert_text",
        ),
        pytest.param(
            "[docs](https://docs.airbyte.com/)",
            ['<link href="https://docs.airbyte.com/"', "docs"],
            [],
            id="http_link_stays_clickable",
        ),
        pytest.param(
            "A `<b>literal</b>` snippet",
            ["&lt;b&gt;literal&lt;/b&gt;"],
            ["<b>literal</b>"],
            id="code_span_html_stays_escaped_text",
        ),
        pytest.param(
            "**bold** and *italic*",
            ["<b>bold</b>", "<i>italic</i>"],
            [],
            id="emphasis_becomes_reportlab_markup",
        ),
        pytest.param(
            "| Owner | Due |\n| --- | --- |\n| Ada | Friday |",
            ["Owner", "Due", "Ada", "Friday"],
            [],
            id="table_cells_are_preserved",
        ),
        pytest.param(
            "1. first\n2. second\n   - nested\n",
            ["first", "second", "nested"],
            [],
            id="ordered_and_nested_lists_are_preserved",
        ),
        pytest.param(
            "Unicode: café — ✓ Привет Ωμέγα",
            ["café", "✓", "Привет"] if pdf_export.UNICODE_FONTS else ["Unicode:"],
            [],
            id="unicode_is_preserved_when_unicode_fonts_exist",
        ),
    ],
)
def test_translation_is_faithful_and_inert(
    markdown: str, present: list[str], absent: list[str]
) -> None:
    text = _flowable_text(markdown)
    for needle in present:
        assert needle in text, needle
    for needle in absent:
        assert needle not in text, needle


def test_render_markdown_pdf_returns_a_pdf_document() -> None:
    body = pdf_export.render_markdown_pdf(_FIXTURE.read_text(encoding="utf-8"))
    assert body.startswith(b"%PDF-")
    assert body.rstrip().endswith(b"%%EOF")
    assert len(body) > 1000


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param("", id="empty_document"),
        pytest.param("# Only a heading", id="heading_only"),
        pytest.param("| a |\n| --- |\n" + "| x |\n" * 400, id="long_table"),
        pytest.param("\n".join(f"- item {n}" for n in range(2000)), id="long_list"),
        pytest.param("> quoted\n>\n> - inside\n", id="blockquote_with_list"),
        pytest.param("```\nraw <script>x</script>\n```\n", id="fenced_code_block"),
        pytest.param("---\n\nafter a rule\n", id="horizontal_rule"),
    ],
)
def test_bounded_documents_still_render(markdown: str) -> None:
    assert pdf_export.render_markdown_pdf(markdown).startswith(b"%PDF-")


def test_oversized_documents_are_refused_before_rendering() -> None:
    oversized = "a" * (pdf_export.MAX_MARKDOWN_BYTES + 1)
    with pytest.raises(pdf_export.PdfRenderError):
        pdf_export.render_markdown_pdf(oversized)


def test_element_count_is_bounded() -> None:
    with pytest.raises(pdf_export.PdfRenderError):
        pdf_export.render_markdown_pdf("\n\n".join(["x"] * (pdf_export._MAX_NODES + 10)))
